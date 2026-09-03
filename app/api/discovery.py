"""
app/api/discovery.py
======================
GET /.well-known/agent-commerce.json

A static, unauthenticated discovery document describing how an external AI
buyer agent talks to this merchant: which endpoints exist, the mandate
signing scheme (AP2: Ed25519 over canonical sorted-key JSON), and the
lifecycle an agent should expect (including the Phase 5 alternative-recovery
path on FAILED responses).
"""
from __future__ import annotations

from fastapi import APIRouter

from app.core.config import get_settings

router = APIRouter(tags=["discovery"])

settings = get_settings()


@router.get(
    "/.well-known/agent-commerce.json",
    summary="AP2 merchant discovery document for external buyer agents",
)
async def agent_commerce_manifest() -> dict:
    return {
        "protocol": "ap2",
        "protocol_version": "1.0",
        "merchant_name": settings.app_name,
        "currency": "INR",
        "endpoints": {
            "catalog": "/api/catalog",
            "register_intent": "/api/mandates/intent",
            "verify_cart": "/api/mandates/verify-cart",
            "transact": "/api/transact",
            "confirm_payment": "/api/transact/{order_id}/confirm-payment",
            "transaction_status": "/api/transact/{order_id}",
            "list_transactions": "/api/transact",
            "audit_chain": "/api/audit/{intent_id}",
            "audit_latest": "/api/audit/latest",
        },
        "mandate_scheme": {
            "signature_algorithm": "Ed25519",
            "canonicalization": "JSON with sorted keys, no whitespace, 'signature' field excluded before signing",
            "intent_mandate_fields": [
                "user_id", "max_amount_paise", "currency", "allowed_categories",
                "expires_at", "nonce", "signature",
            ],
            "cart_mandate_fields": [
                "parent_intent_id", "agent_id", "sku_list", "total_amount_paise",
                "expires_at", "nonce", "signature",
            ],
        },
        "transact_lifecycle": {
            "success": {
                "status": "APPROVED",
                "next": "POST the returned razorpay_order_id to /api/transact/{order_id}/confirm-payment",
            },
            "recoverable_failure": {
                "status": "FAILED",
                "reasons": ["INSUFFICIENT_INVENTORY", "PRICE_DRIFT"],
                "next": (
                    "Response includes failed_sku, alternatives (ranked in-stock substitutes), "
                    "and requires_new_mandate=true — sign and submit a new CartMandate for one "
                    "of the alternatives to retry."
                ),
            },
            "hard_denial": {
                "status": "DENIED",
                "reasons": ["MANDATE_EXPIRED", "MANDATE_INVALID", "CATEGORY_VIOLATION",
                            "CURRENCY_MISMATCH", "PAYMENT_GATEWAY_ERROR"],
            },
        },
    }
