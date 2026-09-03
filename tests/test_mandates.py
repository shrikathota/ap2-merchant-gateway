"""
tests/test_mandates.py
======================
Phase 2 acceptance tests for the AP2 mandate chain.

Tests cover:
  - sign_mandate / verify_mandate primitives
  - MandateVerifier.register_intent
  - MandateVerifier.verify_cart — happy path
  - Nonce replay detection
  - Budget exceeded
  - Tampered signature
  - Expired mandates
  - Category not in whitelist
  - HTTP API layer (via httpx AsyncClient)

Redis calls are mocked with fakeredis so tests run without docker compose.
"""
from __future__ import annotations

import base64
import json
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from httpx import ASGITransport, AsyncClient

from app.main import app
from app.schemas.mandates import CartMandate, IntentMandate, SkuItem
from app.services.mandates import (
    MandateVerifier,
    VerificationError,
    _canonical_json,
    _normalize_datetime_str,
    sign_mandate,
    verify_mandate,
)


# ---------------------------------------------------------------------------
# Fixtures & helpers
# ---------------------------------------------------------------------------

def utcnow() -> datetime:
    return datetime.now(tz=timezone.utc)


def future(hours: float = 1.0) -> datetime:
    return utcnow() + timedelta(hours=hours)


def past(hours: float = 1.0) -> datetime:
    return utcnow() - timedelta(hours=hours)


def make_keypair() -> tuple[Ed25519PrivateKey, str]:
    """Returns (private_key, public_key_b64)."""
    priv = Ed25519PrivateKey.generate()
    pub_b64 = base64.b64encode(priv.public_key().public_bytes_raw()).decode()
    return priv, pub_b64


def make_intent(
    priv: Ed25519PrivateKey,
    max_paise: int = 500_000,
    categories: list[str] | None = None,
    expires_at: datetime | None = None,
    nonce: str | None = None,
) -> IntentMandate:
    nonce = nonce or str(uuid.uuid4())
    expires_at = expires_at or future(1)
    payload = {
        "user_id": "test-user",
        "max_amount_paise": max_paise,
        "currency": "INR",
        "allowed_categories": categories or ["electronics", "books"],
        "expires_at": expires_at.isoformat(),
        "nonce": nonce,
    }
    sig = sign_mandate(payload, priv)
    return IntentMandate(**payload, signature=sig)


def make_cart(
    priv: Ed25519PrivateKey,
    intent_nonce: str,
    total_paise: int = 300_000,
    expires_at: datetime | None = None,
    nonce: str | None = None,
    sku_list: list[dict] | None = None,
) -> CartMandate:
    nonce = nonce or str(uuid.uuid4())
    expires_at = expires_at or future(0.25)
    if sku_list is None:
        # Build a valid sku_list where all unit prices are >= 0
        unit_price = max(total_paise, 0)
        sku_list = [
            {"sku": "ITEM-001", "qty": 1, "unit_price_paise": unit_price, "category": "books"},
        ]
    payload = {
        "parent_intent_id": intent_nonce,
        "agent_id": "agent-001",
        "sku_list": sku_list,
        "total_amount_paise": total_paise,
        "expires_at": expires_at.isoformat(),
        "nonce": nonce,
    }
    sig = sign_mandate(payload, priv)
    return CartMandate(**payload, signature=sig)


@pytest.fixture
def keypair() -> tuple[Ed25519PrivateKey, str]:
    return make_keypair()


# ---------------------------------------------------------------------------
# fakeredis fixture — patches app.services.mandates.get_redis as AsyncMock
# ---------------------------------------------------------------------------

@pytest.fixture
def fake_redis():
    """
    Provide a fakeredis async instance and patch get_redis() in the mandates module.
    get_redis is an async function, so we use AsyncMock with return_value=r.
    """
    try:
        import fakeredis.aioredis as fakeredis_async
    except ImportError:
        pytest.skip("fakeredis not installed; run: pip install fakeredis")

    r = fakeredis_async.FakeRedis(decode_responses=True)
    mock_get_redis = AsyncMock(return_value=r)
    with patch("app.services.mandates.get_redis", mock_get_redis):
        yield r


# ---------------------------------------------------------------------------
# Unit tests — canonical JSON primitives
# ---------------------------------------------------------------------------

class TestCanonicalJson:
    def test_sorted_keys(self):
        out = _canonical_json({"b": 1, "a": 2})
        parsed = json.loads(out)
        assert list(parsed.keys()) == ["a", "b"]

    def test_no_extra_spaces(self):
        raw = _canonical_json({"x": 1}).decode()
        assert " " not in raw

    def test_deterministic(self):
        d = {"z": 99, "a": [1, 2], "m": {"nested": True}}
        assert _canonical_json(d) == _canonical_json(d)

    def test_datetime_normalization_plus00(self):
        """Python isoformat() '+00:00' and Pydantic 'Z' produce identical bytes."""
        payload_plus = {"ts": "2024-01-01T12:00:00+00:00"}
        payload_z    = {"ts": "2024-01-01T12:00:00Z"}
        assert _canonical_json(payload_plus) == _canonical_json(payload_z)

    def test_normalize_datetime_str(self):
        assert _normalize_datetime_str("2024-01-01T12:00:00+00:00") == "2024-01-01T12:00:00Z"
        assert _normalize_datetime_str("2024-01-01T12:00:00Z") == "2024-01-01T12:00:00Z"
        assert _normalize_datetime_str("hello") == "hello"


# ---------------------------------------------------------------------------
# Unit tests — sign / verify primitives
# ---------------------------------------------------------------------------

class TestSignVerify:
    def test_round_trip(self, keypair):
        priv, pub_b64 = keypair
        from app.services.mandates import load_public_key_b64
        pub = load_public_key_b64(pub_b64)
        payload = {"foo": "bar", "n": 42}
        sig = sign_mandate(payload, priv)
        assert verify_mandate(payload, sig, pub)

    def test_tampered_payload_fails(self, keypair):
        priv, pub_b64 = keypair
        from app.services.mandates import load_public_key_b64
        pub = load_public_key_b64(pub_b64)
        payload = {"foo": "bar"}
        sig = sign_mandate(payload, priv)
        tampered = {"foo": "BAZ"}
        assert not verify_mandate(tampered, sig, pub)

    def test_tampered_signature_fails(self, keypair):
        priv, pub_b64 = keypair
        from app.services.mandates import load_public_key_b64
        pub = load_public_key_b64(pub_b64)
        payload = {"data": "hello"}
        sig_b64 = sign_mandate(payload, priv)
        raw = bytearray(base64.b64decode(sig_b64))
        raw[0] ^= 0xFF
        bad_sig = base64.b64encode(bytes(raw)).decode()
        assert not verify_mandate(payload, bad_sig, pub)

    def test_sign_rejects_signature_key(self, keypair):
        priv, _ = keypair
        with pytest.raises(ValueError, match="must not contain 'signature'"):
            sign_mandate({"a": 1, "signature": "x"}, priv)

    def test_verify_rejects_signature_key(self, keypair):
        priv, pub_b64 = keypair
        from app.services.mandates import load_public_key_b64
        pub = load_public_key_b64(pub_b64)
        with pytest.raises(ValueError, match="must not contain 'signature'"):
            verify_mandate({"a": 1, "signature": "x"}, "sig", pub)

    def test_wrong_key_fails(self):
        priv1, _ = make_keypair()
        priv2, pub2_b64 = make_keypair()
        from app.services.mandates import load_public_key_b64
        pub2 = load_public_key_b64(pub2_b64)
        payload = {"msg": "hello"}
        sig = sign_mandate(payload, priv1)
        assert not verify_mandate(payload, sig, pub2)

    def test_key_order_independence(self, keypair):
        """Canonical JSON must make signing order-independent."""
        priv, pub_b64 = keypair
        from app.services.mandates import load_public_key_b64
        pub = load_public_key_b64(pub_b64)
        payload_a = {"z": 1, "a": 2}
        payload_b = {"a": 2, "z": 1}
        sig = sign_mandate(payload_a, priv)
        assert verify_mandate(payload_b, sig, pub)

    def test_python_isoformat_matches_pydantic_z(self, keypair):
        """
        Signing with Python's isoformat() (+00:00) must verify against a payload
        using Pydantic's 'Z' suffix — proving datetime normalization works.
        """
        priv, pub_b64 = keypair
        from app.services.mandates import load_public_key_b64
        pub = load_public_key_b64(pub_b64)
        payload_python = {"ts": "2024-06-15T10:30:00+00:00", "val": 1}
        payload_pydantic = {"ts": "2024-06-15T10:30:00Z", "val": 1}
        sig = sign_mandate(payload_python, priv)
        assert verify_mandate(payload_pydantic, sig, pub)


# ---------------------------------------------------------------------------
# Integration tests — Redis-backed MandateVerifier
# ---------------------------------------------------------------------------

class TestMandateVerifier:
    @pytest.fixture
    def verifier(self):
        return MandateVerifier()

    @pytest.mark.asyncio
    async def test_register_intent_ok(self, verifier, keypair, fake_redis):
        priv, pub_b64 = keypair
        intent = make_intent(priv)
        intent_id = await verifier.register_intent(intent, pub_b64)
        assert intent_id == intent.nonce

    @pytest.mark.asyncio
    async def test_register_intent_stores_in_redis(self, verifier, keypair, fake_redis):
        priv, pub_b64 = keypair
        intent = make_intent(priv)
        intent_id = await verifier.register_intent(intent, pub_b64)
        from app.services.mandates import load_intent_mandate
        loaded = await load_intent_mandate(intent_id)
        assert loaded is not None
        assert loaded.nonce == intent.nonce

    @pytest.mark.asyncio
    async def test_register_intent_expired(self, verifier, keypair, fake_redis):
        priv, pub_b64 = keypair
        intent = make_intent(priv, expires_at=past(1))
        with pytest.raises(VerificationError) as exc_info:
            await verifier.register_intent(intent, pub_b64)
        assert exc_info.value.reason == "INTENT_EXPIRED"

    @pytest.mark.asyncio
    async def test_register_intent_bad_signature(self, verifier, keypair, fake_redis):
        priv, pub_b64 = keypair
        intent = make_intent(priv)
        raw = bytearray(base64.b64decode(intent.signature))
        raw[0] ^= 0xFF
        intent = intent.model_copy(update={"signature": base64.b64encode(bytes(raw)).decode()})
        with pytest.raises(VerificationError) as exc_info:
            await verifier.register_intent(intent, pub_b64)
        assert exc_info.value.reason == "INTENT_SIGNATURE_INVALID"

    @pytest.mark.asyncio
    async def test_register_intent_nonce_reuse(self, verifier, keypair, fake_redis):
        priv, pub_b64 = keypair
        intent = make_intent(priv)
        await verifier.register_intent(intent, pub_b64)
        with pytest.raises(VerificationError) as exc_info:
            await verifier.register_intent(intent, pub_b64)
        assert exc_info.value.reason == "INTENT_NONCE_REUSED"

    @pytest.mark.asyncio
    async def test_verify_cart_happy_path(self, verifier, keypair, fake_redis):
        priv, pub_b64 = keypair
        intent = make_intent(priv, max_paise=500_000)
        intent_id = await verifier.register_intent(intent, pub_b64)
        cart = make_cart(priv, intent_id, total_paise=300_000)
        result = await verifier.verify_cart(cart, pub_b64)
        assert result == intent_id

    @pytest.mark.asyncio
    async def test_verify_cart_nonce_replay(self, verifier, keypair, fake_redis):
        priv, pub_b64 = keypair
        intent = make_intent(priv, max_paise=500_000)
        intent_id = await verifier.register_intent(intent, pub_b64)
        cart = make_cart(priv, intent_id, total_paise=300_000)
        await verifier.verify_cart(cart, pub_b64)
        with pytest.raises(VerificationError) as exc_info:
            await verifier.verify_cart(cart, pub_b64)
        assert exc_info.value.reason == "NONCE_REUSED"

    @pytest.mark.asyncio
    async def test_verify_cart_budget_exceeded(self, verifier, keypair, fake_redis):
        priv, pub_b64 = keypair
        intent = make_intent(priv, max_paise=100_000)
        intent_id = await verifier.register_intent(intent, pub_b64)
        cart = make_cart(priv, intent_id, total_paise=200_000)
        with pytest.raises(VerificationError) as exc_info:
            await verifier.verify_cart(cart, pub_b64)
        assert exc_info.value.reason == "BUDGET_EXCEEDED"

    @pytest.mark.asyncio
    async def test_verify_cart_category_not_allowed(self, verifier, keypair, fake_redis):
        priv, pub_b64 = keypair
        intent = make_intent(priv, categories=["books"])  # no electronics
        intent_id = await verifier.register_intent(intent, pub_b64)
        # cart has category "electronics" which is not in ["books"]
        cart = make_cart(priv, intent_id, total_paise=50_000, sku_list=[
            {"sku": "ELEC-001", "qty": 1, "unit_price_paise": 50_000, "category": "electronics"},
        ])
        with pytest.raises(VerificationError) as exc_info:
            await verifier.verify_cart(cart, pub_b64)
        assert exc_info.value.reason == "CATEGORY_NOT_ALLOWED"

    @pytest.mark.asyncio
    async def test_verify_cart_bad_signature(self, verifier, keypair, fake_redis):
        priv, pub_b64 = keypair
        intent = make_intent(priv)
        intent_id = await verifier.register_intent(intent, pub_b64)
        cart = make_cart(priv, intent_id, total_paise=300_000)
        raw = bytearray(base64.b64decode(cart.signature))
        raw[32] ^= 0xFF
        cart = cart.model_copy(update={"signature": base64.b64encode(bytes(raw)).decode()})
        with pytest.raises(VerificationError) as exc_info:
            await verifier.verify_cart(cart, pub_b64)
        assert exc_info.value.reason == "CART_SIGNATURE_INVALID"

    @pytest.mark.asyncio
    async def test_verify_cart_expired(self, verifier, keypair, fake_redis):
        priv, pub_b64 = keypair
        intent = make_intent(priv)
        intent_id = await verifier.register_intent(intent, pub_b64)
        cart = make_cart(priv, intent_id, total_paise=100_000, expires_at=past(1))
        with pytest.raises(VerificationError) as exc_info:
            await verifier.verify_cart(cart, pub_b64)
        assert exc_info.value.reason == "CART_EXPIRED"

    @pytest.mark.asyncio
    async def test_verify_cart_intent_not_found(self, verifier, keypair, fake_redis):
        priv, pub_b64 = keypair
        cart = make_cart(priv, "nonexistent-intent-id", total_paise=100_000)
        with pytest.raises(VerificationError) as exc_info:
            await verifier.verify_cart(cart, pub_b64)
        assert exc_info.value.reason == "INTENT_NOT_FOUND"

    @pytest.mark.asyncio
    async def test_verify_cart_exactly_at_budget(self, verifier, keypair, fake_redis):
        """Edge case: cart total == max_amount_paise should PASS."""
        priv, pub_b64 = keypair
        intent = make_intent(priv, max_paise=300_000)
        intent_id = await verifier.register_intent(intent, pub_b64)
        cart = make_cart(priv, intent_id, total_paise=300_000)
        result = await verifier.verify_cart(cart, pub_b64)
        assert result == intent_id


# ---------------------------------------------------------------------------
# HTTP API tests
# ---------------------------------------------------------------------------

@pytest.fixture
def fake_redis_api():
    """Patch get_redis for the API layer (same fakeredis instance per test)."""
    try:
        import fakeredis.aioredis as fakeredis_async
    except ImportError:
        pytest.skip("fakeredis not installed")

    r = fakeredis_async.FakeRedis(decode_responses=True)
    mock_get_redis = AsyncMock(return_value=r)
    with patch("app.services.mandates.get_redis", mock_get_redis):
        yield r


@pytest.fixture
async def http_client(fake_redis_api) -> AsyncClient:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield client


class TestMandateAPI:
    @pytest.mark.asyncio
    async def test_post_intent_returns_201(self, http_client):
        priv, pub_b64 = make_keypair()
        intent = make_intent(priv)
        resp = await http_client.post("/api/mandates/intent", json={
            "mandate": intent.model_dump(mode="json"),
            "public_key_b64": pub_b64,
        })
        assert resp.status_code == 201, resp.json()
        assert resp.json()["intent_id"] == intent.nonce

    @pytest.mark.asyncio
    async def test_post_intent_duplicate_returns_422(self, http_client):
        priv, pub_b64 = make_keypair()
        intent = make_intent(priv)
        body = {"mandate": intent.model_dump(mode="json"), "public_key_b64": pub_b64}
        r1 = await http_client.post("/api/mandates/intent", json=body)
        assert r1.status_code == 201, r1.json()
        resp = await http_client.post("/api/mandates/intent", json=body)
        assert resp.status_code == 422
        assert resp.json()["detail"]["reason"] == "INTENT_NONCE_REUSED"

    @pytest.mark.asyncio
    async def test_verify_cart_happy_path_api(self, http_client):
        priv, pub_b64 = make_keypair()
        intent = make_intent(priv, max_paise=500_000)
        r = await http_client.post("/api/mandates/intent", json={
            "mandate": intent.model_dump(mode="json"), "public_key_b64": pub_b64
        })
        assert r.status_code == 201, r.json()
        cart = make_cart(priv, intent.nonce, total_paise=300_000)
        resp = await http_client.post("/api/mandates/verify-cart", json={
            "mandate": cart.model_dump(mode="json"), "public_key_b64": pub_b64
        })
        assert resp.status_code == 200, resp.json()
        assert resp.json()["verified"] is True

    @pytest.mark.asyncio
    async def test_verify_cart_nonce_replay_api(self, http_client):
        priv, pub_b64 = make_keypair()
        intent = make_intent(priv, max_paise=500_000)
        r = await http_client.post("/api/mandates/intent", json={
            "mandate": intent.model_dump(mode="json"), "public_key_b64": pub_b64
        })
        assert r.status_code == 201, r.json()
        cart = make_cart(priv, intent.nonce, total_paise=300_000)
        body = {"mandate": cart.model_dump(mode="json"), "public_key_b64": pub_b64}
        r1 = await http_client.post("/api/mandates/verify-cart", json=body)
        assert r1.json()["verified"] is True
        resp = await http_client.post("/api/mandates/verify-cart", json=body)
        assert resp.status_code == 200
        assert resp.json()["verified"] is False
        assert resp.json()["reason"] == "NONCE_REUSED"

    @pytest.mark.asyncio
    async def test_verify_cart_budget_exceeded_api(self, http_client):
        priv, pub_b64 = make_keypair()
        intent = make_intent(priv, max_paise=100_000)
        r = await http_client.post("/api/mandates/intent", json={
            "mandate": intent.model_dump(mode="json"), "public_key_b64": pub_b64
        })
        assert r.status_code == 201, r.json()
        cart = make_cart(priv, intent.nonce, total_paise=500_000)
        resp = await http_client.post("/api/mandates/verify-cart", json={
            "mandate": cart.model_dump(mode="json"), "public_key_b64": pub_b64
        })
        assert resp.status_code == 200
        assert resp.json()["verified"] is False
        assert resp.json()["reason"] == "BUDGET_EXCEEDED"

    @pytest.mark.asyncio
    async def test_verify_cart_tampered_sig_api(self, http_client):
        priv, pub_b64 = make_keypair()
        intent = make_intent(priv, max_paise=500_000)
        r = await http_client.post("/api/mandates/intent", json={
            "mandate": intent.model_dump(mode="json"), "public_key_b64": pub_b64
        })
        assert r.status_code == 201, r.json()
        cart = make_cart(priv, intent.nonce, total_paise=100_000)
        cart_data = cart.model_dump(mode="json")
        raw = bytearray(base64.b64decode(cart_data["signature"]))
        raw[32] ^= 0xFF
        cart_data["signature"] = base64.b64encode(bytes(raw)).decode()
        resp = await http_client.post("/api/mandates/verify-cart", json={
            "mandate": cart_data, "public_key_b64": pub_b64
        })
        assert resp.status_code == 422, resp.json()
        assert resp.json()["detail"]["reason"] == "CART_SIGNATURE_INVALID"