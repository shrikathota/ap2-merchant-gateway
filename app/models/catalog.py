"""
SQLAlchemy ORM models for the product catalog and policy audit log.

Product          — live catalog entry (sku, unit_price_paise, stock_qty, category)
PolicyEvaluation — immutable audit record written for every transact attempt
"""
from __future__ import annotations

import enum
from datetime import datetime, timezone

from sqlalchemy import (
    BigInteger,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _utcnow() -> datetime:
    return datetime.now(tz=timezone.utc)


# ---------------------------------------------------------------------------
# Product catalog
# ---------------------------------------------------------------------------

class Product(Base):
    """
    Live product catalog entry.  One row per SKU.

    unit_price_paise  — authoritative price; compared against CartMandate line items
    stock_qty         — live inventory; must be >= qty requested in cart
    category          — must be in IntentMandate.allowed_categories
    """
    __tablename__ = "products"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    sku: Mapped[str] = mapped_column(String(128), unique=True, nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(256), nullable=False)
    category: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    unit_price_paise: Mapped[int] = mapped_column(BigInteger, nullable=False)
    stock_qty: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="INR")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=_utcnow,
        nullable=False,
    )

    # back-ref from PolicyEvaluation
    evaluations: Mapped[list[PolicyEvaluation]] = relationship(
        "PolicyEvaluation", back_populates="product", lazy="noload"
    )

    def __repr__(self) -> str:
        return f"<Product sku={self.sku!r} price={self.unit_price_paise} stock={self.stock_qty}>"


# ---------------------------------------------------------------------------
# Policy evaluation outcome enum
# ---------------------------------------------------------------------------

class PolicyOutcome(str, enum.Enum):
    APPROVED = "APPROVED"
    MANDATE_EXPIRED = "MANDATE_EXPIRED"
    INSUFFICIENT_INVENTORY = "INSUFFICIENT_INVENTORY"
    PRICE_DRIFT = "PRICE_DRIFT"
    CATEGORY_VIOLATION = "CATEGORY_VIOLATION"
    CURRENCY_MISMATCH = "CURRENCY_MISMATCH"
    MANDATE_INVALID = "MANDATE_INVALID"   # cryptographic / structural failure


# ---------------------------------------------------------------------------
# Policy evaluation audit log
# ---------------------------------------------------------------------------

class PolicyEvaluation(Base):
    """
    Immutable audit row written for every POST /api/transact attempt.

    One row per SKU that caused the evaluation to fail (or one "APPROVED" row
    when the whole cart passes).  This gives auditors full line-item granularity.
    """
    __tablename__ = "policy_evaluations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    cart_nonce: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    intent_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    agent_id: Mapped[str] = mapped_column(String(256), nullable=False)

    # Which SKU triggered the failure (null for cart-level failures)
    sku: Mapped[str | None] = mapped_column(String(128), nullable=True)
    product_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("products.id", ondelete="SET NULL"), nullable=True
    )
    product: Mapped[Product | None] = relationship("Product", back_populates="evaluations")

    outcome: Mapped[PolicyOutcome] = mapped_column(
        Enum(PolicyOutcome, name="policy_outcome"), nullable=False, index=True
    )
    reason_detail: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Snapshot of what the mandate contained vs what the catalog had
    mandate_unit_price_paise: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    catalog_unit_price_paise: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    requested_qty: Mapped[int | None] = mapped_column(Integer, nullable=True)
    available_qty: Mapped[int | None] = mapped_column(Integer, nullable=True)

    evaluated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )

    __table_args__ = (
        Index("ix_pe_cart_nonce_outcome", "cart_nonce", "outcome"),
    )

    def __repr__(self) -> str:
        return f"<PolicyEvaluation cart={self.cart_nonce!r} outcome={self.outcome} sku={self.sku!r}>"