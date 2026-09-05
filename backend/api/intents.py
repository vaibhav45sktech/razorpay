"""Intent endpoints that represent structured USER actions.

Approval lives here and NOT in the agent's tool set (PRD s5.4: authorisation is
never inferred from conversation). The chat can tell the user an approval is
needed; only a call to these endpoints can grant it.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.orm import Session

from backend import observability
from backend.models.db import get_session
from backend.services import money_action_service as mas
from backend.services import razorpay_adapter

router = APIRouter(prefix="/api/intents", tags=["intents"])


class ApprovalBody(BaseModel):
    # No auth in the prototype: the body names the acting user and the service
    # checks it owns the intent. A real deployment replaces this with a session.
    user_id: str = Field(..., min_length=1)

    @field_validator("*", mode="before")
    @classmethod
    def _strip_strings(cls, v):  # noqa: ANN001
        # Ids pasted with a stray space ("usr_x ") produced confusing 403/404s
        # during Phase 5 manual testing; trim every string field.
        return v.strip() if isinstance(v, str) else v


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


@router.post("/{intent_id}/execute")
@observability.limiter.limit("20/minute")
def execute(request: Request, intent_id: str, body: ApprovalBody,
            session: Session = Depends(get_session)) -> dict:
    """ALLOWED/APPROVED -> EXECUTING by creating a Razorpay TEST-MODE order
    (HLD s6.4). Returns what the browser needs to open Checkout: order id,
    amount, currency and the PUBLIC key id. Idempotent - see
    money_action_service.execute()."""
    if not razorpay_adapter.enabled():
        raise HTTPException(status_code=503, detail="Razorpay test mode is not configured on this server")
    try:
        result = mas.execute(session, intent_id=intent_id, user_id=body.user_id)
        session.commit()
    except mas.IntentNotFound:
        session.rollback()
        raise HTTPException(status_code=404, detail="unknown intent")
    except mas.NotPermitted as exc:
        session.rollback()
        raise HTTPException(status_code=403, detail=str(exc))
    except mas.IllegalTransition as exc:
        session.rollback()
        raise HTTPException(status_code=409, detail=str(exc))
    except razorpay_adapter.ProviderTimeout:
        session.commit()  # the audit row about the timeout must survive
        raise HTTPException(status_code=503, detail="payment provider timed out; retry - no duplicate order will be created",
                            headers={"Retry-After": "3"})
    except razorpay_adapter.ProviderError as exc:
        session.rollback()
        raise HTTPException(status_code=502, detail=f"payment provider error: {exc}")
    return {**result.as_dict(), **razorpay_adapter.public_checkout_config()}
