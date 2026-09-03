"""Intent endpoints that represent structured USER actions.

Approval lives here and NOT in the agent's tool set (PRD s5.4: authorisation is
never inferred from conversation). The chat can tell the user an approval is
needed; only a call to these endpoints can grant it.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from backend.models.db import get_session
from backend.services import money_action_service as mas

router = APIRouter(prefix="/api/intents", tags=["intents"])


class ApprovalBody(BaseModel):
    # No auth in the prototype: the body names the acting user and the service
    # checks it owns the intent. A real deployment replaces this with a session.
    user_id: str = Field(..., min_length=1)


def _handle(fn, session: Session):
    try:
        intent = fn()
        session.commit()
        return {"intent_id": intent.id, "status": intent.status.value}
    except mas.IntentNotFound:
        session.rollback()
        raise HTTPException(status_code=404, detail="unknown intent")
    except mas.NotPermitted as exc:
        session.rollback()
        raise HTTPException(status_code=403, detail=str(exc))
    except mas.IllegalTransition as exc:
        session.rollback()
        raise HTTPException(status_code=409, detail=str(exc))


@router.post("/{intent_id}/approve")
def approve(intent_id: str, body: ApprovalBody, session: Session = Depends(get_session)) -> dict:
    return _handle(lambda: mas.approve(session, intent_id=intent_id, user_id=body.user_id), session)


@router.post("/{intent_id}/deny")
def deny(intent_id: str, body: ApprovalBody, session: Session = Depends(get_session)) -> dict:
    return _handle(lambda: mas.deny_approval(session, intent_id=intent_id, user_id=body.user_id), session)
