"""Synthetic partner offers: deterministic filtering and ranking.

PRD s4.2/s11: offers are promotions from partners, always synthetic, always
labelled as such, and ranking is plain deterministic code — never an LLM
judgement. This module only reads; nothing here moves money or writes state.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.models.entities import Offer
from backend.services import reward_service


def _effective_discount_paise(offer: Offer) -> int:
    """A comparable "how much this saves" figure, for ranking only.

    A percentage discount needs a list price to convert to paise; without one
    it ranks below any offer with a known paise discount rather than being
    guessed at.
    """
    if offer.discount_paise is not None:
        return offer.discount_paise
    if offer.discount_pct is not None and offer.list_price_paise is not None:
        return round(offer.list_price_paise * offer.discount_pct / 100)
    return 0


def _effective_price_paise(offer: Offer) -> int | None:
    if offer.list_price_paise is None:
        return None
    return max(0, offer.list_price_paise - _effective_discount_paise(offer))


def list_eligible_offers(
    session: Session,
    user_id: str,
    *,
    category: str | None = None,
    budget_paise: int | None = None,
) -> list[dict[str, Any]]:
    """Non-expired offers, optionally filtered by category/budget, ranked by
    the highest deterministic savings first.

    Eligibility is evaluated the same way reward eligibility is (min
    contributions, streaks, etc.) via reward_service.evaluate, since offers
    carry the same `eligibility` shape as rewards (PRD s4.2). An offer whose
    price is unknown is never excluded by a budget filter, because there is
    nothing to disprove affordability with — it is surfaced with
    effective_price_paise: null instead of a guess.
    """
    now = datetime.now(timezone.utc)
    query = select(Offer).where((Offer.expiry.is_(None)) | (Offer.expiry > now))
    if category:
        query = query.where(Offer.category == category)

    offers = list(session.execute(query).scalars().all())

    rows: list[dict[str, Any]] = []
    for offer in offers:
        eligible, facts = reward_service.evaluate(session, user_id, offer.eligibility or {})
        if not eligible:
            continue
        price = _effective_price_paise(offer)
        if budget_paise is not None and price is not None and price > budget_paise:
            continue
        rows.append(
            {
                "offer_id": offer.id,
                "merchant": offer.merchant,
                "title": offer.title,
                "category": offer.category,
                "list_price_paise": offer.list_price_paise,
                "discount_paise": offer.discount_paise,
                "discount_pct": offer.discount_pct,
                "effective_price_paise": price,
                "effective_discount_paise": _effective_discount_paise(offer),
                "expiry": offer.expiry.isoformat() if offer.expiry else None,
                "funding_source": offer.funding_source.value,
                "eligibility_facts": facts,
                "is_synthetic": offer.is_synthetic,
            }
        )

    rows.sort(key=lambda r: r["effective_discount_paise"], reverse=True)
    return rows
