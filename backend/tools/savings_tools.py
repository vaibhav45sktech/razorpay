"""calculate_safe_contribution, update_goal — savings-goal tools.

calculate_safe_contribution is a deterministic RECOMMENDATION only: it never
creates a contribution itself (that still goes through create_payment_intent
-> policy_engine, like any other money action). The PRD does not define the
recommendation formula, so the one below is invented and clearly marked.
TODO: confirm the formula with product owner.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from backend.models.entities import Bucket, Goal, GoalStatus
from backend.models.schemas import (
    CalculateSafeContributionArgs,
    CalculateSafeContributionOut,
    UpdateGoalArgs,
    UpdateGoalOut,
)
from backend.services import ledger_service, policy_engine


def _owned_goal(session: Session, user_id: str, goal_id: str) -> Goal:
    goal = session.get(Goal, goal_id)
    if goal is None:
        raise LookupError(f"no such goal: {goal_id!r}")
    if goal.user_id != user_id:
        raise PermissionError("that goal does not belong to this account")
    return goal


def calculate_safe_contribution(
    session: Session, user_id: str, args: CalculateSafeContributionArgs
) -> CalculateSafeContributionOut:
    goal = _owned_goal(session, user_id, args.goal_id)
    cfg = policy_engine.load_config()

    current = ledger_service.get_balance(session, user_id, Bucket.EMERGENCY_SAVINGS)
    remaining = max(0, goal.target_amount_paise - current)

    reasons = [
        f"Goal '{goal.label}' targets ₹{goal.target_amount_paise / 100:,.0f}; "
        f"₹{current / 100:,.0f} saved so far, ₹{remaining / 100:,.0f} remaining.",
        f"Contributions must be between ₹{cfg.contribution_min_paise / 100:,.0f} "
        f"and ₹{cfg.contribution_max_paise / 100:,.0f} (PRD s1).",
    ]

    if remaining <= 0:
        recommended = cfg.contribution_min_paise
        reasons.append("Goal target already reached; recommending the minimum to keep the habit going.")
    else:
        recommended = min(cfg.contribution_max_paise, max(cfg.contribution_min_paise, remaining))
        if remaining < cfg.contribution_min_paise:
            reasons.append("Rounded up to the minimum contribution amount since less than that remains.")
        elif remaining > cfg.contribution_max_paise:
            reasons.append("Capped at the maximum single contribution amount.")

    return CalculateSafeContributionOut(
        goal_id=goal.id,
        recommended_amount_paise=recommended,
        min_paise=cfg.contribution_min_paise,
        max_paise=cfg.contribution_max_paise,
        reasons=reasons,
    )


def update_goal(session: Session, user_id: str, args: UpdateGoalArgs) -> UpdateGoalOut:
    goal = _owned_goal(session, user_id, args.goal_id)

    if args.event == "pause":
        goal.status = GoalStatus.PAUSED
    elif args.event == "resume":
        goal.status = GoalStatus.ACTIVE
    else:  # pragma: no cover - Literal type already restricts this
        raise ValueError(f"unsupported event: {args.event!r}")

    session.flush()
    return UpdateGoalOut(goal_id=goal.id, status=goal.status.value)
