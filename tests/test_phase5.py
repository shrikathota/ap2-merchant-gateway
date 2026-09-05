"""
tests/test_phase5.py
=====================
Phase 5 acceptance tests: graceful failure & alternative-recovery engine.

Coverage:
  1. Out-of-stock SKU → structured FAILED payload with 2+ in-stock alternatives
     from the same category, requires_new_mandate=True.
  2. No funds moved / no order created on this path (Razorpay never called,
     zero new Transaction rows, stock of the failed SKU untouched).
  3. FAILURE_DIVERTED audit row is written (not a raw 500 / bare DENIED).
  4. AlternativeFinder unit tests: category filter, stock filter, budget
     filter, closest-price ranking, top-3 cap.
"""
from __future__ import annotations

import base64
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db.base import Base
from app.main import app
from app.models.catalog import PolicyEvaluation, PolicyOutcome, Product
from app.models.transaction import Transaction
from app.services.mandates import sign_mandate

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
        "user_id": "user-phase5",
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
        "agent_id": "agent-phase5",
        "sku_list": [{"sku": sku, "qty": qty, "unit_price_paise": unit_price, "category": category}],
        "total_amount_paise": qty * unit_price,
        "expires_at": future(1).isoformat(),
        "nonce": nonce,
    }
    payload["signature"] = sign_mandate(payload, priv)
    return payload, nonce


@pytest.fixture
async def db_engine():
    engine = create_async_engine(SQLITE_URL, echo=False)
    import app.models.audit  # noqa: F401
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
    mock = MagicMock()
    mock.create_order = AsyncMock(return_value={
        "id": "order_TEST123456789",
        "amount": 50_000,
        "currency": "INR",
        "status": "created",
        "receipt": "ap2-test",
    })
    mock.capture_payment = AsyncMock(return_value={
        "id": "pay_TEST123456789", "amount": 50_000, "currency": "INR", "status": "captured",
    })
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


async def seed_product(db_engine, sku, price=50_000, stock=10, category="books", name=None):
    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    async with factory() as s:
        s.add(Product(sku=sku, name=name or f"Test {sku}", category=category,
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
# Tests — POST /api/transact recoverable-failure recovery payload
# ===========================================================================

class TestTransactRecoveryPayload:

    @pytest.mark.asyncio
    async def test_out_of_stock_sku_returns_alternatives(self, http_client, db_engine, mock_razorpay):
        """
        Agent requests an out-of-stock SKU. Gateway returns 2+ in-stock
        alternatives from the same category, no order created, no funds moved.
        """
        priv, pub_b64 = make_keypair()
        # Failed SKU: out of stock
        await seed_product(db_engine, sku="BOOK-OOS", price=50_000, stock=0, category="books", name="Out of Stock Book")
        # In-stock alternatives, same category, within budget
        await seed_product(db_engine, sku="BOOK-ALT1", price=45_000, stock=5, category="books", name="Alt Book 1")
        await seed_product(db_engine, sku="BOOK-ALT2", price=55_000, stock=3, category="books", name="Alt Book 2")
        await seed_product(db_engine, sku="BOOK-ALT3", price=90_000, stock=2, category="books", name="Alt Book 3")
        # Different category — must never appear
        await seed_product(db_engine, sku="ELEC-1", price=50_000, stock=5, category="electronics", name="Gadget")

        intent_payload, intent_nonce = await register_intent(http_client, priv, pub_b64, max_paise=1_000_000)
        cart_payload, cart_nonce = make_cart_payload(priv, intent_nonce, sku="BOOK-OOS", unit_price=50_000)

        resp = await http_client.post("/api/transact", json={
            "cart_mandate_json": cart_payload,
            "agent_public_key_b64": pub_b64,
            "intent_public_key_b64": pub_b64,
        })
        assert resp.status_code == 200, resp.json()
        data = resp.json()

        assert data["status"] == "FAILED"
        assert data["reason"] == "INSUFFICIENT_INVENTORY"
        assert data["failed_sku"] == "BOOK-OOS"
        assert data["requires_new_mandate"] is True

        alternatives = data["alternatives"]
        assert len(alternatives) >= 2
        alt_skus = {a["sku"] for a in alternatives}
        assert "ELEC-1" not in alt_skus  # different category excluded
        assert "BOOK-OOS" not in alt_skus  # failed sku excluded
        for alt in alternatives:
            assert alt["stock_qty"] > 0
            assert alt["price_paise"] <= 1_000_000
            assert "similarity_reason" in alt and alt["similarity_reason"]

        # Upsell candidates (priced above the mandate's 50_000, still in-budget)
        # rank first, closest markup first; ALT1 (cheaper) ranks last.
        assert [a["sku"] for a in alternatives] == ["BOOK-ALT2", "BOOK-ALT3", "BOOK-ALT1"]
        assert alternatives[0]["is_upsell"] is True
        assert alternatives[0]["revenue_delta_paise"] == 5_000
        assert alternatives[-1]["is_upsell"] is False

        # No Razorpay order created
        mock_razorpay.create_order.assert_not_called()

        # No new Transaction rows
        factory = async_sessionmaker(db_engine, expire_on_commit=False)
        async with factory() as s:
            result = await s.execute(select(Transaction))
            assert result.scalars().all() == []

        # Failed SKU stock untouched (still 0, no decrement/rollback churn)
        async with factory() as s:
            result = await s.execute(select(Product).where(Product.sku == "BOOK-OOS"))
            p = result.scalars().first()
        assert p.stock_qty == 0

    @pytest.mark.asyncio
    async def test_failure_diverted_audit_row_written(self, http_client, db_engine, mock_razorpay):
        """A FAILURE_DIVERTED audit row is written, not a raw error."""
        priv, pub_b64 = make_keypair()
        await seed_product(db_engine, sku="BOOK-OOS2", price=50_000, stock=0, category="books")
        await seed_product(db_engine, sku="BOOK-ALT", price=48_000, stock=4, category="books")

        intent_payload, intent_nonce = await register_intent(http_client, priv, pub_b64)
        cart_payload, cart_nonce = make_cart_payload(priv, intent_nonce, sku="BOOK-OOS2", unit_price=50_000)

        resp = await http_client.post("/api/transact", json={
            "cart_mandate_json": cart_payload,
            "agent_public_key_b64": pub_b64,
            "intent_public_key_b64": pub_b64,
        })
        assert resp.json()["status"] == "FAILED"

        factory = async_sessionmaker(db_engine, expire_on_commit=False)
        async with factory() as s:
            result = await s.execute(
                select(PolicyEvaluation).where(
                    PolicyEvaluation.cart_nonce == cart_nonce,
                    PolicyEvaluation.outcome == PolicyOutcome.FAILURE_DIVERTED,
                )
            )
            row = result.scalars().first()
        assert row is not None
        assert row.sku == "BOOK-OOS2"
        assert row.recovery_payload_json is not None
        assert "BOOK-ALT" in row.recovery_payload_json

    @pytest.mark.asyncio
    async def test_price_drift_also_diverted(self, http_client, db_engine, mock_razorpay):
        """PRICE_DRIFT is a recoverable outcome too — same structured payload."""
        priv, pub_b64 = make_keypair()
        # Catalog price differs from mandate price -> PRICE_DRIFT
        await seed_product(db_engine, sku="BOOK-DRIFT", price=60_000, stock=5, category="books")
        await seed_product(db_engine, sku="BOOK-ALT-D", price=58_000, stock=3, category="books")

        intent_payload, intent_nonce = await register_intent(http_client, priv, pub_b64)
        cart_payload, _ = make_cart_payload(priv, intent_nonce, sku="BOOK-DRIFT", unit_price=50_000)

        resp = await http_client.post("/api/transact", json={
            "cart_mandate_json": cart_payload,
            "agent_public_key_b64": pub_b64,
            "intent_public_key_b64": pub_b64,
        })
        data = resp.json()
        assert data["status"] == "FAILED"
        assert data["reason"] == "PRICE_DRIFT"
        assert data["failed_sku"] == "BOOK-DRIFT"
        assert len(data["alternatives"]) >= 1
        mock_razorpay.create_order.assert_not_called()


# ===========================================================================
# Tests — AlternativeFinder unit tests
# ===========================================================================

class TestAlternativeFinder:

    @pytest.fixture
    async def session(self, db_engine):
        factory = async_sessionmaker(db_engine, expire_on_commit=False)
        async with factory() as s:
            yield s

    @pytest.mark.asyncio
    async def test_filters_category_stock_and_budget(self, db_engine, session):
        await seed_product(db_engine, sku="A", price=10_000, stock=5, category="books")
        await seed_product(db_engine, sku="B-wrong-cat", price=10_000, stock=5, category="toys")
        await seed_product(db_engine, sku="C-oos", price=10_000, stock=0, category="books")
        await seed_product(db_engine, sku="D-too-expensive", price=999_999, stock=5, category="books")

        from app.services.alternative_finder import AlternativeFinder
        finder = AlternativeFinder()
        results = await finder.find_alternatives(
            session, failed_sku="X", category="books", max_amount_paise=100_000,
        )
        skus = {r.sku for r in results}
        assert skus == {"A"}

    @pytest.mark.asyncio
    async def test_upsell_ranked_before_downsell_capped_at_three(self, db_engine, session):
        """
        Upsell candidates (priced above the reference, still in-budget) rank
        first, closest markup first; downsell candidates rank after, closest
        match first. The top-3 cap can push out a downsell candidate even
        when it's the closest match overall.
        """
        await seed_product(db_engine, sku="P1", price=40_000, stock=1, category="books")  # downsell, -10000
        await seed_product(db_engine, sku="P2", price=53_000, stock=1, category="books")  # upsell, +3000
        await seed_product(db_engine, sku="P3", price=48_000, stock=1, category="books")  # downsell, -2000
        await seed_product(db_engine, sku="P4", price=90_000, stock=1, category="books")  # upsell, +40000

        from app.services.alternative_finder import AlternativeFinder
        finder = AlternativeFinder()
        results = await finder.find_alternatives(
            session, failed_sku="X", category="books", max_amount_paise=200_000,
            reference_price_paise=50_000,
        )
        assert [r.sku for r in results] == ["P2", "P4", "P3"]  # P1 (closest downsell) capped out
        assert len(results) == 3
        assert [r.is_upsell for r in results] == [True, True, False]
        assert [r.revenue_delta_paise for r in results] == [3_000, 40_000, -2_000]

    @pytest.mark.asyncio
    async def test_no_reference_price_falls_back_to_cheapest_first(self, db_engine, session):
        await seed_product(db_engine, sku="Q1", price=40_000, stock=1, category="books")
        await seed_product(db_engine, sku="Q2", price=20_000, stock=1, category="books")
        await seed_product(db_engine, sku="Q3", price=30_000, stock=1, category="books")

        from app.services.alternative_finder import AlternativeFinder
        finder = AlternativeFinder()
        results = await finder.find_alternatives(
            session, failed_sku="X", category="books", max_amount_paise=200_000,
        )
        assert [r.sku for r in results] == ["Q2", "Q3", "Q1"]
        assert all(r.is_upsell is False and r.revenue_delta_paise == 0 for r in results)

    @pytest.mark.asyncio
    async def test_excludes_failed_sku_itself(self, db_engine, session):
        await seed_product(db_engine, sku="SELF", price=10_000, stock=5, category="books")

        from app.services.alternative_finder import AlternativeFinder
        finder = AlternativeFinder()
        results = await finder.find_alternatives(
            session, failed_sku="SELF", category="books", max_amount_paise=100_000,
        )
        assert results == []
