"""
Pydantic v2 schemas for AP2 mandate types.

IntentMandate  — top-level budget/category authorization (signed by user)
CartMandate    — per-cart spend request (signed by agent, chained to an intent)
SkuItem        — single line item within a cart
"""
from __future__ import annotations

import base64
from datetime import datetime
from typing import Annotated

from pydantic import BaseModel, Field, field_validator


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _b64_validator(v: str) -> str:
    """Ensure value is valid base64."""
    try:
        base64.b64decode(v, validate=True)
    except Exception as exc:
        raise ValueError(f"must be valid base64: {exc}") from exc
    return v


B64Str = Annotated[str, Field(min_length=1)]


# ---------------------------------------------------------------------------
# SKU line item
# ---------------------------------------------------------------------------

class SkuItem(BaseModel):
    """Single product line inside a CartMandate."""
    sku: str = Field(..., min_length=1, description="Product SKU identifier")
    qty: int = Field(..., ge=1, description="Quantity (≥1)")
    unit_price_paise: int = Field(..., ge=0, description="Unit price in paise (≥0)")
    category: str = Field(..., min_length=1, description="Product category (must match intent allowed_categories)")


# ---------------------------------------------------------------------------
# IntentMandate
# ---------------------------------------------------------------------------

class IntentMandate(BaseModel):
    """
    Top-level spending authorization issued by the user.

    The *signature* covers the canonical JSON of all other fields.
    """
    user_id: str = Field(..., description="Opaque user identifier")
    max_amount_paise: int = Field(..., ge=1, description="Budget ceiling in paise")
    currency: str = Field(default="INR", pattern=r"^[A-Z]{3}$")
    allowed_categories: list[str] = Field(..., min_length=1, description="Allowed SKU categories")
    expires_at: datetime = Field(..., description="UTC expiry timestamp (ISO-8601)")
    nonce: str = Field(..., min_length=1, description="One-time random value (UUID recommended)")
    # Populated after signing
    signature: B64Str = Field(..., description="Ed25519 signature over canonical JSON, base64-encoded")

    @field_validator("allowed_categories")
    @classmethod
    def categories_not_empty(cls, v: list[str]) -> list[str]:
        if any(not c.strip() for c in v):
            raise ValueError("allowed_categories entries must not be blank")
        return [c.strip() for c in v]


class IntentMandateCreate(BaseModel):
    """Request body for registering an already-signed IntentMandate."""
    mandate: IntentMandate
    # Public key of the signer (base64-DER or base64-raw 32-byte Ed25519 pubkey)
    public_key_b64: str = Field(..., description="Base64-encoded Ed25519 public key (32 raw bytes)")

    @field_validator("public_key_b64")
    @classmethod
    def validate_pubkey(cls, v: str) -> str:
        raw = base64.b64decode(v)
        if len(raw) != 32:
            raise ValueError("Ed25519 public key must be 32 bytes")
        return v


# ---------------------------------------------------------------------------
# CartMandate
# ---------------------------------------------------------------------------

class CartMandate(BaseModel):
    """
    Per-cart spend request signed by the agent/merchant, chained to an IntentMandate.
    """
    parent_intent_id: str = Field(..., description="ID (nonce) of the parent IntentMandate")
    agent_id: str = Field(..., description="Opaque agent/merchant identifier")
    sku_list: list[SkuItem] = Field(..., min_length=1, description="Line items")
    total_amount_paise: int = Field(..., ge=1, description="Sum of qty*unit_price for all items")
    expires_at: datetime = Field(..., description="UTC expiry timestamp (ISO-8601)")
    nonce: str = Field(..., min_length=1, description="One-time random value (UUID recommended)")
    signature: B64Str = Field(..., description="Ed25519 signature over canonical JSON, base64-encoded")


class CartMandateVerifyRequest(BaseModel):
    """Request body for POST /api/mandates/verify-cart."""
    mandate: CartMandate
    # Public key of the cart signer (agent/merchant)
    public_key_b64: str = Field(..., description="Base64-encoded Ed25519 public key (32 raw bytes)")

    @field_validator("public_key_b64")
    @classmethod
    def validate_pubkey(cls, v: str) -> str:
        raw = base64.b64decode(v)
        if len(raw) != 32:
            raise ValueError("Ed25519 public key must be 32 bytes")
        return v


# ---------------------------------------------------------------------------
# Response models
# ---------------------------------------------------------------------------

class IntentMandateResponse(BaseModel):
    intent_id: str
    message: str


class CartVerifyResponse(BaseModel):
    verified: bool
    reason: str | None = None
    intent_id: str | None = None