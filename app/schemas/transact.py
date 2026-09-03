"""
Pydantic v2 schemas for POST /api/transact.

TransactRequest   — cart mandate + agent public key
CatalogItem       — one entry in the catalog_snapshot the caller provides
TransactResponse  — final decision from the policy engine
"""
from __future__ import annotations

import base64
from typing import Annotated

from pydantic import BaseModel, Field, field_validator


class CatalogItem(BaseModel):
    """
    Caller-supplied snapshot of a single product from the live catalog.

    In Phase 3 the caller provides this; in Phase 5 we will fetch it ourselves
    from the DB.  For now we trust the caller to pass the current catalog state
    AND we cross-check it against the DB to detect tampered snapshots.
    """
    sku: str = Field(..., min_length=1)
    unit_price_paise: int = Field(..., ge=0)
    stock_qty: int = Field(..., ge=0)
    category: str = Field(..., min_length=1)
    currency: str = Field(default="INR", pattern=r"^[A-Z]{3}$")


class TransactRequest(BaseModel):
    """
    Request body for POST /api/transact.

    The caller must supply:
      - The signed CartMandate
      - The agent's Ed25519 public key (base64, 32 raw bytes)
      - A catalog snapshot (one CatalogItem per SKU in the cart)
      - The intent public key (the key that signed the IntentMandate)
    """
    cart_mandate_json: dict = Field(
        ..., description="CartMandate object (as JSON dict, including signature)"
    )
    agent_public_key_b64: str = Field(
        ..., description="Base64-encoded Ed25519 public key of the cart signer (32 raw bytes)"
    )
    intent_public_key_b64: str = Field(
        ..., description="Base64-encoded Ed25519 public key of the intent signer (32 raw bytes)"
    )
    catalog_snapshot: list[CatalogItem] = Field(
        default_factory=list,
        description="Optional catalog snapshot for cross-validation (Phase 5). Leave empty to rely on DB fetch.",
    )

    @field_validator("agent_public_key_b64", "intent_public_key_b64")
    @classmethod
    def validate_pubkey(cls, v: str) -> str:
        raw = base64.b64decode(v)
        if len(raw) != 32:
            raise ValueError("Ed25519 public key must be 32 bytes")
        return v


class TransactResponse(BaseModel):
    """Response from POST /api/transact."""
    status: str                       # "APPROVED" | "DENIED"
    reason: str | None = None         # machine-readable reason code on denial
    reason_detail: str | None = None  # human-readable detail
    next: str | None = None           # "proceed_to_settlement" on approval
    cart_nonce: str | None = None
    intent_id: str | None = None