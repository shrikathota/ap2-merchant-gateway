"""
app/services/alternative_finder.py
====================================
Finds in-stock substitute products when a cart line item fails on
INSUFFICIENT_INVENTORY or PRICE_DRIFT.

AlternativeFinder.find_alternatives(session, *, failed_sku, category, max_amount_paise)
    -> list[AlternativeProduct]

Query: same category, stock_qty > 0, price_paise <= max_amount_paise,
excluding the failed SKU itself, ranked by closest absolute price match to
the failed SKU's mandate price (or the cheapest in-budget items if no
reference price is available). Returns the top 3.
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.catalog import Product
from app.schemas.transact import AlternativeProduct


class AlternativeFinder:
    """Stateless catalog-backed substitute product finder."""

    async def find_alternatives(
        self,
        session: AsyncSession,
        *,
        failed_sku: str,
        category: str,
        max_amount_paise: int,
        reference_price_paise: int | None = None,
        limit: int = 3,
    ) -> list[AlternativeProduct]:
        """
        Return up to *limit* in-stock products from *category* that fit within
        *max_amount_paise*, ranked by closest price match to
        *reference_price_paise* (falls back to cheapest-first when no reference
        price is available).
        """
        stmt = select(Product).where(
            Product.category == category,
            Product.sku != failed_sku,
            Product.stock_qty > 0,
            Product.unit_price_paise <= max_amount_paise,
        )
        result = await session.execute(stmt)
        candidates: list[Product] = list(result.scalars().all())

        if reference_price_paise is not None:
            candidates.sort(key=lambda p: abs(p.unit_price_paise - reference_price_paise))
        else:
            candidates.sort(key=lambda p: p.unit_price_paise)

        return [
            AlternativeProduct(
                sku=p.sku,
                name=p.name,
                price_paise=p.unit_price_paise,
                stock_qty=p.stock_qty,
                similarity_reason=self._similarity_reason(p, category, reference_price_paise),
            )
            for p in candidates[:limit]
        ]

    @staticmethod
    def _similarity_reason(
        product: Product, category: str, reference_price_paise: int | None
    ) -> str:
        if reference_price_paise is None:
            return f"same category ({category})"
        delta = product.unit_price_paise - reference_price_paise
        if delta == 0:
            return f"same category ({category}), same price"
        direction = "cheaper" if delta < 0 else "more expensive"
        return f"same category ({category}), {abs(delta)} paise {direction}"
