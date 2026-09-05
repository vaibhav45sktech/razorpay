"""ExceptionRecord — the honest answer to "we don't know".

PRD s6 / s10: when the system cannot determine the truth (unverifiable
webhook, unknown order, payment status still indeterminate after the
reconciliation window, amount mismatch), it opens an exception for a human
and stops. It never guesses, and nothing here auto-resolves anything.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.models.entities import AuditActor, ExceptionKind, ExceptionRecord, ExceptionStatus
from backend.services import audit_service

logger = logging.getLogger("campuspool.exceptions")


def open(session: Session, *, kind: ExceptionKind, detail: dict[str, Any], intent_id: str | None = None,
         user_id: str | None = None) -> ExceptionRecord:  # noqa: A001 - "open" reads right at call sites
    rec = ExceptionRecord(kind=kind, intent_id=intent_id, detail=detail)
    session.add(rec)
    session.flush()
    audit_service.write(
        session, actor=AuditActor.SYSTEM, action=f"exception_opened:{kind.value}", user_id=user_id,
        intent_id=intent_id, inputs={"exception_id": rec.id, **{k: str(v)[:200] for k, v in detail.items()}},
    )
    logger.warning("EXCEPTION opened %s kind=%s intent=%s", rec.id, kind.value, intent_id)
    return rec


def list_open(session: Session, *, limit: int = 100) -> list[ExceptionRecord]:
    return list(
        session.execute(
            select(ExceptionRecord).where(ExceptionRecord.status == ExceptionStatus.OPEN)
            .order_by(ExceptionRecord.created_at.desc()).limit(limit)
        ).scalars().all()
    )


def resolve(session: Session, exception_id: str, *, note: str) -> ExceptionRecord:
    """A HUMAN closes an exception. Records who/why in the audit trail; never
    changes an intent by itself - that is a separate, deliberate action."""
    rec = session.get(ExceptionRecord, exception_id)
    if rec is None:
        raise LookupError(exception_id)
    rec.status = ExceptionStatus.RESOLVED
    rec.resolved_at = datetime.now(timezone.utc)
    rec.detail = {**(rec.detail or {}), "resolution_note": note}
    session.flush()
    audit_service.write(session, actor=AuditActor.USER, action="exception_resolved", intent_id=rec.intent_id,
                        inputs={"exception_id": rec.id, "note": note})
    return rec
