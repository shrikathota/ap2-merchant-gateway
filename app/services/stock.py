"""
app/services/stock.py
======================
Atomic stock management with race-condition protection.

The key primitive is ``decrement_stock_atomic``:

    UPDATE products
    SET    stock_qty = stock_qty - :qty,
           updated_at = now()
    WHERE  sku = :sku
      AND  stock_qty >= :qty          -- guard: only decrement if sufficient
    RETURNING id, stock_qty

The WHERE ``stock_qty >= qty`` guard turns "read-check-write" into a single
atomic statement, eliminating the TOCTOU race between the policy engine's
pre-check and the actual decrement under concurrent load.

If the UPDATE matches 0 rows the decrement was not applied (lost race or
stock genuinely exhausted) and we raise ``StockUnavailable``.

``rollback_stock`` increments stock_qty back by the exact qty that was
decremented — used when Razorpay order creation or payment confirmation fails.
"""
from __future__ import annotations

import logging

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)


class StockUnavailable(Exception):
    """Raised when atomic stock decrement matches 0 rows (race-condition loss or empty stock)."""

    def __init__(self, sku: str, qty: int) -> None:
        self.sku = sku
        self.qty = qty
        super().__init__(f"Stock unavailable for sku={sku!r} qty={qty}")


async def decrement_stock_atomic(
    session: AsyncSession,
    sku: str,
    qty: int,
) -> int:
    """
    Atomically decrement stock for a single SKU.

    Executes::

        UPDATE products
        SET    stock_qty = stock_qty - :qty,
               updated_at = :now
        WHERE  sku = :sku AND stock_qty >= :qty

    Returns the new stock_qty value after decrement.
    Raises StockUnavailable if the update matched 0 rows.
    """
    stmt = text(
        """
        UPDATE products
        SET    stock_qty  = stock_qty - :qty
        WHERE  sku        = :sku
          AND  stock_qty >= :qty
        """
    )
    result = await session.execute(stmt, {"sku": sku, "qty": qty})
    rows_affected: int = result.rowcount

    if rows_affected == 0:
        logger.warning(
            "Atomic stock decrement FAILED (race or empty) sku=%r qty=%d", sku, qty
        )
        raise StockUnavailable(sku=sku, qty=qty)

    logger.info("Atomic stock decrement OK sku=%r qty=%d rows=%d", sku, qty, rows_affected)
    return rows_affected


async def rollback_stock(
    session: AsyncSession,
    sku: str,
    qty: int,
) -> None:
    """
    Restore *qty* units back to stock for a given SKU.

    Called when:
      - Razorpay order creation fails after stock was decremented.
      - Payment confirmation fails (FAILED → rollback).
    """
    stmt = text(
        """
        UPDATE products
        SET    stock_qty = stock_qty + :qty
        WHERE  sku       = :sku
        """
    )
    await session.execute(stmt, {"sku": sku, "qty": qty})
    logger.info("Stock rollback applied sku=%r qty=%d", sku, qty)


async def decrement_all_skus(
    session: AsyncSession,
    sku_qty_pairs: list[tuple[str, int]],
) -> None:
    """
    Atomically decrement stock for every (sku, qty) pair in order.

    On any failure, rolls back ALL previously decremented SKUs before raising.
    """
    decremented: list[tuple[str, int]] = []
    try:
        for sku, qty in sku_qty_pairs:
            await decrement_stock_atomic(session, sku, qty)
            decremented.append((sku, qty))
    except StockUnavailable:
        # Roll back all successfully decremented SKUs so far
        for sku, qty in decremented:
            await rollback_stock(session, sku, qty)
        raise


async def rollback_all_skus(
    session: AsyncSession,
    sku_qty_pairs: list[tuple[str, int]],
) -> None:
    """Roll back stock for all (sku, qty) pairs (best-effort, logs errors)."""
    for sku, qty in sku_qty_pairs:
        try:
            await rollback_stock(session, sku, qty)
        except Exception as exc:
            logger.error("Stock rollback error sku=%r qty=%d: %s", sku, qty, exc)