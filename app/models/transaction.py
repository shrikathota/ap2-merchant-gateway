"""
app/models/transaction.py
==========================
Transaction ORM model — one row per Razorpay order created from a CartMandate.

Lifecycle:
  PENDING_PAYMENT  → order created in Razorpay, awaiting capture
  SETTLED          → payment confirmed / captured successfully
  FAILED           → payment confirmation failed; stock rollback applied
  ROLLED_BACK      → order creation failed; stock rollback applied (guard for Razorpay errors)
"""
from __future__ import annotations

import enum
from datetime import datetime, timezone

from sqlalchemy import BigInteger, DateTime, Enum, Index, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


def _utcnow() -> datetime:
    return datetime.now(tz=timezone.utc)


class TransactionStatus(str, enum.Enum):
    PENDING_PAYMENT = "PENDING_PAYMENT"
    SETTLED = "SETTLED"
    FAILED = "FAILED"
    ROLLED_BACK = "ROLLED_BACK"


class Transaction(Base):
    """
    Persists the full lifecycle of a Razorpay order created from an APPROVED CartMandate.

    razorpay_order_id  — primary external key used for lookup + confirm-payment
    razorpay_payment_id — populated after successful payment capture
    """
    __tablename__ = "transactions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    # Mandate identifiers
    cart_nonce: Mapped[str] = mapped_column(String(128), nullable=False, unique=True, index=True)
    intent_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    agent_id: Mapped[str] = mapped_column(String(256), nullable=False)
    cart_signature: Mapped[str] = mapped_column(Text, nullable=False)

    # Razorpay fields
    razorpay_order_id: Mapped[str] = mapped_column(
        String(128), nullable=False, unique=True, index=True
    )
    razorpay_payment_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    razorpay_order_receipt: Mapped[str | None] = mapped_column(String(128), nullable=True)

    # Financial fields
    amount_paise: Mapped[int] = mapped_column(BigInteger, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="INR")

    # Status lifecycle
    status: Mapped[TransactionStatus] = mapped_column(
        Enum(TransactionStatus, name="transaction_status"),
        nullable=False,
        default=TransactionStatus.PENDING_PAYMENT,
        index=True,
    )
    failure_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=_utcnow,
        nullable=False,
    )

    __table_args__ = (
        Index("ix_txn_status_created", "status", "created_at"),
    )

    def __repr__(self) -> str:
        return (
            f"<Transaction id={self.id} order={self.razorpay_order_id!r}"
            f" status={self.status} amount={self.amount_paise}>"
        )