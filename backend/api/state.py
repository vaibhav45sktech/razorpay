"""Read-only state endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from backend.models.db import get_session
from backend.services import money_action_service, state_service

router = APIRouter(prefix="/api", tags=["state"])


@router.get("/state/{user_id}")
def get_state(user_id: str, session: Session = Depends(get_session)) -> dict:
    """Everything the UI renders and the agent is shown. Same numbers, same source."""
    try:
        return state_service.get_state(session, user_id)
    except state_service.UnknownUser:
        raise HTTPException(status_code=404, detail="unknown user")


@router.get("/intents/{intent_id}")
def get_intent(intent_id: str, session: Session = Depends(get_session)) -> dict:
    try:
        intent = money_action_service.get(session, intent_id)
    except money_action_service.IntentNotFound:
        raise HTTPException(status_code=404, detail="unknown intent")
    return {
        "intent_id": intent.id, "user_id": intent.user_id, "type": intent.type.value,
        "amount_paise": intent.amount_paise, "purpose": intent.purpose,
        "status": intent.status.value, "policy_result": intent.policy_result,
        "provider_ref": intent.provider_ref, "created_at": intent.created_at.isoformat(),
        "updated_at": intent.updated_at.isoformat(),
        "reversed": money_action_service.is_reversed(session, intent),
    }
