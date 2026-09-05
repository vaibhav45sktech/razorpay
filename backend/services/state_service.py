"""One verified snapshot of a user's financial state, assembled from the ledger.

Two consumers, one function:
  - GET /api/state/{user_id}   the frontend renders exactly this
  - orchestrator.observe()     the agent is shown exactly this every turn

That sharing is deliberate. What the user sees and what the model is told are
the same numbers from the same derivation, so the agent can never "know"
something the UI does not, and vice versa. Everything here is read-only.

Amounts are paise. Discretionary spending is reported as spend-against-limit,
never as a balance (decision D2.1 in the master build plan).
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from backend import config
from backend.models.entities import Bucket, Goal, Notification, PurchaseRule, RuleStatus, User
from backend.services import ledger_service, money_action_service, pool_service, reward_service


class UnknownUser(LookupError):
    pass


def get_state(session: Session, user_id: str) -> dict[str, Any]:
    user = session.get(User, user_id)
    if user is None:
        raise UnknownUser(user_id)

    policy = user.spend_policy
    balances = ledger_service.get_balances(session, user_id)
    emergency = balances[Bucket.EMERGENCY_SAVINGS.value]

    spending = (
        ledger_service.get_month_spend_summary(session, user_id, monthly_limit_paise=policy.monthly_limit_paise)
        if policy else None
    )

    # ALL goals, paused included, each carrying its status. A paused goal
    # that vanishes from state cannot be resumed by anyone - the UI has
    # nothing to click and the agent has no id to pass (2026-09-05: it
    # invented one). Callers that want only active goals filter on status.
    goals = session.execute(select(Goal).where(Goal.user_id == user_id).order_by(Goal.created_at)).scalars().all()
    goal_views = []
    for g in goals:
        pct = round(min(100.0, emergency / g.target_amount_paise * 100), 1) if g.target_amount_paise else 0.0
        goal_views.append({
            "goal_id": g.id, "label": g.label, "target_paise": g.target_amount_paise,
            "current_paise": emergency,  # derived: the emergency bucket IS the goal's progress
            "remaining_paise": max(0, g.target_amount_paise - emergency),
            "pct_complete": pct, "cadence": g.cadence, "status": g.status.value,
        })

    pending = [
        {"intent_id": i.id, "type": i.type.value, "amount_paise": i.amount_paise,
         "purpose": i.purpose, "status": i.status.value,
         "needs_your_approval": i.status.value == "AWAITING_APPROVAL"}
        for i in money_action_service.list_pending(session, user_id)
    ]

    return {
        "user": {"user_id": user.id, "name": user.name, "status": user.status.value,
                 "is_synthetic": user.is_synthetic},
        "currency": config.CURRENCY,
        "balances_paise": balances,
        "spending_this_month": spending,
        "goals": goal_views,
        "policy": None if policy is None else {
            "monthly_limit_paise": policy.monthly_limit_paise,
            "approval_threshold_paise": policy.approval_threshold_paise,
            "per_tx_limit_paise": policy.per_tx_limit_paise,
            "protected_buckets": policy.protected_buckets,
            "paused": policy.paused,
        },
        "pending_actions": pending,
        # Agentic Card (Phase 6b): just the counters the nav bell and the tab
        # badge need on every poll. The Card screen reads the full view from
        # /api/card/{user_id}; this keeps the common case one request.
        "card": {
            "unread_notifications": int(session.execute(
                select(func.count(Notification.id)).where(
                    Notification.user_id == user_id, Notification.read.is_(False))
            ).scalar_one()),
            "rules_watching": int(session.execute(
                select(func.count(PurchaseRule.id)).where(
                    PurchaseRule.user_id == user_id, PurchaseRule.status == RuleStatus.ACTIVE)
            ).scalar_one()),
            "rules_awaiting_you": int(session.execute(
                select(func.count(PurchaseRule.id)).where(
                    PurchaseRule.user_id == user_id, PurchaseRule.status == RuleStatus.AWAITING_APPROVAL)
            ).scalar_one()),
            "rules_blocked": int(session.execute(
                select(func.count(PurchaseRule.id)).where(
                    PurchaseRule.user_id == user_id, PurchaseRule.status == RuleStatus.BLOCKED)
            ).scalar_one()),
        },
        "pool": pool_service.cycle_summary(session, user_id),
        "rewards": reward_service.list_for_user(session, user_id),
        "recent_events": [
            {"event_id": e.id, "type": e.type.value, "amount_paise": e.amount_paise,
             "bucket": e.bucket.value, "source": e.source, "at": e.created_at.isoformat()}
            for e in ledger_service.get_recent_events(session, user_id, limit=10)
        ],
        "demo_notice": "ALL DATA IS SYNTHETIC. Payments run in Razorpay Test Mode only.",
    }
