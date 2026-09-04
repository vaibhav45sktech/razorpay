"""Read-only access to the passive watcher's suggestions.

Two routes, both deliberately dull: list them, dismiss one. There is no route
to act on a suggestion — acting is always the user's own structured request
(chat, or the intent endpoints), never a click on an AI's nudge.
"""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.models.db import get_session
from backend.models.entities import Suggestion, User

router = APIRouter(prefix="/api", tags=["suggestions"])


def suggestion_view(s: Suggestion) -> dict:
    return {
        "suggestion_id": s.id, "kind": s.kind, "text": s.text, "facts": s.facts,
        "phrased_by": s.phrased_by, "source_event_id": s.source_event_id,
        "created_at": s.created_at.isoformat(), "dismissed": s.dismissed_at is not None,
        "advisory_notice": "AI suggestion (demo). Informational only — it cannot act on your account.",
    }


def list_active(session: Session, user_id: str, limit: int = 20) -> list[dict]:
    rows = session.execute(
        select(Suggestion)
        .where(Suggestion.user_id == user_id, Suggestion.dismissed_at.is_(None))
        .order_by(Suggestion.created_at.desc())
        .limit(limit)
    ).scalars().all()
    return [suggestion_view(s) for s in rows]


@router.get("/suggestions/{user_id}")
def get_suggestions(user_id: str, include_dismissed: bool = False, session: Session = Depends(get_session)) -> dict:
    if session.get(User, user_id) is None:
        raise HTTPException(status_code=404, detail="unknown user")
    query = select(Suggestion).where(Suggestion.user_id == user_id)
    if not include_dismissed:
        query = query.where(Suggestion.dismissed_at.is_(None))
    rows = session.execute(query.order_by(Suggestion.created_at.desc()).limit(50)).scalars().all()
    return {"user_id": user_id, "suggestions": [suggestion_view(s) for s in rows]}


class DismissBody(BaseModel):
    user_id: str = Field(..., min_length=1)


@router.post("/suggestions/{suggestion_id}/dismiss")
def dismiss(suggestion_id: str, body: DismissBody, session: Session = Depends(get_session)) -> dict:
    s = session.get(Suggestion, suggestion_id)
    if s is None or s.user_id != body.user_id:
        raise HTTPException(status_code=404, detail="unknown suggestion")
    if s.dismissed_at is None:
        s.dismissed_at = datetime.now(timezone.utc)
        session.commit()
    return suggestion_view(s)
