"""Deterministic detectors. Every rule is a pure function of verified state
and returns zero or more Candidates. No model is involved here; this file is
the answer to "why did it suggest that?" and it must stay boring.

Each Candidate carries:
- kind:       rule name (stable, used for cooldown and for the UI)
- dedup_key:  what makes this instance distinct (an event id, a month, a
              milestone); the same (user, kind, dedup_key) is never stored twice
- facts:      the numbers the sentence must be written from — in RUPEES where
              user-facing, so the phrasing model never converts anything
- template:   the fallback sentence, already correct, used when the model is
              unavailable or produces something unusable
- source_event_id: the ledger event that triggered it, if any
"""

from __future__ import annotations

import dataclasses
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy.orm import Session

from backend.models.entities import IntentStatus, LedgerEvent, LedgerEventType
from backend.services import money_action_service, offer_service, state_service


@dataclasses.dataclass(frozen=True)
class Candidate:
    kind: str
    dedup_key: str
    facts: dict[str, Any]
    template: str
    source_event_id: str | None = None


def _r(paise: int | None) -> float:
    return round((paise or 0) / 100, 2)


def _rs(paise: int | None) -> str:
    return f"₹{(paise or 0) / 100:,.0f}" if (paise or 0) % 100 == 0 else f"₹{(paise or 0) / 100:,.2f}"


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


# --------------------------------------------------------------------------
# Rules
# --------------------------------------------------------------------------

def spend_pace(state: dict[str, Any], now: datetime) -> list[Candidate]:
    """Spending faster than the month is passing. Fires once per month per
    user (dedup on YYYY-MM), only past 40% of the limit, only when pace is
    clearly ahead — a nag that fires on day 2 for a coffee is worse than none."""
    spend = state.get("spending_this_month") or {}
    limit, used = spend.get("limit_paise") or 0, spend.get("used_paise") or 0
    if not limit or used < 0.4 * limit:
        return []
    days_in_month = 30
    month_fraction = max(now.day / days_in_month, 1 / days_in_month)
    used_fraction = used / limit
    if used_fraction < month_fraction * 1.25:
        return []
    facts = {
        "used_rupees": _r(used), "limit_rupees": _r(limit), "remaining_rupees": _r(spend.get("remaining_paise")),
        "pct_used": round(used_fraction * 100), "day_of_month": now.day,
    }
    return [Candidate(
        kind="spend_pace", dedup_key=now.strftime("%Y-%m"), facts=facts,
        template=(
            f"Heads up: you've used {facts['pct_used']}% of your {_rs(limit)} monthly budget by the "
            f"{now.day}th — {_rs(spend.get('remaining_paise'))} left for the rest of the month."
        ),
    )]


def large_purchase(state: dict[str, Any], event: LedgerEvent) -> list[Candidate]:
    """A single purchase worth 25%+ of the monthly limit. Dedup on the event."""
    if event.type is not LedgerEventType.PURCHASE:
        return []
    spend = state.get("spending_this_month") or {}
    limit = spend.get("limit_paise") or 0
    amount = abs(event.amount_paise)
    if not limit or amount < 0.25 * limit:
        return []
    facts = {
        "purchase_rupees": _r(amount), "limit_rupees": _r(limit),
        "remaining_rupees": _r(spend.get("remaining_paise")), "source": event.source,
    }
    return [Candidate(
        kind="large_purchase", dedup_key=event.id, facts=facts, source_event_id=event.id,
        template=(
            f"That {_rs(amount)} purchase was a big one — it leaves {_rs(spend.get('remaining_paise'))} "
            f"of your {_rs(limit)} monthly budget."
        ),
    )]


def goal_milestone(state: dict[str, Any], event: LedgerEvent) -> list[Candidate]:
    """A contribution pushed a goal across 25/50/75/100%. Dedup on goal+milestone."""
    if event.type is not LedgerEventType.CONTRIBUTION:
        return []
    out: list[Candidate] = []
    for g in state.get("goals") or []:
        target, current = g.get("target_paise") or 0, g.get("current_paise") or 0
        if not target:
            continue
        before = (current - event.amount_paise) / target * 100
        after = current / target * 100
        for m in (25, 50, 75, 100):
            if before < m <= after:
                facts = {
                    "goal": g.get("label"), "milestone_pct": m, "saved_rupees": _r(current),
                    "target_rupees": _r(target), "contribution_rupees": _r(event.amount_paise),
                }
                out.append(Candidate(
                    kind="goal_milestone", dedup_key=f"{g.get('goal_id')}:{m}", facts=facts,
                    source_event_id=event.id,
                    template=(
                        f"Nice — that {_rs(event.amount_paise)} took '{g.get('label')}' past {m}%: "
                        f"{_rs(current)} of {_rs(target)} saved."
                    ),
                ))
    return out


def savings_nudge(session: Session, state: dict[str, Any], user_id: str, now: datetime) -> list[Candidate]:
    """No contribution in 14 days while a goal is still open. Dedup on the
    ISO week so it can repeat, but not daily."""
    goals = [g for g in (state.get("goals") or []) if (g.get("pct_complete") or 0) < 100]
    if not goals:
        return []
    recent = [
        e for e in _recent_events(session, user_id)
        if e.type is LedgerEventType.CONTRIBUTION and _aware(e.created_at) >= now - timedelta(days=14)
    ]
    if recent:
        return []
    g = goals[0]
    facts = {
        "goal": g.get("label"), "remaining_rupees": _r(g.get("remaining_paise")),
        "pct_complete": g.get("pct_complete"), "days_since_last_contribution": ">14",
    }
    return [Candidate(
        kind="savings_nudge", dedup_key=now.strftime("%G-W%V"), facts=facts,
        template=(
            f"It's been a couple of weeks since your last contribution. '{g.get('label')}' is "
            f"{g.get('pct_complete')}% there — even ₹100 today keeps the habit going."
        ),
    )]


def pending_approval(session: Session, user_id: str, now: datetime) -> list[Candidate]:
    """An intent has been waiting for the user's approval for 10+ minutes.
    Dedup on the intent id. This is a reminder that the user has a decision to
    make in the app — never a suggestion about which way to decide."""
    out: list[Candidate] = []
    for i in money_action_service.list_pending(session, user_id):
        if i.status is not IntentStatus.AWAITING_APPROVAL:
            continue
        if _aware(i.created_at) > now - timedelta(minutes=10):
            continue
        facts = {"intent_id": i.id, "amount_rupees": _r(i.amount_paise), "purpose": i.purpose, "type": i.type.value}
        out.append(Candidate(
            kind="pending_approval", dedup_key=i.id, facts=facts,
            template=(
                f"Reminder: a {_rs(i.amount_paise)} {i.type.value.lower()} ({i.purpose}) is still waiting "
                "for your approval in the app."
            ),
        ))
    return out


def offer_match(session: Session, user_id: str, event: LedgerEvent) -> list[Candidate]:
    """A purchase just happened; is there an eligible partner offer in the same
    category? Labelled as a promotion. Dedup on event+offer. The category is
    read from the purchase's source string when it carries one
    ("purchase:food:..." style); unknown categories produce nothing rather
    than a guess."""
    if event.type is not LedgerEventType.PURCHASE:
        return []
    parts = (event.source or "").split(":")
    category = parts[1] if len(parts) >= 3 and parts[0] == "purchase" else None
    if not category:
        return []
    offers = offer_service.list_eligible_offers(session, user_id, category=category)
    if not offers:
        return []
    o = offers[0]
    facts = {
        "category": category, "merchant": o.get("merchant"), "title": o.get("title"),
        "savings_rupees": _r(o.get("effective_discount_paise")), "offer_id": o.get("offer_id"),
    }
    return [Candidate(
        kind="offer_match", dedup_key=f"{event.id}:{facts['offer_id']}", facts=facts, source_event_id=event.id,
        template=(
            f"Since you just spent on {category}: {o.get('merchant')} has a partner offer "
            f"('{o.get('title')}'). Promotions are not financial advice — only if it's useful to you."
        ),
    )]


# --------------------------------------------------------------------------
# Entry points used by service.py
# --------------------------------------------------------------------------

def _recent_events(session: Session, user_id: str) -> list[LedgerEvent]:
    from backend.services import ledger_service
    return ledger_service.get_recent_events(session, user_id, limit=50)


def candidates_for_event(session: Session, user_id: str, event: LedgerEvent) -> list[Candidate]:
    """Rules that react to ONE new ledger event."""
    state = state_service.get_state(session, user_id)
    return [*large_purchase(state, event), *goal_milestone(state, event), *offer_match(session, user_id, event)]


def candidates_periodic(session: Session, user_id: str, now: datetime | None = None) -> list[Candidate]:
    """Rules that look at the user's overall position, run every poll."""
    now = now or datetime.now(timezone.utc)
    state = state_service.get_state(session, user_id)
    return [
        *spend_pace(state, now),
        *savings_nudge(session, state, user_id, now),
        *pending_approval(session, user_id, now),
    ]
