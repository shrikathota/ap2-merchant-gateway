"""
tests/test_phase6.py
======================
Phase 6 acceptance tests: append-only audit ledger + GET /api/audit/{intent_id}.

Coverage:
  1. Full successful Phase 4 flow (intent -> cart -> approve -> settle) writes
     exactly the expected ordered event chain: INTENT_VERIFIED, CART_VERIFIED,
     BUDGET_PASSED, POLICY_PASSED, ORDER_CREATED, SETTLED.
  2. A Phase 5 failure (INSUFFICIENT_INVENTORY) writes a chain ending in
     FAILURE_DIVERTED, with the alternatives payload attached and visible.
  3. GET /api/audit/latest returns the most recently active intent_id.
  4. GET /api/audit/{intent_id} for an unknown id returns an empty chain
     (not a 404 — the ledger is just empty for that key).
  5. Immutability: there is no DELETE or PATCH route on /api/audit/{intent_id}
     or /api/audit/{event_id} — the API returns 404/405, not 200.
"""
from __future__ import annotations

import base64
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db.base import Base
from app.main import app
from app.models.catalog import Product
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
        "user_id": "user-phase6",
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
        "agent_id": "agent-phase6",
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
        "id": "order_PHASE6TEST", "amount": 50_000, "currency": "INR", "status": "created",
    })
    mock.capture_payment = AsyncMock(return_value={
        "id": "pay_PHASE6TEST", "amount": 50_000, "currency": "INR", "status": "captured",
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
# Full successful flow -> full event chain
# ===========================================================================

class TestAuditChainHappyPath:

    @pytest.mark.asyncio
    async def test_full_flow_writes_ordered_chain(self, http_client, db_engine, mock_razorpay):
        priv, pub_b64 = make_keypair()
        await seed_product(db_engine, sku="BOOK-001", price=50_000, stock=10)
        intent_payload, intent_nonce = await register_intent(http_client, priv, pub_b64)
        cart_payload, cart_nonce = make_cart_payload(priv, intent_nonce)

        r = await http_client.post("/api/transact", json={
            "cart_mandate_json": cart_payload,
            "agent_public_key_b64": pub_b64,
            "intent_public_key_b64": pub_b64,
        })
        assert r.json()["status"] == "APPROVED"
        order_id = r.json()["razorpay_order_id"]

        r2 = await http_client.post(f"/api/transact/{order_id}/confirm-payment", json={
            "payment_id": "pay_PHASE6TEST"
        })
        assert r2.json()["status"] == "SETTLED"

        chain_resp = await http_client.get(f"/api/audit/{intent_nonce}")
        assert chain_resp.status_code == 200, chain_resp.json()
        data = chain_resp.json()
        assert data["intent_id"] == intent_nonce

        event_types = [e["event_type"] for e in data["events"]]
        assert event_types == [
            "INTENT_VERIFIED",
            "CART_VERIFIED",
            "BUDGET_PASSED",
            "POLICY_PASSED",
            "ORDER_CREATED",
            "SETTLED",
        ]

        # Timestamps are non-decreasing (ordered chain)
        timestamps = [e["timestamp"] for e in data["events"]]
        assert timestamps == sorted(timestamps)

        # Every event in this flow carries the same intent_id
        assert all(e["intent_id"] == intent_nonce for e in data["events"])

        # ORDER_CREATED payload carries the real Razorpay order id
        order_event = next(e for e in data["events"] if e["event_type"] == "ORDER_CREATED")
        assert order_event["payload_snapshot"]["razorpay_order_id"] == order_id

        # SETTLED payload carries the payment id
        settled_event = next(e for e in data["events"] if e["event_type"] == "SETTLED")
        assert settled_event["payload_snapshot"]["razorpay_payment_id"] == "pay_PHASE6TEST"

    @pytest.mark.asyncio
    async def test_capture_rejected_writes_terminal_audit_event(self, http_client, db_engine, mock_razorpay):
        """
        When payment capture fails, the chain must not silently trail off after
        ORDER_CREATED — a CAPTURE_REJECTED event should close it out, mirroring
        SETTLED on the success path.
        """
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
        assert r2.json()["status"] == "FAILED"

        chain_resp = await http_client.get(f"/api/audit/{intent_nonce}")
        data = chain_resp.json()
        event_types = [e["event_type"] for e in data["events"]]

        assert event_types == [
            "INTENT_VERIFIED",
            "CART_VERIFIED",
            "BUDGET_PASSED",
            "POLICY_PASSED",
            "ORDER_CREATED",
            "CAPTURE_REJECTED",
        ]
        rejected = next(e for e in data["events"] if e["event_type"] == "CAPTURE_REJECTED")
        assert rejected["payload_snapshot"]["razorpay_order_id"] == order_id
        assert rejected["payload_snapshot"]["attempted_payment_id"] == "pay_DECLINED001"
        assert "Payment declined" in rejected["payload_snapshot"]["reason"]

    @pytest.mark.asyncio
    async def test_audit_latest_returns_most_recent_intent(self, http_client, db_engine, mock_razorpay):
        priv, pub_b64 = make_keypair()
        await seed_product(db_engine, sku="BOOK-001", price=50_000, stock=10)
        _, intent_nonce_1 = await register_intent(http_client, priv, pub_b64)
        _, intent_nonce_2 = await register_intent(http_client, priv, pub_b64)

        r = await http_client.get("/api/audit/latest")
        assert r.status_code == 200
        assert r.json()["intent_id"] == intent_nonce_2

    @pytest.mark.asyncio
    async def test_unknown_intent_returns_empty_chain(self, http_client):
        r = await http_client.get("/api/audit/does-not-exist")
        assert r.status_code == 200
        data = r.json()
        assert data["intent_id"] == "does-not-exist"
        assert data["events"] == []


# ===========================================================================
# Failure flow -> chain ends in FAILURE_DIVERTED with alternatives visible
# ===========================================================================

class TestAuditChainFailureDiverted:

    @pytest.mark.asyncio
    async def test_out_of_stock_writes_failure_diverted_with_alternatives(
        self, http_client, db_engine, mock_razorpay
    ):
        priv, pub_b64 = make_keypair()
        await seed_product(db_engine, sku="BOOK-OOS", price=50_000, stock=0, category="books")
        await seed_product(db_engine, sku="BOOK-ALT", price=48_000, stock=5, category="books")

        intent_payload, intent_nonce = await register_intent(http_client, priv, pub_b64)
        cart_payload, cart_nonce = make_cart_payload(priv, intent_nonce, sku="BOOK-OOS", unit_price=50_000)

        r = await http_client.post("/api/transact", json={
            "cart_mandate_json": cart_payload,
            "agent_public_key_b64": pub_b64,
            "intent_public_key_b64": pub_b64,
        })
        assert r.json()["status"] == "FAILED"

        chain_resp = await http_client.get(f"/api/audit/{intent_nonce}")
        data = chain_resp.json()
        event_types = [e["event_type"] for e in data["events"]]

        assert event_types == ["INTENT_VERIFIED", "CART_VERIFIED", "BUDGET_PASSED", "FAILURE_DIVERTED"]
        # Pipeline stopped: no order/settlement events past the diversion
        assert "ORDER_CREATED" not in event_types
        assert "SETTLED" not in event_types

        diverted = next(e for e in data["events"] if e["event_type"] == "FAILURE_DIVERTED")
        payload = diverted["payload_snapshot"]
        assert payload["failed_sku"] == "BOOK-OOS"
        assert payload["reason"] == "INSUFFICIENT_INVENTORY"
        assert payload["requires_new_mandate"] is True
        alt_skus = {a["sku"] for a in payload["alternatives"]}
        assert "BOOK-ALT" in alt_skus
        mock_razorpay.create_order.assert_not_called()


# ===========================================================================
# Immutability: no mutation routes exist
# ===========================================================================

class TestAuditImmutability:

    @pytest.mark.asyncio
    async def test_delete_audit_event_not_allowed(self, http_client):
        """No DELETE route exists anywhere under /api/audit — must be 404 or 405, never 200."""
        r = await http_client.delete("/api/audit/some-intent-id")
        assert r.status_code in (404, 405)

    @pytest.mark.asyncio
    async def test_patch_audit_event_not_allowed(self, http_client):
        r = await http_client.patch("/api/audit/some-intent-id", json={"event_type": "SETTLED"})
        assert r.status_code in (404, 405)

    @pytest.mark.asyncio
    async def test_put_audit_event_not_allowed(self, http_client):
        r = await http_client.put("/api/audit/some-intent-id", json={})
        assert r.status_code in (404, 405)

    @pytest.mark.asyncio
    async def test_delete_by_numeric_event_id_not_allowed(self, http_client, db_engine):
        """Even addressing a specific event row by id has no delete route."""
        r = await http_client.delete("/api/audit/events/1")
        assert r.status_code == 404
