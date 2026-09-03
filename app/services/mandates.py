"""
Mandate cryptographic primitives and verification service.

Primitives
----------
sign_mandate(payload, private_key)  -> base64 signature string
verify_mandate(payload, sig_b64, public_key) -> bool

The *payload* dict MUST NOT include the "signature" key — the signature
covers the canonical (sorted-keys) JSON serialization of everything else.

MandateVerifier
---------------
Stateful async service that:
1. Verifies the CartMandate Ed25519 signature.
2. Resolves the parent IntentMandate from Redis.
3. Checks expiry on both mandates.
4. Checks total_amount_paise <= intent.max_amount_paise.
5. Checks every SKU category is in intent.allowed_categories.
6. Checks nonce hasn't been consumed (replay-protection via Redis SET NX + TTL).
"""
from __future__ import annotations

import base64
import json
import logging
from datetime import datetime, timezone

from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

from app.schemas.mandates import CartMandate, IntentMandate
from app.services.redis_client import get_redis

logger = logging.getLogger(__name__)

# Redis key prefixes
_INTENT_PREFIX = "mandate:intent:"
_NONCE_PREFIX = "mandate:nonce:"

# How long we retain intent data beyond its own expiry (grace for late carts)
_INTENT_STORE_GRACE_SECONDS = 300


# ---------------------------------------------------------------------------
# Canonical serialization
# ---------------------------------------------------------------------------

def _normalize_datetime_str(s: str) -> str:
    """
    Normalize UTC datetime strings so that +00:00 and Z suffixes produce
    identical canonical bytes.  Both Python's datetime.isoformat() and
    Pydantic v2's model_dump(mode='json') are supported as inputs.
    """
    # Python isoformat() emits "+00:00"; Pydantic v2 emits "Z"
    if s.endswith("+00:00"):
        return s[:-6] + "Z"
    return s


def _normalize_value(v: object) -> object:
    """Recursively normalise datetime strings in a JSON-serializable value."""
    if isinstance(v, str):
        return _normalize_datetime_str(v)
    if isinstance(v, dict):
        return {k: _normalize_value(val) for k, val in v.items()}
    if isinstance(v, list):
        return [_normalize_value(item) for item in v]
    return v


def _canonical_json(payload: dict) -> bytes:
    """
    Deterministic UTF-8 JSON — sorted keys, no extra whitespace.

    Datetime strings are normalized (``+00:00`` → ``Z``) before serialization
    so that Python's ``datetime.isoformat()`` and Pydantic's
    ``model_dump(mode='json')`` produce identical canonical bytes.
    """
    normalized = _normalize_value(payload)
    return json.dumps(normalized, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


# ---------------------------------------------------------------------------
# Signing / verification primitives
# ---------------------------------------------------------------------------

def sign_mandate(payload: dict, private_key: Ed25519PrivateKey) -> str:
    """
    Sign *payload* (must NOT contain 'signature') with *private_key*.

    Returns: base64-encoded signature string (URL-safe, no padding stripped).
    """
    if "signature" in payload:
        raise ValueError("payload must not contain 'signature' before signing")
    message = _canonical_json(payload)
    raw_sig = private_key.sign(message)
    return base64.b64encode(raw_sig).decode()


def verify_mandate(payload: dict, signature_b64: str, public_key: Ed25519PublicKey) -> bool:
    """
    Verify *signature_b64* over *payload* using *public_key*.

    The payload must NOT include the 'signature' key (strip it before calling).
    Returns True if valid, False otherwise (never raises on bad sigs).
    """
    if "signature" in payload:
        raise ValueError("payload must not contain 'signature' during verification")
    try:
        message = _canonical_json(payload)
        raw_sig = base64.b64decode(signature_b64)
        public_key.verify(raw_sig, message)
        return True
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Helpers — key loading
# ---------------------------------------------------------------------------

def load_public_key_b64(b64: str) -> Ed25519PublicKey:
    """Load an Ed25519PublicKey from 32-byte base64-encoded raw key material."""
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey as _PK
    raw = base64.b64decode(b64)
    return Ed25519PublicKey.from_public_bytes(raw)


def load_private_key_b64(b64: str) -> Ed25519PrivateKey:
    """Load an Ed25519PrivateKey from 32-byte base64-encoded raw seed."""
    raw = base64.b64decode(b64)
    return Ed25519PrivateKey.from_private_bytes(raw)


# ---------------------------------------------------------------------------
# Redis storage helpers
# ---------------------------------------------------------------------------

def _utcnow() -> datetime:
    return datetime.now(tz=timezone.utc)


def _mandate_payload(mandate: IntentMandate) -> dict:
    """Convert IntentMandate to a storable dict (JSON-serializable)."""
    return mandate.model_dump(mode="json")


async def store_intent_mandate(intent: IntentMandate, intent_id: str) -> None:
    """Persist IntentMandate in Redis under its intent_id."""
    redis = await get_redis()
    data = json.dumps(_mandate_payload(intent))
    now = _utcnow()
    expires_at = intent.expires_at
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    ttl = max(int((expires_at - now).total_seconds()) + _INTENT_STORE_GRACE_SECONDS, 60)
    await redis.set(_INTENT_PREFIX + intent_id, data, ex=ttl)
    logger.debug("Stored intent %s (TTL=%ds)", intent_id, ttl)


async def load_intent_mandate(intent_id: str) -> IntentMandate | None:
    """Retrieve IntentMandate from Redis, or None if missing/expired."""
    redis = await get_redis()
    raw = await redis.get(_INTENT_PREFIX + intent_id)
    if raw is None:
        return None
    data = json.loads(raw)
    return IntentMandate.model_validate(data)


async def consume_nonce(nonce: str, expires_at: datetime) -> bool:
    """
    Attempt to consume *nonce*.  Returns True if this is the FIRST use
    (nonce was not yet seen), False on replay.

    Uses Redis SET NX (atomic) with TTL derived from mandate expiry.
    """
    redis = await get_redis()
    now = _utcnow()
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    ttl = max(int((expires_at - now).total_seconds()), 1)
    key = _NONCE_PREFIX + nonce
    # SET key value NX EX ttl — returns True only if key was absent
    result = await redis.set(key, "1", nx=True, ex=ttl)
    return result is True


# ---------------------------------------------------------------------------
# MandateVerifier
# ---------------------------------------------------------------------------

class VerificationError(Exception):
    """Raised with a machine-readable reason code."""

    def __init__(self, reason: str, detail: str = ""):
        self.reason = reason
        self.detail = detail
        super().__init__(f"{reason}: {detail}" if detail else reason)


class MandateVerifier:
    """
    Stateful mandate verification service.

    Usage::

        verifier = MandateVerifier()
        result = await verifier.verify_cart(cart_mandate, agent_public_key_b64)
    """

    async def register_intent(
        self,
        intent: IntentMandate,
        public_key_b64: str,
    ) -> str:
        """
        Validate and store an IntentMandate.

        Returns the intent_id (= intent.nonce) that callers should record.
        Raises VerificationError on any failure.
        """
        # 1. Check expiry
        now = _utcnow()
        expires_at = intent.expires_at
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        if expires_at <= now:
            raise VerificationError("INTENT_EXPIRED", f"expires_at={expires_at.isoformat()}")

        # 2. Verify intent signature
        pub = load_public_key_b64(public_key_b64)
        payload = intent.model_dump(mode="json")
        sig = payload.pop("signature")
        if not verify_mandate(payload, sig, pub):
            raise VerificationError("INTENT_SIGNATURE_INVALID")

        # 3. Check intent nonce not already registered (prevents replay of intents too)
        intent_id = intent.nonce
        existing = await load_intent_mandate(intent_id)
        if existing is not None:
            raise VerificationError("INTENT_NONCE_REUSED", f"intent_id={intent_id}")

        # 4. Store
        await store_intent_mandate(intent, intent_id)
        return intent_id

    async def verify_cart(
        self,
        cart: CartMandate,
        agent_public_key_b64: str,
    ) -> str:
        """
        Fully verify a CartMandate against its parent IntentMandate.

        Returns the intent_id on success.
        Raises VerificationError with a machine-readable reason on any failure.
        """
        now = _utcnow()

        # ---- Step 1: Verify cart Ed25519 signature ----
        pub = load_public_key_b64(agent_public_key_b64)
        cart_payload = cart.model_dump(mode="json")
        cart_sig = cart_payload.pop("signature")
        if not verify_mandate(cart_payload, cart_sig, pub):
            raise VerificationError("CART_SIGNATURE_INVALID")

        # ---- Step 2: Cart expiry ----
        cart_expires = cart.expires_at
        if cart_expires.tzinfo is None:
            cart_expires = cart_expires.replace(tzinfo=timezone.utc)
        if cart_expires <= now:
            raise VerificationError("CART_EXPIRED", f"expires_at={cart_expires.isoformat()}")

        # ---- Step 3: Resolve parent IntentMandate ----
        intent = await load_intent_mandate(cart.parent_intent_id)
        if intent is None:
            raise VerificationError(
                "INTENT_NOT_FOUND", f"parent_intent_id={cart.parent_intent_id}"
            )

        # ---- Step 4: Intent expiry ----
        intent_expires = intent.expires_at
        if intent_expires.tzinfo is None:
            intent_expires = intent_expires.replace(tzinfo=timezone.utc)
        if intent_expires <= now:
            raise VerificationError(
                "INTENT_EXPIRED", f"intent expires_at={intent_expires.isoformat()}"
            )

        # ---- Step 5: Budget check ----
        if cart.total_amount_paise > intent.max_amount_paise:
            raise VerificationError(
                "BUDGET_EXCEEDED",
                f"cart={cart.total_amount_paise} > intent_max={intent.max_amount_paise}",
            )

        # ---- Step 6: SKU category whitelist ----
        allowed = set(intent.allowed_categories)
        for item in cart.sku_list:
            if item.category not in allowed:
                raise VerificationError(
                    "CATEGORY_NOT_ALLOWED",
                    f"sku={item.sku} category={item.category!r} not in {sorted(allowed)}",
                )

        # ---- Step 7: Nonce replay-protection (atomic, must be last) ----
        consumed = await consume_nonce(cart.nonce, cart.expires_at)
        if not consumed:
            raise VerificationError("NONCE_REUSED", f"nonce={cart.nonce}")

        return cart.parent_intent_id