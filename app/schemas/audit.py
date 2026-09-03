"""Pydantic v2 schemas for the audit ledger API."""
from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel


class AuditEventResponse(BaseModel):
    id: int
    event_type: str
    intent_id: str
    mandate_id: str | None = None
    agent_id: str | None = None
    payload_snapshot: dict | None = None
    timestamp: datetime

    model_config = {"from_attributes": True}


class AuditChainResponse(BaseModel):
    intent_id: str
    events: list[AuditEventResponse]


class LatestFlowResponse(BaseModel):
    intent_id: str | None = None
