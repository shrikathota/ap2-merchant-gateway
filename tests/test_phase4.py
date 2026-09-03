"""
tests/test_phase4.py
=====================
Phase 4 acceptance tests: Razorpay order creation + payment confirmation + stock race.

Test strategy
-------------
- Razorpay SDK calls are mocked (no real API keys needed for automated tests).
- SQLite in-memory DB for all ORM operations.
- fakeredis for mandate storage.
- Real asyncio concurrency test for the race-condition guard.

Coverage:
  1. Happy path: APPROVED → real Razorpay order ID returned.
  2. confirm-payment → SETTLED, GET shows SETTLED.
  3. Concurrent requests on stock_qty=1 → exactly one APPROVED, one INSUFFICIENT_INVENTORY.
  4. GET /api/transact/{order_id} → correct fields.
  5. confirm-payment fails (Razorpay error) → status FAILED.
  6. Duplicate confirm-payment → 409 Conflict.
"""
from __future__ import annotations

import asyncio
import base64
import uuid
from datetime import datetime, timedelta, timezone
from typing import AsyncGenerator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.db.base import Base
from app.main import app
from app.models.transaction import Transaction, TransactionStatus
from app.models.catalog import Product, PolicyEvaluation
from app.services.mandates import sign_mandate


# ===========================================================================
# Helpers
# ===========================================================================

SQLITE_URL = "sqlite+aiosqlite:///:memory:"


def utcnow() -> datetime:
    return datetime.now(tz=timezone.utc)


def future(hours: float = 2.0) -> datetime:
    return utcnow() + timedelta(hours=hours)


def make_keypair() -> tuple[Ed25519PrivateKey, str]:
    priv = Ed25519PrivateKey.generate()
    return priv, base64.b64encode(priv.public_key().public_bytes_raw()).decode()


def make_intent_payload(priv, max_paise=1_000_000, categories=None):
    nonce = str(uuid.uuid4())
    payload = {
        "user_id": "user-phase4",
        "max_amount_paise": max_paise,
        "currency": "INR",
        "allowed_categories": categories or ["books", "electronics"],
        "expires_at": future(2).isoformat(),
        "nonce": nonce,
    }
    payload["signature"] = sign_mandate(payload, priv)
    return payload, nonce


def make_cart_payload(priv, intent_nonce, sku="BOOK-001", qty=1, unit_price=50_000, category="books"):
    nonce = str(uuid.uuid4())
    payload = {
        "parent_intent_id": intent_nonce,
        "agent_id": "agent-phase4",
        "sku_list": [{"sku": sku, "qty": qty, "unit_price_paise": unit_price, "category": category}],
        "total_amount_paise": qty * unit_price,
        "expires_at": future(1).isoformat(),
        "nonce": nonce,
    }
    payload["signature"] = sign_mandate(payload, priv)
    return payload, nonce


# ===========================================================================
# Fixtures
# ===========================================================================

@pytest.fixture
async def db_engine():
    engine = create_async_engine(SQLITE_URL, echo=False)
    import app.models.catalog  # noqa: F401
    import app.models.transaction  # noqa: F401
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()


@pytest.fixture
def fake_redis():
    try:
        import fakeredis.aioredis as fr
    except ImportError:
        pytest.skip("fakeredis not installed")
    r = fr.FakeRedis(decode_responses=True)
    with patch("app.services.mandates.get_redis", AsyncMock(return_value=r)):
        yield r


@pytest.fixture
def mock_razorpay():
    """
    Return a mock RazorpayClient whose create_order / capture_payment
    are AsyncMocks with configurable return values.
    """
    mock = MagicMock()
    mock.create_order = AsyncMock(return_value={
        "id": "order_TEST123456789",
        "amount": 50_000,
        "currency": "INR",
        "status": "created",
        "receipt": "ap2-test",
    })
    mock.capture_payment = AsyncMock(return_value={
        "id": "pay_TEST123456789",
        "amount": 50_000,
        "currency": "INR",
        "status": "captured",
    })
    mock.fetch_order = AsyncMock(return_value={"id": "order_TEST123456789", "status": "created"})
    return mock


@pytest.fixture
async def http_client(db_engine, fake_redis, mock_razorpay):
    from app.db.session import get_db as real_get_db
    from app.services.razorpay_client import get_razorpay as real_get_razorpay

    session_factory = async_sessionmaker(db_engine, expire_on_commit=False)

    async def override_get_db():
        async with session_factory() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    def override_get_razorpay():
        return mock_razorpay

    app.dependency_overrides[real_get_db] = override_get_db
    app.dependency_overrides[real_get_razorpay] = override_get_razorpay

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield client

    app.dependency_overrides.clear()


async def seed_product(
    db_engine,
    sku="BOOK-001",
    price=50_000,
    stock=10,
    category="books",
):
    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    async with factory() as s:
        s.add(Product(sku=sku, name=f"Test {sku}", category=category,
                      unit_price_paise=price, stock_qty=stock))
        await s.commit()


async def register_intent(client, priv, pub_b64, **kwargs):
    payload, nonce = make_intent_payload(priv, **kwargs)
    r = await client.post("/api/mandates/intent", json={
        "mandate": payload, "public_key_b64": pub_b64
    })
    assert r.status_code == 201, r.json()
    return payload, nonce


# ===========================================================================
# Tests — RazorpayClient wrapper
# ===========================================================================

class TestRazorpayClient:
    """Unit tests for the async wrapper (no real Razorpay calls)."""

    def test_initialization(self):
        from app.services.razorpay_client import RazorpayClient
        client = RazorpayClient(key_id="rzp_test_FAKE", key_secret="fakesecret")
        assert client._client is not None

    @pytest.mark.asyncio
    async def test_create_order_passes_amount(self):
        from app.services.razorpay_client import RazorpayClient
        client = RazorpayClient(key_id="rzp_test_FAKE", key_secret="fakesecret")
        captured = {}

        def fake_create(data):
            captured.update(data)
            return {"id": "order_MOCK001", "amount": data["amount"], "currency": data["currency"], "status": "created"}

        client._client.order.create = fake_create
        order = await client.create_order(amount_paise=100_000, currency="INR",
                                          receipt="receipt-1",
                                          notes={"k": "v"})
        assert order["id"] == "order_MOCK001"
        assert captured["amount"] == 100_000
        assert captured["currency"] == "INR"
        assert captured["notes"] == {"k": "v"}

    @pytest.mark.asyncio
    async def test_capture_payment(self):
        from app.services.razorpay_client import RazorpayClient
        client = RazorpayClient(key_id="rzp_test_FAKE", key_secret="fakesecret")
        calls = []

        def fake_capture(payment_id, amount, data):
            calls.append((payment_id, amount, data))
            return {"id": payment_id, "status": "captured"}

        client._client.payment.capture = fake_capture
        result = await client.capture_payment("pay_FAKE001", 50_000, "INR")
        assert result["status"] == "captured"
        assert calls[0][0] == "pay_FAKE001"
        assert calls[0][1] == 50_000


# ===========================================================================
# Tests — Atomic stock service
# ===========================================================================

class TestStockService:
    @pytest.fixture
    async def session(self, db_engine):
        factory = async_sessionmaker(db_engine, expire_on_commit=False)
        async with factory() as s:
            yield s

    @pytest.mark.asyncio
    async def test_decrement_atomic_ok(self, db_engine, session):
        await seed_product(db_engine, sku="S1", stock=5)
        from app.services.stock import decrement_stock_atomic
        await decrement_stock_atomic(session, "S1", 3)
        await session.commit()
        # Verify new stock
        from sqlalchemy import select
        result = await session.execute(select(Product).where(Product.sku == "S1"))
        p = result.scalars().first()
        assert p.stock_qty == 2

    @pytest.mark.asyncio
    async def test_decrement_atomic_exact_qty(self, db_engine, session):
        """qty == stock_qty should succeed (boundary)."""
        await seed_product(db_engine, sku="S2", stock=3)
        from app.services.stock import decrement_stock_atomic
        await decrement_stock_atomic(session, "S2", 3)
        await session.commit()
        result = await session.execute(select(Product).where(Product.sku == "S2"))
        p = result.scalars().first()
        assert p.stock_qty == 0

    @pytest.mark.asyncio
    async def test_decrement_atomic_raises_on_insufficient(self, db_engine, session):
        await seed_product(db_engine, sku="S3", stock=1)
        from app.services.stock import decrement_stock_atomic, StockUnavailable
        with pytest.raises(StockUnavailable) as exc_info:
            await decrement_stock_atomic(session, "S3", 2)
        assert exc_info.value.sku == "S3"

    @pytest.mark.asyncio
    async def test_rollback_stock(self, db_engine, session):
        await seed_product(db_engine, sku="S4", stock=5)
        from app.services.stock import decrement_stock_atomic, rollback_stock
        await decrement_stock_atomic(session, "S4", 3)
        await rollback_stock(session, "S4", 3)
        await session.commit()
        result = await session.execute(select(Product).where(Product.sku == "S4"))
        p = result.scalars().first()
        assert p.stock_qty == 5


# ===========================================================================
# Tests — POST /api/transact (Phase 4 integration)
# ===========================================================================

class TestTransactPhase4:

    @pytest.mark.asyncio
    async def test_approved_returns_razorpay_order_id(self, http_client, db_engine, mock_razorpay):
        """Happy path: valid mandate + product → APPROVED with order ID."""
        priv, pub_b64 = make_keypair()
        await seed_product(db_engine, sku="BOOK-001", price=50_000, stock=10)
        intent_payload, intent_nonce = await register_intent(http_client, priv, pub_b64)
        cart_payload, _ = make_cart_payload(priv, intent_nonce)

        resp = await http_client.post("/api/transact", json={
            "cart_mandate_json": cart_payload,
            "agent_public_key_b64": pub_b64,
            "intent_public_key_b64": pub_b64,
        })
        assert resp.status_code == 200, resp.json()
        data = resp.json()
        assert data["status"] == "APPROVED"
        assert data["razorpay_order_id"] == "order_TEST123456789"
        assert data["next"] == "proceed_to_payment_capture"

    @pytest.mark.asyncio
    async def test_approved_calls_razorpay_with_correct_amount(self, http_client, db_engine, mock_razorpay):
        priv, pub_b64 = make_keypair()
        await seed_product(db_engine, sku="BOOK-001", price=75_000, stock=5)
        intent_payload, intent_nonce = await register_intent(http_client, priv, pub_b64, max_paise=500_000)
        cart_payload, _ = make_cart_payload(priv, intent_nonce, unit_price=75_000)

        await http_client.post("/api/transact", json={
            "cart_mandate_json": cart_payload,
            "agent_public_key_b64": pub_b64,
            "intent_public_key_b64": pub_b64,
        })
        mock_razorpay.create_order.assert_called_once()
        call_kwargs = mock_razorpay.create_order.call_args
        assert call_kwargs.kwargs["amount_paise"] == 75_000
        assert call_kwargs.kwargs["currency"] == "INR"
        notes = call_kwargs.kwargs["notes"]
        assert "mandate_id" in notes
        assert "parent_intent_id" in notes
        assert "agent_id" in notes
        assert "mandate_signature" in notes

    @pytest.mark.asyncio
    async def test_approved_creates_transaction_row(self, http_client, db_engine, mock_razorpay):
        priv, pub_b64 = make_keypair()
        await seed_product(db_engine, sku="BOOK-001", price=50_000, stock=10)
        intent_payload, intent_nonce = await register_intent(http_client, priv, pub_b64)
        cart_payload, cart_nonce = make_cart_payload(priv, intent_nonce)

        await http_client.post("/api/transact", json={
            "cart_mandate_json": cart_payload,
            "agent_public_key_b64": pub_b64,
            "intent_public_key_b64": pub_b64,
        })

        factory = async_sessionmaker(db_engine, expire_on_commit=False)
        async with factory() as s:
            result = await s.execute(select(Transaction).where(Transaction.cart_nonce == cart_nonce))
            txn = result.scalars().first()
        assert txn is not None
        assert txn.status == TransactionStatus.PENDING_PAYMENT
        assert txn.razorpay_order_id == "order_TEST123456789"
        assert txn.amount_paise == 50_000

    @pytest.mark.asyncio
    async def test_approved_decrements_stock(self, http_client, db_engine, mock_razorpay):
        priv, pub_b64 = make_keypair()
        await seed_product(db_engine, sku="BOOK-001", price=50_000, stock=5)
        intent_payload, intent_nonce = await register_intent(http_client, priv, pub_b64)
        cart_payload, _ = make_cart_payload(priv, intent_nonce, qty=2)

        resp = await http_client.post("/api/transact", json={
            "cart_mandate_json": cart_payload,
            "agent_public_key_b64": pub_b64,
            "intent_public_key_b64": pub_b64,
        })
        assert resp.json()["status"] == "APPROVED"

        factory = async_sessionmaker(db_engine, expire_on_commit=False)
        async with factory() as s:
            result = await s.execute(select(Product).where(Product.sku == "BOOK-001"))
            p = result.scalars().first()
        assert p.stock_qty == 3   # 5 - 2 = 3

    @pytest.mark.asyncio
    async def test_razorpay_failure_rolls_back_stock(self, http_client, db_engine, mock_razorpay):
        """If Razorpay create_order fails, stock must be restored."""
        mock_razorpay.create_order = AsyncMock(side_effect=Exception("Razorpay down"))
        priv, pub_b64 = make_keypair()
        await seed_product(db_engine, sku="BOOK-001", price=50_000, stock=5)
        intent_payload, intent_nonce = await register_intent(http_client, priv, pub_b64)
        cart_payload, _ = make_cart_payload(priv, intent_nonce)

        resp = await http_client.post("/api/transact", json={
            "cart_mandate_json": cart_payload,
            "agent_public_key_b64": pub_b64,
            "intent_public_key_b64": pub_b64,
        })
        assert resp.json()["status"] == "DENIED"
        assert resp.json()["reason"] == "PAYMENT_GATEWAY_ERROR"

        # Stock must be restored
        factory = async_sessionmaker(db_engine, expire_on_commit=False)
        async with factory() as s:
            result = await s.execute(select(Product).where(Product.sku == "BOOK-001"))
            p = result.scalars().first()
        assert p.stock_qty == 5   # fully restored

    @pytest.mark.asyncio
    async def test_insufficient_inventory_does_not_decrement_stock(self, http_client, db_engine, mock_razorpay):
        priv, pub_b64 = make_keypair()
        await seed_product(db_engine, sku="BOOK-001", price=50_000, stock=0)
        intent_payload, intent_nonce = await register_intent(http_client, priv, pub_b64)
        cart_payload, _ = make_cart_payload(priv, intent_nonce)

        resp = await http_client.post("/api/transact", json={
            "cart_mandate_json": cart_payload,
            "agent_public_key_b64": pub_b64,
            "intent_public_key_b64": pub_b64,
        })
        assert resp.json()["status"] == "DENIED"
        assert resp.json()["reason"] == "INSUFFICIENT_INVENTORY"
        mock_razorpay.create_order.assert_not_called()


# ===========================================================================
# Tests — confirm-payment endpoint
# ===========================================================================

class TestConfirmPayment:

    @pytest.mark.asyncio
    async def test_confirm_payment_settles_transaction(self, http_client, db_engine, mock_razorpay):
        priv, pub_b64 = make_keypair()
        await seed_product(db_engine, sku="BOOK-001", price=50_000, stock=5)
        intent_payload, intent_nonce = await register_intent(http_client, priv, pub_b64)
        cart_payload, _ = make_cart_payload(priv, intent_nonce)

        # Create order
        r = await http_client.post("/api/transact", json={
            "cart_mandate_json": cart_payload,
            "agent_public_key_b64": pub_b64,
            "intent_public_key_b64": pub_b64,
        })
        assert r.json()["status"] == "APPROVED"
        order_id = r.json()["razorpay_order_id"]

        # Confirm payment
        r2 = await http_client.post(f"/api/transact/{order_id}/confirm-payment", json={
            "payment_id": "pay_TESTPAYMENT001"
        })
        assert r2.status_code == 200, r2.json()
        data = r2.json()
        assert data["status"] == "SETTLED"
        assert data["razorpay_payment_id"] == "pay_TESTPAYMENT001"

    @pytest.mark.asyncio
    async def test_get_transaction_shows_settled(self, http_client, db_engine, mock_razorpay):
        priv, pub_b64 = make_keypair()
        await seed_product(db_engine, sku="BOOK-001", price=50_000, stock=5)
        intent_payload, intent_nonce = await register_intent(http_client, priv, pub_b64)
        cart_payload, _ = make_cart_payload(priv, intent_nonce)

        r = await http_client.post("/api/transact", json={
            "cart_mandate_json": cart_payload,
            "agent_public_key_b64": pub_b64,
            "intent_public_key_b64": pub_b64,
        })
        order_id = r.json()["razorpay_order_id"]
        await http_client.post(f"/api/transact/{order_id}/confirm-payment", json={
            "payment_id": "pay_SETTLED001"
        })

        # GET status
        r3 = await http_client.get(f"/api/transact/{order_id}")
        assert r3.status_code == 200, r3.json()
        data = r3.json()
        assert data["status"] == "SETTLED"
        assert data["razorpay_order_id"] == order_id
        assert data["amount_paise"] == 50_000
        assert data["currency"] == "INR"

    @pytest.mark.asyncio
    async def test_confirm_payment_failed_marks_failed(self, http_client, db_engine, mock_razorpay):
        """Razorpay capture fails → transaction moves to FAILED."""
        mock_razorpay.capture_payment = AsyncMock(side_effect=Exception("Payment declined"))
        priv, pub_b64 = make_keypair()
        await seed_product(db_engine, sku="BOOK-001", price=50_000, stock=5)
        intent_payload, intent_nonce = await register_intent(http_client, priv, pub_b64)
        cart_payload, _ = make_cart_payload(priv, intent_nonce)

        r = await http_client.post("/api/transact", json={
            "cart_mandate_json": cart_payload,
            "agent_public_key_b64": pub_b64,
            "intent_public_key_b64": pub_b64,
        })
        order_id = r.json()["razorpay_order_id"]

        r2 = await http_client.post(f"/api/transact/{order_id}/confirm-payment", json={
            "payment_id": "pay_DECLINED001"
        })
        assert r2.status_code == 200
        assert r2.json()["status"] == "FAILED"
        assert "failure_reason" in r2.json()

    @pytest.mark.asyncio
    async def test_double_confirm_returns_409(self, http_client, db_engine, mock_razorpay):
        """Attempting to confirm an already-settled transaction → 409."""
        priv, pub_b64 = make_keypair()
        await seed_product(db_engine, sku="BOOK-001", price=50_000, stock=5)
        intent_payload, intent_nonce = await register_intent(http_client, priv, pub_b64)
        cart_payload, _ = make_cart_payload(priv, intent_nonce)

        r = await http_client.post("/api/transact", json={
            "cart_mandate_json": cart_payload,
            "agent_public_key_b64": pub_b64,
            "intent_public_key_b64": pub_b64,
        })
        order_id = r.json()["razorpay_order_id"]
        await http_client.post(f"/api/transact/{order_id}/confirm-payment", json={"payment_id": "pay_FIRST"})
        r2 = await http_client.post(f"/api/transact/{order_id}/confirm-payment", json={"payment_id": "pay_SECOND"})
        assert r2.status_code == 409

    @pytest.mark.asyncio
    async def test_get_nonexistent_order_returns_404(self, http_client, db_engine, mock_razorpay):
        r = await http_client.get("/api/transact/order_DOESNOTEXIST")
        assert r.status_code == 404


# ===========================================================================
# Tests — Race condition: concurrent requests on stock_qty=1
# ===========================================================================

class TestStockRaceCondition:

    @pytest.mark.asyncio
    async def test_concurrent_requests_only_one_wins(self, db_engine, fake_redis, mock_razorpay):
        """
        Two concurrent POST /api/transact on the same SKU with stock_qty=1.
        Exactly one must succeed (APPROVED) and one must fail (INSUFFICIENT_INVENTORY).
        This proves the atomic UPDATE ... WHERE stock_qty >= qty guard works.
        """
        from app.db.session import get_db as real_get_db
        from app.services.razorpay_client import get_razorpay as real_get_razorpay

        session_factory = async_sessionmaker(db_engine, expire_on_commit=False)

        async def override_get_db():
            async with session_factory() as session:
                try:
                    yield session
                    await session.commit()
                except Exception:
                    await session.rollback()
                    raise

        # Give each agent a unique mock so their order IDs are distinct
        def make_rzp_mock(order_id: str):
            m = MagicMock()
            m.create_order = AsyncMock(return_value={
                "id": order_id, "amount": 50_000, "currency": "INR", "status": "created"
            })
            m.capture_payment = AsyncMock(return_value={"id": "pay_X", "status": "captured"})
            return m

        rzp_mock_1 = make_rzp_mock("order_AGENT1_WIN")
        rzp_mock_2 = make_rzp_mock("order_AGENT2_WIN")
        rzp_mocks = [rzp_mock_1, rzp_mock_2]
        call_index = 0

        def override_get_razorpay():
            nonlocal call_index
            mock = rzp_mocks[call_index % 2]
            call_index += 1
            return mock

        app.dependency_overrides[real_get_db] = override_get_db
        app.dependency_overrides[real_get_razorpay] = override_get_razorpay

        try:
            # Seed product with stock_qty=1
            await seed_product(db_engine, sku="RARE-001", price=50_000, stock=1)

            # Two agents, both with valid intent + cart mandates
            priv1, pub1 = make_keypair()
            priv2, pub2 = make_keypair()

            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                # Register both intents
                intent1, nonce1 = make_intent_payload(priv1, max_paise=500_000)
                intent2, nonce2 = make_intent_payload(priv2, max_paise=500_000)
                r1 = await client.post("/api/mandates/intent", json={"mandate": intent1, "public_key_b64": pub1})
                r2 = await client.post("/api/mandates/intent", json={"mandate": intent2, "public_key_b64": pub2})
                assert r1.status_code == 201, r1.json()
                assert r2.status_code == 201, r2.json()

                # Build cart for both (same SKU RARE-001, qty=1)
                cart1, _ = make_cart_payload(priv1, nonce1, sku="RARE-001", qty=1, unit_price=50_000)
                cart2, _ = make_cart_payload(priv2, nonce2, sku="RARE-001", qty=1, unit_price=50_000)

                # Fire both concurrently
                results = await asyncio.gather(
                    client.post("/api/transact", json={
                        "cart_mandate_json": cart1,
                        "agent_public_key_b64": pub1,
                        "intent_public_key_b64": pub1,
                    }),
                    client.post("/api/transact", json={
                        "cart_mandate_json": cart2,
                        "agent_public_key_b64": pub2,
                        "intent_public_key_b64": pub2,
                    }),
                    return_exceptions=False,
                )

            statuses = [r.json()["status"] for r in results]
            reasons = [r.json().get("reason") for r in results]

            # Exactly one APPROVED, one DENIED
            assert statuses.count("APPROVED") == 1, f"Expected 1 APPROVED, got: {statuses}"
            assert statuses.count("DENIED") == 1, f"Expected 1 DENIED, got: {statuses}"

            # The denial reason must be INSUFFICIENT_INVENTORY
            denied_reason = reasons[statuses.index("DENIED")]
            assert denied_reason == "INSUFFICIENT_INVENTORY", f"Got: {denied_reason}"

            # Final stock must be 0 (decremented by the winner)
            factory = async_sessionmaker(db_engine, expire_on_commit=False)
            async with factory() as s:
                result = await s.execute(select(Product).where(Product.sku == "RARE-001"))
                p = result.scalars().first()
            assert p.stock_qty == 0, f"Expected stock=0 after race, got {p.stock_qty}"

        finally:
            app.dependency_overrides.clear()