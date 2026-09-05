"""Agentic Card endpoints (Phase 6b).

Every route is a structured USER action or a read of verified state; the
browser calls these, the model never does. Money still moves only through
the intent state machine: a rule that fires creates an intent through the
same policy engine as everything else, and the student's YES leads to
Razorpay test checkout (or, in DEBUG auto mode, a simulated settlement).

  GET    /api/card/{user_id}                          the whole Card screen
  PATCH  /api/card/{user_id}/limits                   set caps / approval line / freeze
  GET    /api/card/products                           synthetic catalogue + quotes
  POST   /api/card/{user_id}/rules                    create a purchase rule
  DELETE /api/card/{user_id}/rules/{rule_id}          cancel one
  POST   /api/card/{user_id}/rules/{rule_id}/respond  {"answer": "yes"|"no"} from the rule card
  POST   /api/card/{user_id}/rules/{rule_id}/resume   BLOCKED -> ACTIVE
  GET    /api/card/{user_id}/notifications
  POST   /api/card/{user_id}/notifications/read       mark all (or one) read
  POST   /api/card/{user_id}/notifications/{id}/respond   {"answer": "yes"|"no"}
  POST   /api/card/{user_id}/tick                     run one monitor pass now (demo)
"""

from __future__ import annotations

from typing import Any, Callable, Literal, TypeVar

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.orm import Session

from backend.models.db import get_session
from backend.services import agent_card_service as card
from backend.services import money_action_service as mas

router = APIRouter(prefix="/api/card", tags=["agentic-card"])
T = TypeVar("T")


def _run(session: Session, fn: Callable[[], T], *, write: bool = False) -> T:
    try:
        out = fn()
        if write:
            session.commit()
        return out
    except LookupError as exc:
        session.rollback()
        raise HTTPException(status_code=404, detail=f"not found: {exc}")
    except (mas.NotPermitted, PermissionError) as exc:
        session.rollback()
        raise HTTPException(status_code=403, detail=str(exc))
    except mas.IllegalTransition as exc:
        session.rollback()
        raise HTTPException(status_code=409, detail=str(exc))
    except ValueError as exc:
        session.rollback()
        raise HTTPException(status_code=400, detail=str(exc))


class _Strip(BaseModel):
    @field_validator("*", mode="before")
    @classmethod
    def _strip(cls, v):  # noqa: ANN001
        return v.strip() if isinstance(v, str) else v


class LimitsBody(_Strip):
    monthly_limit_rupees: float | None = Field(None, ge=0)
    per_tx_limit_rupees: float | None = Field(None, gt=0)
    clear_per_tx_limit: bool = False
    approval_threshold_rupees: float | None = Field(None, ge=0)
    frozen: bool | None = None


class ConditionBody(BaseModel):
    type: str
    value: Any = None


class RuleBody(_Strip):
    product_id: str = Field(..., min_length=1)
    target_price_rupees: float = Field(..., gt=0)
    platforms: list[str] = Field(default_factory=list)
    conditions: list[ConditionBody] = Field(default_factory=list, max_length=8)
    approval_mode: Literal["manual", "auto"] = "manual"
    approval_window_seconds: int | None = Field(None, ge=30, le=86_400)


class ReadBody(BaseModel):
    notification_id: str | None = None


class RespondBody(BaseModel):
    answer: Literal["yes", "no"]


def _paise(rupees: float | None) -> int | None:
    return None if rupees is None else int(round(rupees * 100))


@router.get("/products")
def products(session: Session = Depends(get_session)) -> dict:
    return {"products": card.list_products(session), "platforms": [{"key": k, "label": v} for k, v in card.PLATFORMS.items()]}


@router.get("/{user_id}")
def view(user_id: str, session: Session = Depends(get_session)) -> dict:
    return _run(session, lambda: card.card_view(session, user_id), write=True)  # issues the card on first look


@router.patch("/{user_id}/limits")
def limits(user_id: str, body: LimitsBody, session: Session = Depends(get_session)) -> dict:
    return _run(session, lambda: card.update_limits(
        session, user_id, monthly_limit_paise=_paise(body.monthly_limit_rupees),
        per_tx_limit_paise=_paise(body.per_tx_limit_rupees), clear_per_tx=body.clear_per_tx_limit,
        approval_threshold_paise=_paise(body.approval_threshold_rupees), frozen=body.frozen), write=True)


@router.post("/{user_id}/rules", status_code=201)
def create_rule(user_id: str, body: RuleBody, session: Session = Depends(get_session)) -> dict:
    return _run(session, lambda: card.create_rule(
        session, user_id, product_id=body.product_id, target_price_paise=_paise(body.target_price_rupees),
        platforms=body.platforms, conditions=[c.model_dump() for c in body.conditions],
        approval_mode=body.approval_mode, approval_window_seconds=body.approval_window_seconds), write=True)


@router.delete("/{user_id}/rules/{rule_id}")
def cancel_rule(user_id: str, rule_id: str, session: Session = Depends(get_session)) -> dict:
    return _run(session, lambda: card.cancel_rule(session, user_id, rule_id), write=True)


@router.post("/{user_id}/rules/{rule_id}/respond")
def respond_rule(user_id: str, rule_id: str, body: RespondBody, session: Session = Depends(get_session)) -> dict:
    """YES / NO straight from the rule card (same action as answering the notification)."""
    return _run(session, lambda: card.respond_rule(session, user_id, rule_id=rule_id, answer=body.answer), write=True)


@router.post("/{user_id}/rules/{rule_id}/resume")
def resume_rule(user_id: str, rule_id: str, session: Session = Depends(get_session)) -> dict:
    return _run(session, lambda: card.resume_rule(session, user_id, rule_id), write=True)


@router.get("/{user_id}/notifications")
def notifications(user_id: str, session: Session = Depends(get_session)) -> dict:
    return _run(session, lambda: card.list_notifications(session, user_id))


@router.post("/{user_id}/notifications/read")
def mark_read(user_id: str, body: ReadBody, session: Session = Depends(get_session)) -> dict:
    n = _run(session, lambda: card.mark_read(session, user_id, notification_id=body.notification_id), write=True)
    return {"marked": n}


@router.post("/{user_id}/notifications/{notification_id}/respond")
def respond(user_id: str, notification_id: str, body: RespondBody, session: Session = Depends(get_session)) -> dict:
    return _run(session, lambda: card.respond(session, user_id, notification_id=notification_id, answer=body.answer), write=True)


@router.post("/{user_id}/tick")
def tick(user_id: str, session: Session = Depends(get_session)) -> dict:
    """Run one monitor pass immediately (the background loop runs it anyway).
    Lets the screen offer a 'check prices now' button without waiting."""
    return _run(session, lambda: card.tick(session), write=True)
