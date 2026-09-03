"""
app/services/catalog.py
========================
Database-backed catalog service and policy audit writer.

CatalogService
  fetch_catalog(session, skus) -> list[CatalogItem]
      Load live product rows for the given SKU list and return them as
      CatalogItem Pydantic objects so the policy engine can consume them
      without touching SQLAlchemy.

  write_evaluation(session, *, cart, intent_id, result) -> PolicyEvaluation
      Persist a PolicyEvaluation audit row (one row per transact call).
"""
from __future__ import annotations

import json
import logging
from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.catalog import PolicyEvaluation, PolicyOutcome, Product
from app.schemas.mandates import CartMandate
from app.schemas.transact import AlternativeProduct, CatalogItem
from app.services.policy_engine import PolicyResult

logger = logging.getLogger(__name__)


class CatalogService:
    """Thin async repository layer for the product catalog and audit log."""

    # ------------------------------------------------------------------
    # Catalog reads
    # ------------------------------------------------------------------

    async def fetch_catalog(
        self,
        session: AsyncSession,
        skus: Sequence[str],
    ) -> list[CatalogItem]:
        """
        Load Product rows for the requested SKUs and convert them to
        CatalogItem schemas for the policy engine.

        SKUs that do not exist in the DB are silently omitted — the policy
        engine treats missing SKUs as PRICE_DRIFT (unknown price = can't confirm).
        """
        if not skus:
            return []

        stmt = select(Product).where(Product.sku.in_(skus))
        result = await session.execute(stmt)
        rows: list[Product] = list(result.scalars().all())

        items = [
            CatalogItem(
                sku=row.sku,
                unit_price_paise=row.unit_price_paise,
                stock_qty=row.stock_qty,
                category=row.category,
                currency=row.currency,
            )
            for row in rows
        ]
        logger.debug("Fetched %d/%d catalog items from DB", len(items), len(skus))
        return items

    # ------------------------------------------------------------------
    # Audit log writes
    # ------------------------------------------------------------------

    async def write_evaluation(
        self,
        session: AsyncSession,
        *,
        cart: CartMandate,
        intent_id: str | None,
        result: PolicyResult,
    ) -> PolicyEvaluation:
        """
        Persist a PolicyEvaluation audit record and flush it to the DB.

        The session is NOT committed here — the caller owns the transaction
        boundary (FastAPI dependency injects a session that auto-commits on
        successful response).
        """
        eval_row = PolicyEvaluation(
            cart_nonce=cart.nonce,
            intent_id=intent_id,
            agent_id=cart.agent_id,
            sku=result.offending_sku,
            outcome=result.outcome,
            reason_detail=result.detail or None,
            mandate_unit_price_paise=result.mandate_unit_price_paise,
            catalog_unit_price_paise=result.catalog_unit_price_paise,
            requested_qty=result.requested_qty,
            available_qty=result.available_qty,
        )
        session.add(eval_row)
        await session.flush()  # assigns PK without committing
        logger.info(
            "PolicyEvaluation id=%d cart=%r outcome=%s sku=%r",
            eval_row.id,
            cart.nonce,
            result.outcome,
            result.offending_sku,
        )
        return eval_row

    async def write_failure_diverted(
        self,
        session: AsyncSession,
        *,
        cart: CartMandate,
        intent_id: str | None,
        failed_sku: str,
        outcome: PolicyOutcome,
        detail: str,
        alternatives: list[AlternativeProduct],
    ) -> PolicyEvaluation:
        """
        Persist a FAILURE_DIVERTED audit record: a recoverable failure (
        INSUFFICIENT_INVENTORY / PRICE_DRIFT) that was diverted to the
        alternative-recovery engine instead of failing raw. The original
        failure outcome and the recovery payload (alternatives) are recorded
        for auditors.
        """
        recovery_payload = {
            "status": "FAILED",
            "reason": outcome.value,
            "failed_sku": failed_sku,
            "alternatives": [alt.model_dump() for alt in alternatives],
            "requires_new_mandate": True,
        }
        eval_row = PolicyEvaluation(
            cart_nonce=cart.nonce,
            intent_id=intent_id,
            agent_id=cart.agent_id,
            sku=failed_sku,
            outcome=PolicyOutcome.FAILURE_DIVERTED,
            reason_detail=detail,
            recovery_payload_json=json.dumps(recovery_payload),
        )
        session.add(eval_row)
        await session.flush()
        logger.info(
            "FAILURE_DIVERTED id=%d cart=%r original_outcome=%s sku=%r alternatives=%d",
            eval_row.id,
            cart.nonce,
            outcome,
            failed_sku,
            len(alternatives),
        )
        return eval_row