"""Read-only endpoints that exist for the frontend's reliability views
(master plan Phase 6 screens 5 and 6: audit trail, exception queue) plus a
demo-user list. Nothing here decides or changes anything except the one
human action a queue needs: resolving an exception, which records who/why
and never touches an intent by itself.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from backend.models.db import get_session
from backend.models.entities import AuditEvent, ExceptionRecord, ExceptionStatus, User
from backend.services import audit_service, exception_service

router = APIRouter(prefix="/api", tags=["ui"])


@router.get("/users")
def list_users(session: Session = Depends(get_session)) -> dict:
    users = session.execute(select(User).order_by(User.created_at)).scalars().all()
    return {
        "users": [{"user_id": u.id, "name": u.name, "status": u.status.value, "is_synthetic": u.is_synthetic} for u in users],
        "demo_notice": "ALL DATA IS SYNTHETIC. Payments run in Razorpay Test Mode only.",
    }


@router.get("/audit")
def audit_feed(
    user_id: str | None = Query(default=None),
    limit: int = Query(default=60, ge=1, le=300),
    session: Session = Depends(get_session),
) -> dict:
    """Newest first. With user_id: that user's rows plus system-level rows
    (webhooks, reconciliation, exceptions) which carry no user — the
    reliability story is told by both."""
    q = select(AuditEvent)
    if user_id:
        q = q.where(or_(AuditEvent.user_id == user_id, AuditEvent.user_id.is_(None)))
    rows = session.execute(q.order_by(AuditEvent.seq.desc()).limit(limit)).scalars().all()
    chain = audit_service.verify_chain(session)
    return {
        "chain": {"ok": chain.ok, "checked": chain.checked, "reason": chain.reason},
        "events": [
            {
                "seq": r.seq, "actor": r.actor.value, "action": r.action, "user_id": r.user_id,
                "intent_id": r.intent_id, "policy_result": r.policy_result, "provider_result": r.provider_result,
                "created_at": r.created_at.isoformat(), "entry_hash": r.entry_hash[:12],
            }
            for r in rows
        ],
    }


@router.get("/exceptions")
def list_exceptions(include_resolved: bool = False, session: Session = Depends(get_session)) -> dict:
    q = select(ExceptionRecord)
    if not include_resolved:
        q = q.where(ExceptionRecord.status == ExceptionStatus.OPEN)
    rows = session.execute(q.order_by(ExceptionRecord.created_at.desc()).limit(100)).scalars().all()
    return {
        "exceptions": [
            {
                "exception_id": r.id, "kind": r.kind.value, "intent_id": r.intent_id, "detail": r.detail,
                "status": r.status.value, "created_at": r.created_at.isoformat(),
                "resolved_at": r.resolved_at.isoformat() if r.resolved_at else None,
            }
            for r in rows
        ]
    }


class ResolveBody(BaseModel):
    note: str = Field(..., min_length=3, max_length=500)


@router.post("/exceptions/{exception_id}/resolve")
def resolve_exception(exception_id: str, body: ResolveBody, session: Session = Depends(get_session)) -> dict:
    try:
        rec = exception_service.resolve(session, exception_id, note=body.note.strip())
    except LookupError:
        raise HTTPException(status_code=404, detail="unknown exception")
    session.commit()
    return {"exception_id": rec.id, "status": rec.status.value, "resolved_at": rec.resolved_at.isoformat()}
