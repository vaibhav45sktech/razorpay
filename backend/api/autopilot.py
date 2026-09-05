"""Autopilot endpoints — the agent-led flow (Phase 6 pivot).

Every route here is a structured USER action or a read of verified state.
The model never calls these; the browser does, and the user's tap is the
authorisation. Money still only moves through the intent state machine +
Razorpay test checkout, and a pool draw still needs the policy engine to
find an explainable allocation (PRD s4.1, s5.4).

  GET  /api/plan/{user_id}                 this month's contribution plan
  POST /api/plan/{user_id}/agree           one tap -> CONTRIBUTION intent
  GET  /api/needs/{user_id}                upcoming needs (form data)
  POST /api/needs/{user_id}                add one
  DELETE /api/needs/{user_id}/{need_id}    remove one
  GET  /api/pool/{user_id}                 round timeline + draw recommendation
  POST /api/pool/{user_id}/request-round   record the member's chosen round
  POST /api/pool/{user_id}/simulate-draw   DEBUG only: policy-gated simulated payout
  GET  /api/spend/{user_id}                offers matched to needs + headroom
  POST /api/spend/{user_id}/propose        tap an offer -> PURCHASE intent
"""

from __future__ import annotations

import re
from typing import Callable, TypeVar

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.orm import Session

from backend.models.db import get_session
from backend.services import autopilot_service as ap
from backend.services import money_action_service as mas

router = APIRouter(prefix="/api", tags=["autopilot"])

T = TypeVar("T")
_YM = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")


def _run(session: Session, fn: Callable[[], T], *, write: bool = False) -> T:
    """Map service exceptions to HTTP; commit writes, roll back on failure."""
    try:
        out = fn()
        if write:
            session.commit()
        return out
    except LookupError as exc:
        session.rollback()
        raise HTTPException(status_code=404, detail=f"not found: {exc}")
    except PermissionError as exc:
        session.rollback()
        raise HTTPException(status_code=403, detail=str(exc))
    except mas.NotPermitted as exc:
        session.rollback()
        raise HTTPException(status_code=403, detail=str(exc))
    except mas.IllegalTransition as exc:
        session.rollback()
        raise HTTPException(status_code=409, detail=str(exc))
    except ValueError as exc:
        session.rollback()
        raise HTTPException(status_code=409, detail=str(exc))


class _Stripped(BaseModel):
    @field_validator("*", mode="before")
    @classmethod
    def _strip_strings(cls, v):  # noqa: ANN001
        return v.strip() if isinstance(v, str) else v


# ---- plan --------------------------------------------------------------------

@router.get("/plan/{user_id}")
def get_plan(user_id: str, session: Session = Depends(get_session)) -> dict:
    return _run(session, lambda: ap.monthly_plan(session, user_id.strip()))


@router.post("/plan/{user_id}/agree")
def agree(user_id: str, session: Session = Depends(get_session)) -> dict:
    return _run(session, lambda: ap.agree_to_plan(session, user_id.strip()), write=True)


# ---- needs -------------------------------------------------------------------

class NeedBody(_Stripped):
    label: str = Field(..., min_length=2, max_length=120)
    month: str = Field(..., description="YYYY-MM")
    amount_rupees: float = Field(..., gt=0, le=1_000_000)
    category: str | None = None

    @field_validator("month")
    @classmethod
    def _month_shape(cls, v: str) -> str:
        if not _YM.match(v):
            raise ValueError("month must look like 2026-11")
        return v

    @field_validator("category")
    @classmethod
    def _known_category(cls, v: str | None) -> str | None:
        if v in (None, ""):
            return None
        v = v.lower()
        if v not in ap.NEED_CATEGORIES:
            raise ValueError(f"category must be one of {', '.join(ap.NEED_CATEGORIES)}")
        return v


@router.get("/needs/{user_id}")
def get_needs(user_id: str, session: Session = Depends(get_session)) -> dict:
    return {"needs": _run(session, lambda: ap.list_needs(session, user_id.strip())), "categories": list(ap.NEED_CATEGORIES)}


@router.post("/needs/{user_id}", status_code=201)
def post_need(user_id: str, body: NeedBody, session: Session = Depends(get_session)) -> dict:
    return _run(
        session,
        lambda: ap.add_need(
            session, user_id.strip(), label=body.label, month=body.month,
            amount_paise=round(body.amount_rupees * 100), category=body.category,
        ),
        write=True,
    )


@router.delete("/needs/{user_id}/{need_id}")
def remove_need(user_id: str, need_id: str, session: Session = Depends(get_session)) -> dict:
    _run(session, lambda: ap.delete_need(session, user_id.strip(), need_id.strip()), write=True)
    return {"deleted": need_id}


# ---- pool --------------------------------------------------------------------

class RoundBody(_Stripped):
    month: str

    @field_validator("month")
    @classmethod
    def _month_shape(cls, v: str) -> str:
        if not _YM.match(v):
            raise ValueError("month must look like 2026-11")
        return v


@router.get("/pool/{user_id}")
def get_pool(user_id: str, session: Session = Depends(get_session)) -> dict:
    return _run(session, lambda: ap.pool_view(session, user_id.strip()))


@router.post("/pool/{user_id}/request-round")
def request_round(user_id: str, body: RoundBody, session: Session = Depends(get_session)) -> dict:
    return _run(session, lambda: ap.request_round(session, user_id.strip(), month=body.month), write=True)


@router.post("/pool/{user_id}/simulate-draw")
def simulate_draw(user_id: str, session: Session = Depends(get_session)) -> dict:
    return _run(session, lambda: ap.simulate_draw(session, user_id.strip()), write=True)


# ---- spend -------------------------------------------------------------------

class ProposeBody(_Stripped):
    offer_id: str = Field(..., min_length=1)


@router.get("/spend/{user_id}")
def get_spend(user_id: str, session: Session = Depends(get_session)) -> dict:
    return _run(session, lambda: ap.spend_view(session, user_id.strip()))


@router.post("/spend/{user_id}/propose")
def propose(user_id: str, body: ProposeBody, session: Session = Depends(get_session)) -> dict:
    return _run(session, lambda: ap.propose_purchase(session, user_id.strip(), offer_id=body.offer_id), write=True)
