"""
API router for mandate registration and verification.

Routes
------
POST /api/mandates/intent       — register a pre-signed IntentMandate
POST /api/mandates/verify-cart  — verify a CartMandate against its parent intent
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from starlette import status as _status

# Starlette >= 0.46 renamed 422 to HTTP_422_UNPROCESSABLE_CONTENT
_422 = getattr(_status, "HTTP_422_UNPROCESSABLE_CONTENT", 422)

from app.db.session import get_db
from app.models.audit import AuditEventType
from app.schemas.mandates import (
    CartMandateVerifyRequest,
    CartVerifyResponse,
    IntentMandateCreate,
    IntentMandateResponse,
)
from app.services.audit import AuditService
from app.services.mandates import MandateVerifier, VerificationError

router = APIRouter(prefix="/api/mandates", tags=["mandates"])
logger = logging.getLogger(__name__)

_verifier = MandateVerifier()
_audit_svc = AuditService()


@router.post(
    "/intent",
    response_model=IntentMandateResponse,
    status_code=201,
    summary="Register a pre-signed IntentMandate",
)
async def register_intent(
    body: IntentMandateCreate,
    db: AsyncSession = Depends(get_db),
) -> IntentMandateResponse:
    """
    Accept a user-signed IntentMandate and store it in Redis.

    The caller must supply the Ed25519 public key (base64, 32 raw bytes)
    that was used to create the signature embedded in the mandate.
    """
    try:
        intent_id = await _verifier.register_intent(body.mandate, body.public_key_b64)
    except VerificationError as exc:
        raise HTTPException(
            status_code=_422,
            detail={"reason": exc.reason, "detail": exc.detail},
        ) from exc
    except Exception as exc:
        logger.exception("Unexpected error registering intent mandate")
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    await _audit_svc.write_event(
        db,
        event_type=AuditEventType.INTENT_VERIFIED,
        intent_id=intent_id,
        mandate_id=intent_id,
        payload_snapshot={
            "user_id": body.mandate.user_id,
            "max_amount_paise": body.mandate.max_amount_paise,
            "allowed_categories": body.mandate.allowed_categories,
            "expires_at": body.mandate.expires_at.isoformat(),
        },
    )

    return IntentMandateResponse(
        intent_id=intent_id,
        message=f"IntentMandate registered successfully (id={intent_id})",
    )


@router.post(
    "/verify-cart",
    response_model=CartVerifyResponse,
    summary="Verify a CartMandate against its parent IntentMandate",
)
async def verify_cart(body: CartMandateVerifyRequest) -> CartVerifyResponse:
    """
    Verify a CartMandate end-to-end:

    1. Ed25519 signature check
    2. Cart and Intent expiry
    3. Budget ceiling (total <= max_amount_paise)
    4. SKU category whitelist
    5. Nonce replay-protection (atomic Redis SET NX)

    Returns ``{"verified": true}`` on success or ``{"verified": false, "reason": "<CODE>"}``
    on failure.  Hard cryptographic failures return HTTP 422; policy failures
    are surfaced as HTTP 200 with ``verified: false`` so clients can distinguish
    them from request-level errors.
    """
    try:
        intent_id = await _verifier.verify_cart(body.mandate, body.public_key_b64)
        return CartVerifyResponse(verified=True, intent_id=intent_id)
    except VerificationError as exc:
        # Signature failures → 422 (client submitted provably bad data)
        if exc.reason in ("CART_SIGNATURE_INVALID", "INTENT_SIGNATURE_INVALID"):
            raise HTTPException(
                status_code=_422,
                detail={"reason": exc.reason, "detail": exc.detail},
            ) from exc
        # Policy failures → 200 verified:false so callers can handle gracefully
        return CartVerifyResponse(verified=False, reason=exc.reason)
    except Exception as exc:
        logger.exception("Unexpected error verifying cart mandate")
        raise HTTPException(status_code=500, detail=str(exc)) from exc