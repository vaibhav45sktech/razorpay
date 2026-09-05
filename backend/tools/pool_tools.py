"""get_pool_status — the user's own view of their community pool cycle.

Reuses pool_service.cycle_summary exactly, the same function GET /api/state
calls, so the agent's numbers and the UI's numbers can never diverge (HLD
s2.2). This never reports a pooled balance because there is none: PRD s4.1,
enforced structurally by test_pool_invariant.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from backend.models.schemas import GetAutopilotPlanOut, GetPoolStatusOut, NoArgs
from backend.services import pool_service


def get_pool_status(session: Session, user_id: str, args: NoArgs) -> GetPoolStatusOut:
    summary = pool_service.cycle_summary(session, user_id)
    return GetPoolStatusOut(
        in_a_cycle=summary is not None,
        cycle=summary,
        note="Simulated. No money is pooled; every member keeps an individual ledger.",
    )


def get_autopilot_plan(session: Session, user_id: str, args: NoArgs) -> GetAutopilotPlanOut:
    """Read-only view of the Autopilot's current decisions (Phase 6): the
    month's proposed contribution with its reasons, the recommended pool
    draw round with its reasons, and the needs the user has listed. Lets the
    chat agent answer "why this month?" from the same deterministic source
    the screen uses. Rupee conversion happens in the orchestrator's
    rupee_view like every other tool result."""
    from backend.services import autopilot_service as ap

    plan = ap.monthly_plan(session, user_id)
    this_month = {
        "month": plan["month_label"], "status": plan["status"], "headline": plan["headline"],
        "proposed_contribution_paise": plan["recommended_paise"],
        "contributed_this_month_paise": plan["contributed_this_month_paise"],
        "goal": plan["goal"], "reasons": plan["reasons"],
    }
    pool = ap.pool_view(session, user_id)
    pool_draw = None
    if pool.get("in_pool"):
        rec = pool.get("recommendation")
        mine = pool.get("my_draw")
        pool_draw = {
            "cycle": pool["cycle"]["label"],
            "round_amount_paise": pool["round_amount_paise"],
            "recommended_round": None if rec is None else {"month": rec["label"], "amount_paise": rec["amount_paise"], "reasons": rec["reasons"]},
            "your_requested_round": None if mine is None else {"month": mine["round_month"], "status": mine["status"]},
            "open_rounds": [r["label"] for r in pool["rounds"] if not r["past"]],
        }
    return GetAutopilotPlanOut(
        this_month=this_month, pool_draw=pool_draw, upcoming_needs=pool.get("needs", []),
        note="Computed by deterministic rules over the ledger; the user acts on it from the Autopilot screen, not from chat.",
    )
