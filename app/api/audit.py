"""
app/api/audit.py
==================
Read-only API for the append-only audit ledger.

GET /api/audit/latest        — intent_id of the most recently active flow
GET /api/audit/{intent_id}   — full ordered event chain for a flow

There is deliberately no POST/PATCH/DELETE route here (or anywhere else in
the app) touching audit_events — the ledger is written only from inside the
Phase 2-5 pipeline (app/api/mandates.py, app/api/transact.py). Any attempt to
mutate an event via the API hits an unregistered route: FastAPI returns 405
if the path matches GET /api/audit/{intent_id} with a different method, or
404 otherwise.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db
from app.schemas.audit import AuditChainResponse, AuditEventResponse, LatestFlowResponse
from app.services.audit import AuditService

router = APIRouter(prefix="/api/audit", tags=["audit"])
logger = logging.getLogger(__name__)

_audit_svc = AuditService()


@router.get(
    "/latest",
    response_model=LatestFlowResponse,
    summary="intent_id of the most recently active transaction flow",
)
async def get_latest_flow(db: AsyncSession = Depends(get_db)) -> LatestFlowResponse:
    intent_id = await _audit_svc.get_latest_intent_id(db)
    return LatestFlowResponse(intent_id=intent_id)


@router.get(
    "/{intent_id}",
    response_model=AuditChainResponse,
    summary="Full ordered audit event chain for one transaction flow",
)
async def get_audit_chain(intent_id: str, db: AsyncSession = Depends(get_db)) -> AuditChainResponse:
    events = await _audit_svc.get_chain(db, intent_id)
    return AuditChainResponse(
        intent_id=intent_id,
        events=[AuditEventResponse.model_validate(e) for e in events],
    )
