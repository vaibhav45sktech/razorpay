"""Rewards: milestone and streak eligibility, evaluated deterministically.

PRD s4.2: reward source is always explicit; PRD s8.2: reward math is plain
deterministic code, never an LLM judgement. This module only flips rewards
between LOCKED and ELIGIBLE by evaluating machine-checkable conditions against
the ledger. It never moves money: crediting a reward is a money action, and
money actions go through the intent state machine like everything else.

Supported eligibility keys (all optional; a reward is ELIGIBLE when every key it
declares is satisfied):

    streak_months        - distinct calendar months with >= 1 CONTRIBUTION event
    min_contributions    - total CONTRIBUTION events
    target_balance_paise - emergency-savings derived balance at or above this

Unknown keys make a reward permanently LOCKED rather than accidentally ELIGIBLE
(default-deny, same principle as the policy engine). The seed data only uses the
keys above; anything else is a TODO to define with the product owner.
"""

from __future__ import annotations

import logging

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from backend.models.entities import Bucket, LedgerEvent, LedgerEventType, Reward, RewardStatus
from backend.services import ledger_service

logger = logging.getLogger("campuspool.rewards")

SUPPORTED_KEYS = frozenset({"streak_months", "min_contributions", "target_balance_paise"})


def contribution_months(session: Session, user_id: str) -> int:
    """Distinct YYYY-MM buckets in which the user contributed at least once."""
    months = session.execute(
        select(func.strftime("%Y-%m", LedgerEvent.created_at))
        .where(LedgerEvent.user_id == user_id, LedgerEvent.type == LedgerEventType.CONTRIBUTION)
        .distinct()
    ).scalars().all()
    return len([m for m in months if m])


def contribution_count(session: Session, user_id: str) -> int:
    return int(
        session.execute(
            select(func.count(LedgerEvent.id)).where(
                LedgerEvent.user_id == user_id, LedgerEvent.type == LedgerEventType.CONTRIBUTION
            )
        ).scalar_one()
    )


def evaluate(session: Session, user_id: str, eligibility: dict) -> tuple[bool, dict]:
    """Return (eligible, facts). facts records the numbers used, for explanation."""
    facts: dict = {}
    if not eligibility:
        return True, facts
    if not set(eligibility) <= SUPPORTED_KEYS:
        facts["unsupported_keys"] = sorted(set(eligibility) - SUPPORTED_KEYS)
        return False, facts

    ok = True
    if "streak_months" in eligibility:
        facts["streak_months"] = contribution_months(session, user_id)
        ok &= facts["streak_months"] >= int(eligibility["streak_months"])
    if "min_contributions" in eligibility:
        facts["contributions"] = contribution_count(session, user_id)
        ok &= facts["contributions"] >= int(eligibility["min_contributions"])
    if "target_balance_paise" in eligibility:
        facts["emergency_balance_paise"] = ledger_service.get_balance(session, user_id, Bucket.EMERGENCY_SAVINGS)
        ok &= facts["emergency_balance_paise"] >= int(eligibility["target_balance_paise"])
    return bool(ok), facts


def recompute_eligibility(session: Session, user_id: str) -> list[Reward]:
    """Re-evaluate every LOCKED/ELIGIBLE reward for the user against the ledger.

    Called after every settlement and reversal. REDEEMED and EXPIRED rewards
    are final and are not touched. A reward can move ELIGIBLE -> LOCKED again
    (e.g. after a reversal drops the balance below target); that is correct.
    """
    rewards = session.execute(
        select(Reward).where(
            Reward.user_id == user_id, Reward.status.in_([RewardStatus.LOCKED, RewardStatus.ELIGIBLE])
        )
    ).scalars().all()

    changed: list[Reward] = []
    for reward in rewards:
        eligible, _ = evaluate(session, user_id, reward.eligibility or {})
        new_status = RewardStatus.ELIGIBLE if eligible else RewardStatus.LOCKED
        if reward.status is not new_status:
            logger.info("reward %s %s -> %s", reward.id, reward.status.value, new_status.value)
            reward.status = new_status
            changed.append(reward)
    session.flush()
    return changed


def list_for_user(session: Session, user_id: str) -> list[dict]:
    rewards = session.execute(select(Reward).where(Reward.user_id == user_id)).scalars().all()
    out = []
    for r in rewards:
        eligible, facts = evaluate(session, user_id, r.eligibility or {})
        out.append({
            "reward_id": r.id, "label": r.label, "source": r.source.value,
            "amount_paise": r.amount_paise, "status": r.status.value,
            "eligibility": r.eligibility, "facts": facts, "is_synthetic": r.is_synthetic,
        })
    return out
