"""get_offers, get_eligible_rewards — partner offers and milestone rewards.

Both are thin wrappers over deterministic services (offer_service,
reward_service). Ranking and eligibility are plain code; the model only
reads the result and must say, per the system prompt, that offers are
promotions rather than financial advice (PRD s4.2).
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from backend.models.schemas import (
    GetEligibleRewardsArgs,
    GetEligibleRewardsOut,
    GetOffersArgs,
    GetOffersOut,
)
from backend.services import offer_service, reward_service


def get_offers(session: Session, user_id: str, args: GetOffersArgs) -> GetOffersOut:
    offers = offer_service.list_eligible_offers(
        session, user_id, category=args.category, budget_paise=args.budget_paise
    )
    return GetOffersOut(
        offers=offers,
        note="All offers are synthetic demo content, not real merchant promotions.",
    )


def get_eligible_rewards(session: Session, user_id: str, args: GetEligibleRewardsArgs) -> GetEligibleRewardsOut:
    rewards = reward_service.list_for_user(session, user_id)
    by_status: dict[str, list[dict]] = {"eligible": [], "locked": [], "redeemed": [], "expired": []}
    for r in rewards:
        by_status.setdefault(r["status"], []).append(r)

    return GetEligibleRewardsOut(
        eligible=by_status["eligible"],
        locked=by_status["locked"],
        redeemed=by_status["redeemed"] + by_status.get("expired", []),
    )
