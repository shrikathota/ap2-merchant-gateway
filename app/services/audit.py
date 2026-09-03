"""
app/services/audit.py
=======================
Append-only audit ledger service.

AuditService.write_event(...)         — insert one immutable checkpoint row
AuditService.get_chain(session, id)   — full ordered event chain for a flow
AuditService.get_latest_intent_id(session) — intent_id of the most recent flow
"""
from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.audit import AuditEvent, AuditEventType

logger = logging.getLogger(__name__)


class AuditService:
    """Thin async repository layer for the append-only audit ledger."""

    async def write_event(
        self,
        session: AsyncSession,
        *,
        event_type: AuditEventType,
        intent_id: str,
        mandate_id: str | None = None,
        agent_id: str | None = None,
        payload_snapshot: dict | None = None,
    ) -> AuditEvent:
        event = AuditEvent(
            event_type=event_type,
            intent_id=intent_id,
            mandate_id=mandate_id,
            agent_id=agent_id,
            payload_snapshot=payload_snapshot,
        )
        session.add(event)
        await session.flush()
        logger.info(
            "AuditEvent id=%d type=%s intent=%r mandate=%r",
            event.id, event_type, intent_id, mandate_id,
        )
        return event

    async def get_chain(self, session: AsyncSession, intent_id: str) -> list[AuditEvent]:
        stmt = (
            select(AuditEvent)
            .where(AuditEvent.intent_id == intent_id)
            .order_by(AuditEvent.timestamp.asc(), AuditEvent.id.asc())
        )
        result = await session.execute(stmt)
        return list(result.scalars().all())

    async def get_latest_intent_id(self, session: AsyncSession) -> str | None:
        stmt = (
            select(AuditEvent.intent_id)
            .order_by(AuditEvent.timestamp.desc(), AuditEvent.id.desc())
            .limit(1)
        )
        result = await session.execute(stmt)
        return result.scalar_one_or_none()
