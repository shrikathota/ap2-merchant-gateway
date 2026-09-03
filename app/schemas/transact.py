"""
Pydantic v2 schemas for POST /api/transact and related endpoints.

TransactRequest       — cart mandate + agent public key
CatalogItem           — one entry in the catalog_snapshot the caller provides
TransactResponse      — final decision (Phase 4: includes razorpay_order_id on APPROVED)
ConfirmPaymentRequest — body for POST /api/transact/{order_id}/confirm-payment
TransactionStatusResponse — body for GET /api/transact/{order_id}
"""
from __future__ import annotations

import base64
from datetime import datetime

from pydantic import BaseModel, Field, field_validator


class CatalogItem(BaseModel):
    """Caller-supplied snapshot of a single product (optional in Phase 3+; DB is authoritative)."""
    sku: str = Field(..., min_length=1)
    unit_price_paise: int = Field(..., ge=0)
    stock_qty: int = Field(..., ge=0)
    category: str = Field(..., min_length=1)
    currency: str = Field(default="INR", pattern=r"^[A-Z]{3}$")


class TransactRequest(BaseModel):
    """Request body for POST /api/transact."""
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
        description="Optional catalog snapshot for cross-validation. Leave empty to rely on DB fetch.",
    )

    @field_validator("agent_public_key_b64", "intent_public_key_b64")
    @classmethod
    def validate_pubkey(cls, v: str) -> str:
        raw = base64.b64decode(v)
        if len(raw) != 32:
            raise ValueError("Ed25519 public key must be 32 bytes")
        return v


class TransactResponse(BaseModel):
    """
    Response from POST /api/transact.

    On APPROVED, razorpay_order_id is populated with the real Razorpay order ID.
    Call POST /api/transact/{razorpay_order_id}/confirm-payment to settle.
    """
    status: str                          # "APPROVED" | "DENIED"
    reason: str | None = None            # machine-readable reason code on denial
    reason_detail: str | None = None     # human-readable detail
    next: str | None = None              # guidance string
    cart_nonce: str | None = None
    intent_id: str | None = None
    razorpay_order_id: str | None = None # populated on APPROVED


class ConfirmPaymentRequest(BaseModel):
    """
    Request body for POST /api/transact/{order_id}/confirm-payment.

    payment_id — Razorpay payment ID (pay_XXXX) from test checkout or Razorpay Dashboard.
                 In automated tests a mock is used; for real test-mode use an ID from
                 the Razorpay test checkout flow.
    """
    payment_id: str = Field(
        ...,
        min_length=4,
        description="Razorpay payment ID (pay_XXXX) from test checkout",
    )


class TransactionStatusResponse(BaseModel):
    """Response for GET /api/transact/{order_id}."""
    razorpay_order_id: str
    status: str                          # TransactionStatus value
    amount_paise: int
    currency: str
    cart_nonce: str
    intent_id: str
    agent_id: str
    razorpay_payment_id: str | None = None
    failure_reason: str | None = None
    created_at: datetime
    updated_at: datetime