"""
POST /api/transact
==================
Pre-settlement transaction gate.

Pipeline
--------
1. Parse and cryptographically verify the CartMandate (Phase 2 MandateVerifier).
2. Fetch the parent IntentMandate from Redis (already done inside MandateVerifier).
3. Fetch live catalog rows for every SKU from the DB.
4. Run PolicyEngine.evaluate() — short-circuits on first policy failure.
5. Write a PolicyEvaluation audit row to the DB regardless of outcome.
6. Return APPROVED or DENIED with a structured reason.

Phase 4 (Razorpay settlement) will be wired in after the APPROVED branch.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db
from app.models.catalog import PolicyOutcome
from app.schemas.mandates import CartMandate
from app.schemas.transact import TransactRequest, TransactResponse
from app.services.catalog import CatalogService
from app.services.mandates import MandateVerifier, VerificationError, load_intent_mandate
from app.services.policy_engine import PolicyEngine, PolicyResult

router = APIRouter(prefix="/api", tags=["transact"])
logger = logging.getLogger(__name__)

_verifier = MandateVerifier()
_policy = PolicyEngine()
_catalog_svc = CatalogService()


@router.post(
    "/transact",
    response_model=TransactResponse,
    summary="Verify mandate chain + run policy engine (pre-settlement gate)",
)
async def transact(
    body: TransactRequest,
    db: AsyncSession = Depends(get_db),
) -> TransactResponse:
    """
    Full pre-settlement pipeline:

    1. Verify cart mandate (Ed25519 signature, expiry, budget, category, nonce)
    2. Run policy engine (stock, price drift, currency, category, TTL)
    3. Write PolicyEvaluation audit row
    4. Return {status: APPROVED, next: proceed_to_settlement} or {status: DENIED, reason: ...}
    """
    # ------------------------------------------------------------------ #
    # Parse CartMandate from the raw dict                                  #
    # ------------------------------------------------------------------ #
    try:
        cart = CartMandate.model_validate(body.cart_mandate_json)
    except Exception as exc:
        logger.warning("Invalid CartMandate payload: %s", exc)
        return TransactResponse(
            status="DENIED",
            reason="MANDATE_INVALID",
            reason_detail=f"CartMandate parse error: {exc}",
        )

    # ------------------------------------------------------------------ #
    # Phase 2 — Cryptographic verification                                #
    # ------------------------------------------------------------------ #
    intent_id: str | None = None
    try:
        intent_id = await _verifier.verify_cart(cart, body.agent_public_key_b64)
    except VerificationError as exc:
        logger.warning(
            "Mandate verification failed cart=%r reason=%s", cart.nonce, exc.reason
        )
        # Write audit row before returning
        mandate_result = PolicyResult(
            passed=False,
            outcome=PolicyOutcome.MANDATE_INVALID,
            detail=f"{exc.reason}: {exc.detail}",
        )
        await _catalog_svc.write_evaluation(
            db, cart=cart, intent_id=intent_id, result=mandate_result
        )
        return TransactResponse(
            status="DENIED",
            reason=exc.reason,
            reason_detail=exc.detail,
            cart_nonce=cart.nonce,
        )

    # ------------------------------------------------------------------ #
    # Fetch parent IntentMandate from Redis                                #
    # (already validated inside verify_cart; we need it for the engine)  #
    # ------------------------------------------------------------------ #
    intent = await load_intent_mandate(cart.parent_intent_id)
    if intent is None:
        # Should not happen if verify_cart passed, but be safe
        mandate_result = PolicyResult(
            passed=False,
            outcome=PolicyOutcome.MANDATE_INVALID,
            detail="Intent mandate disappeared from Redis after verification",
        )
        await _catalog_svc.write_evaluation(
            db, cart=cart, intent_id=intent_id, result=mandate_result
        )
        return TransactResponse(
            status="DENIED",
            reason="INTENT_NOT_FOUND",
            cart_nonce=cart.nonce,
        )

    # ------------------------------------------------------------------ #
    # Fetch live catalog from DB                                           #
    # ------------------------------------------------------------------ #
    skus = [item.sku for item in cart.sku_list]
    catalog_items = await _catalog_svc.fetch_catalog(db, skus)

    # ------------------------------------------------------------------ #
    # Phase 3 — Policy engine                                             #
    # ------------------------------------------------------------------ #
    policy_result = _policy.evaluate(cart, catalog_items, intent)

    # ------------------------------------------------------------------ #
    # Audit log (always written, regardless of outcome)                   #
    # ------------------------------------------------------------------ #
    await _catalog_svc.write_evaluation(
        db, cart=cart, intent_id=intent_id, result=policy_result
    )

    # ------------------------------------------------------------------ #
    # Response                                                            #
    # ------------------------------------------------------------------ #
    if policy_result.passed:
        logger.info(
            "APPROVED cart=%r intent=%r agent=%r total=%d",
            cart.nonce,
            intent_id,
            cart.agent_id,
            cart.total_amount_paise,
        )
        return TransactResponse(
            status="APPROVED",
            next="proceed_to_settlement",
            cart_nonce=cart.nonce,
            intent_id=intent_id,
        )

    logger.warning(
        "DENIED cart=%r reason=%s sku=%r detail=%r",
        cart.nonce,
        policy_result.outcome,
        policy_result.offending_sku,
        policy_result.detail,
    )
    return TransactResponse(
        status="DENIED",
        reason=policy_result.outcome.value,
        reason_detail=policy_result.detail,
        cart_nonce=cart.nonce,
        intent_id=intent_id,
    )