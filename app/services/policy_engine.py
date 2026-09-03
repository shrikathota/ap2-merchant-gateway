"""
app/services/policy_engine.py
==============================
Deterministic pre-transaction policy guardrail engine.

PolicyEngine.evaluate(cart_mandate, catalog_snapshot, intent)
    -> PolicyResult

Checks (in order, short-circuit on first failure):
  1. MANDATE_EXPIRED        — now > cart.expires_at
  2. CURRENCY_MISMATCH      — cart currency != "INR"  (intent always carries INR)
  3. CATEGORY_VIOLATION     — any SKU category not in intent.allowed_categories
  4. PRICE_DRIFT            — mandate unit_price_paise != catalog unit_price_paise (exact)
  5. INSUFFICIENT_INVENTORY — qty requested > live stock_qty

On success: PolicyResult(passed=True)

The engine is a pure-function service — it takes fully-resolved domain objects
and has no I/O of its own.  The caller (the transact endpoint) is responsible
for DB reads, mandate verification, and writing the audit log.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

from app.models.catalog import PolicyOutcome
from app.schemas.mandates import CartMandate, IntentMandate, SkuItem
from app.schemas.transact import CatalogItem


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class PolicyResult:
    """
    Immutable result from a single PolicyEngine.evaluate() call.

    On success : passed=True, outcome=APPROVED, offending_sku=None
    On failure : passed=False, outcome=<reason>, offending_sku=<sku or None>,
                 detail=<human-readable explanation>
    """
    passed: bool
    outcome: PolicyOutcome = PolicyOutcome.APPROVED
    offending_sku: str | None = None
    detail: str = ""

    # Snapshot values for the audit row (only set on relevant failures)
    mandate_unit_price_paise: int | None = None
    catalog_unit_price_paise: int | None = None
    requested_qty: int | None = None
    available_qty: int | None = None


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------

class PolicyEngine:
    """
    Stateless policy evaluator.

    Usage::

        engine = PolicyEngine()
        result = engine.evaluate(cart_mandate, catalog_snapshot, intent_mandate)
    """

    def evaluate(
        self,
        cart: CartMandate,
        catalog: list[CatalogItem],
        intent: IntentMandate,
    ) -> PolicyResult:
        """
        Run all policy checks in priority order and short-circuit on first failure.

        Parameters
        ----------
        cart:
            The CartMandate that has ALREADY been cryptographically verified by
            MandateVerifier (Phase 2).  This method does NOT re-check the signature.
        catalog:
            Live product entries for every SKU in the cart (fetched from DB by caller).
        intent:
            The parent IntentMandate (fetched from Redis by caller).

        Returns
        -------
        PolicyResult
            ``passed=True`` if all checks pass, otherwise the first failing check.
        """
        now = datetime.now(tz=timezone.utc)

        # Build a fast lookup: sku -> CatalogItem
        catalog_map: dict[str, CatalogItem] = {item.sku: item for item in catalog}

        # ------------------------------------------------------------------ #
        # 1. MANDATE_EXPIRED                                                  #
        # ------------------------------------------------------------------ #
        cart_expires = cart.expires_at
        if cart_expires.tzinfo is None:
            cart_expires = cart_expires.replace(tzinfo=timezone.utc)
        if now > cart_expires:
            return PolicyResult(
                passed=False,
                outcome=PolicyOutcome.MANDATE_EXPIRED,
                detail=f"Cart mandate expired at {cart_expires.isoformat()}",
            )

        # ------------------------------------------------------------------ #
        # 2. CURRENCY_MISMATCH                                                #
        # ------------------------------------------------------------------ #
        # We check each SKU's catalog currency; all must be "INR".
        for item in cart.sku_list:
            cat_entry = catalog_map.get(item.sku)
            if cat_entry is not None and cat_entry.currency != "INR":
                return PolicyResult(
                    passed=False,
                    outcome=PolicyOutcome.CURRENCY_MISMATCH,
                    offending_sku=item.sku,
                    detail=(
                        f"SKU {item.sku!r} catalog currency is {cat_entry.currency!r},"
                        f" expected INR"
                    ),
                )

        # ------------------------------------------------------------------ #
        # 3. CATEGORY_VIOLATION                                               #
        # ------------------------------------------------------------------ #
        allowed = set(intent.allowed_categories)
        for item in cart.sku_list:
            if item.category not in allowed:
                return PolicyResult(
                    passed=False,
                    outcome=PolicyOutcome.CATEGORY_VIOLATION,
                    offending_sku=item.sku,
                    detail=(
                        f"SKU {item.sku!r} category {item.category!r} not in "
                        f"intent allowed_categories {sorted(allowed)}"
                    ),
                )

        # ------------------------------------------------------------------ #
        # 4. PRICE_DRIFT (exact match — zero tolerance)                       #
        # ------------------------------------------------------------------ #
        for item in cart.sku_list:
            cat_entry = catalog_map.get(item.sku)
            if cat_entry is None:
                # No catalog entry means we cannot confirm the price → treat as drift
                return PolicyResult(
                    passed=False,
                    outcome=PolicyOutcome.PRICE_DRIFT,
                    offending_sku=item.sku,
                    detail=f"SKU {item.sku!r} not found in catalog snapshot",
                    mandate_unit_price_paise=item.unit_price_paise,
                    catalog_unit_price_paise=None,
                )
            if item.unit_price_paise != cat_entry.unit_price_paise:
                return PolicyResult(
                    passed=False,
                    outcome=PolicyOutcome.PRICE_DRIFT,
                    offending_sku=item.sku,
                    detail=(
                        f"SKU {item.sku!r} price drift: mandate={item.unit_price_paise}"
                        f" catalog={cat_entry.unit_price_paise}"
                    ),
                    mandate_unit_price_paise=item.unit_price_paise,
                    catalog_unit_price_paise=cat_entry.unit_price_paise,
                )

        # ------------------------------------------------------------------ #
        # 5. INSUFFICIENT_INVENTORY                                           #
        # ------------------------------------------------------------------ #
        for item in cart.sku_list:
            cat_entry = catalog_map[item.sku]  # guaranteed present after check #4
            if item.qty > cat_entry.stock_qty:
                return PolicyResult(
                    passed=False,
                    outcome=PolicyOutcome.INSUFFICIENT_INVENTORY,
                    offending_sku=item.sku,
                    detail=(
                        f"SKU {item.sku!r}: requested qty={item.qty}"
                        f" > available stock={cat_entry.stock_qty}"
                    ),
                    requested_qty=item.qty,
                    available_qty=cat_entry.stock_qty,
                )

        # ------------------------------------------------------------------ #
        # All checks passed                                                   #
        # ------------------------------------------------------------------ #
        return PolicyResult(passed=True)