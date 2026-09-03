"""
app/models/audit.py
=====================
Append-only audit ledger — one row per meaningful pipeline checkpoint across
Phases 2-5 (mandate verification, policy evaluation, settlement, and
failure-recovery diversion).

There are intentionally no update/delete code paths anywhere in this codebase
for AuditEvent: no ORM update helpers, no API routes. Immutability is
enforced by omission, not by a DB trigger — see app/api/audit.py, which
exposes only a GET route.
"""
from __future__ import annotations

import enum
from datetime import datetime, timezone

from sqlalchemy import JSON, DateTime, Enum, Index, Integer, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


def _utcnow() -> datetime:
    return datetime.now(tz=timezone.utc)


class AuditEventType(str, enum.Enum):
    INTENT_VERIFIED = "INTENT_VERIFIED"
    CART_VERIFIED = "CART_VERIFIED"
    BUDGET_PASSED = "BUDGET_PASSED"
    POLICY_PASSED = "POLICY_PASSED"
    ORDER_CREATED = "ORDER_CREATED"
    SETTLED = "SETTLED"
    FAILURE_DIVERTED = "FAILURE_DIVERTED"


class AuditEvent(Base):
    """
    One immutable row per pipeline checkpoint.

    intent_id   — the top-level flow key; every event in one AP2 transaction
                  flow (intent registration -> cart -> settlement/failure)
                  shares the same intent_id, which is what GET /api/audit/{id}
                  queries on.
    mandate_id  — the specific mandate nonce this event pertains to (the
                  intent's own nonce for INTENT_VERIFIED, otherwise the
                  cart's nonce).
    payload_snapshot — a JSONB blob capturing whatever was true at that
                  checkpoint (amounts, SKUs, order IDs, alternatives, ...).
    """
    __tablename__ = "audit_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    event_type: Mapped[AuditEventType] = mapped_column(
        Enum(AuditEventType, name="audit_event_type"), nullable=False, index=True
    )
    intent_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    mandate_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    agent_id: Mapped[str | None] = mapped_column(String(256), nullable=True)
    payload_snapshot: Mapped[dict | None] = mapped_column(
        JSONB().with_variant(JSON(), "sqlite"), nullable=True
    )
    timestamp: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False, index=True
    )

    __table_args__ = (
        Index("ix_audit_intent_timestamp", "intent_id", "timestamp"),
    )

    def __repr__(self) -> str:
        return (
            f"<AuditEvent id={self.id} type={self.event_type} "
            f"intent={self.intent_id!r} mandate={self.mandate_id!r}>"
        )
