"""DEVELOPMENT-ONLY routes. Every route here 404s unless DEBUG=true.

The fake settler stands in for Razorpay until Phase 5. It calls the SAME
settle_success / settle_failure code paths the real webhook will call, so the
state machine and ledger are exercised for real; only the provider is fake.
Evidence is stamped {"debug": True} so a fake settlement can never be mistaken
for a provider-confirmed one in the audit trail.

TEMPORARY SCAFFOLDING. Phase 5 checklist: confirm these 404 with DEBUG=false.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.orm import Session

from backend.api.deps import require_debug
from backend.models.db import get_session
from backend.models.entities import AuditActor
from backend.seed import demo_data
from backend.services import money_action_service as mas

router = APIRouter(prefix="/debug", tags=["debug"], dependencies=[Depends(require_debug)])


class CreateIntentBody(BaseModel):
    user_id: str
    action: str = Field(..., description="PURCHASE | CONTRIBUTION | TEST_PAYOUT")
    amount_paise: int
    purpose: str
    bucket: str | None = None

    @field_validator("*", mode="before")
    @classmethod
    def _strip_strings(cls, v):  # noqa: ANN001
        # Ids pasted with a stray space ("usr_x ") produced confusing 403/404s
        # during Phase 5 manual testing; trim every string field.
        return v.strip() if isinstance(v, str) else v


@router.post("/seed")
def seed(reset: bool = False, session: Session = Depends(get_session)) -> dict:
    wrote = demo_data.seed_all(session, force=reset)
    session.commit()
    return {"seeded": wrote, "note": "ALL DATA IS SYNTHETIC / DEMO"}


@router.post("/intents")
def create_intent(body: CreateIntentBody, session: Session = Depends(get_session)) -> dict:
    """Stand-in for the agent's create_payment_intent tool until Phase 4."""
    try:
        result = mas.create(
            session, user_id=body.user_id, action=body.action, amount_paise=body.amount_paise,
            purpose=body.purpose, bucket=body.bucket, actor=AuditActor.USER,
        )
        session.commit()
        return result.as_dict()
    except mas.NotPermitted as exc:
        session.rollback()
        raise HTTPException(status_code=403, detail=str(exc))


def _load(session: Session, intent_id: str):
    try:
        return mas.get(session, intent_id)
    except mas.IntentNotFound:
        raise HTTPException(status_code=404, detail="unknown intent")


@router.post("/intents/{intent_id}/fake-settle")
def fake_settle(intent_id: str, session: Session = Depends(get_session)) -> dict:
    """ALLOWED/APPROVED -> EXECUTING -> ... -> LEDGER_UPDATED, with a fake provider."""
    intent = _load(session, intent_id)
    try:
        mas.begin_execution(session, intent, evidence={"debug": True, "provider": "fake"})
        mas.settle_success(
            session, intent,
            provider_evidence={"debug": True, "provider": "fake", "id": f"fake_{intent.id}"},
            source=f"debug:fake_settle:{intent.id}",
        )
        session.commit()
    except mas.IllegalTransition as exc:
        session.rollback()
        raise HTTPException(status_code=409, detail=str(exc))
    return {"intent_id": intent.id, "status": intent.status.value, "debug": True}


@router.post("/intents/{intent_id}/fake-fail")
def fake_fail(intent_id: str, session: Session = Depends(get_session)) -> dict:
    """ALLOWED/APPROVED -> EXECUTING -> FAILURE -> CLOSED. Ledger untouched."""
    intent = _load(session, intent_id)
    try:
        mas.begin_execution(session, intent, evidence={"debug": True, "provider": "fake"})
        mas.settle_failure(session, intent, provider_evidence={"debug": True, "error": "simulated failure"})
        session.commit()
    except mas.IllegalTransition as exc:
        session.rollback()
        raise HTTPException(status_code=409, detail=str(exc))
    return {"intent_id": intent.id, "status": intent.status.value, "debug": True}


class SetPriceBody(BaseModel):
    platform: str = "shopkart"
    price_rupees: float = Field(..., gt=0)
    stock: int | None = Field(None, ge=0)
    pinned: bool = True


@router.post("/card/price/{product_id}")
def set_price(product_id: str, body: SetPriceBody, session: Session = Depends(get_session)) -> dict:
    """Agentic Card demo lever: put a product at a chosen price (recorded as a
    real tick) and run the monitor once, so a rule can be watched firing."""
    from backend.services import agent_card_service as card

    try:
        view = card.set_price(session, product_id=product_id, platform=body.platform,
                              price_paise=int(round(body.price_rupees * 100)), stock=body.stock, pinned=body.pinned)
        report = card.tick(session, quote=False)
        session.commit()
    except LookupError as exc:
        session.rollback()
        raise HTTPException(status_code=404, detail=f"not found: {exc}")
    except ValueError as exc:
        session.rollback()
        raise HTTPException(status_code=400, detail=str(exc))
    return {"product": view, "tick": report, "debug": True}


@router.post("/card/rules/{rule_id}/expire-now")
def expire_rule_now(rule_id: str, session: Session = Depends(get_session)) -> dict:
    """Collapse a fired rule's approval window to now and run the monitor, to
    demonstrate the release path without waiting 15 minutes."""
    from datetime import datetime, timezone

    from backend.models.entities import PurchaseRule
    from backend.services import agent_card_service as card

    rule = session.get(PurchaseRule, rule_id)
    if rule is None:
        raise HTTPException(status_code=404, detail="unknown rule")
    rule.lock_expires_at = datetime.now(timezone.utc)
    report = card.tick(session, quote=False)
    session.commit()
    return {"rule": card.rule_view(session, rule), "tick": report, "debug": True}


@router.post("/intents/{intent_id}/reverse")
def reverse(intent_id: str, reason: str = "debug reversal", session: Session = Depends(get_session)) -> dict:
    intent = _load(session, intent_id)
    try:
        mas.reverse_settled(session, intent_id=intent.id, reason=reason)
        session.commit()
    except (mas.IllegalTransition, mas.NotPermitted) as exc:
        session.rollback()
        raise HTTPException(status_code=409, detail=str(exc))
    return {"intent_id": intent.id, "status": intent.status.value, "reversed": True}
