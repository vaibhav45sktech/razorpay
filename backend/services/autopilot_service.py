"""Autopilot — the agent that LEADS instead of waiting to be asked.

Everything here is deterministic code over verified state (the same numbers
/api/state shows). The model is not involved: the plan, the projection and
the draw-round recommendation are arithmetic and rules, so they are
explainable, testable and benchmarkable. The user's only job is to agree.

Three views:
  monthly_plan()   - what to contribute this month, whether it is done, what
                     the policy engine says about it, and when the goal lands.
  pool_view()      - the cycle as a timeline of rounds, who draws when, and
                     which round THIS user should request given their needs.
  request_round()  - records the user's request as a PoolAllocation with the
                     recommendation's reasons as its written justification.
  simulate_draw()  - (DEBUG only) the policy-gated TEST_PAYOUT for that
                     allocation, settled with evidence marked simulated.

Nothing here moves money on its own. A contribution still goes through an
intent + Razorpay checkout; a draw still goes through the policy engine,
which authorises a payout only against an explainable allocation.
"""

from __future__ import annotations

import dataclasses
import math
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from backend import config
from backend.models.entities import (
    ActionIntent, AllocationStatus, AuditActor, Bucket, Goal, GoalStatus, IntentStatus, LedgerEvent,
    LedgerEventType, Need, PoolAllocation, PoolCycle, PoolCycleStatus, User,
)
from backend.services import ledger_service, policy_engine, pool_service
from backend.services import money_action_service as mas


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def _ym(dt: datetime) -> str:
    return dt.strftime("%Y-%m")


def _add_months(ym: str, n: int) -> str:
    y, m = int(ym[:4]), int(ym[5:7])
    m0 = m - 1 + n
    return f"{y + m0 // 12:04d}-{m0 % 12 + 1:02d}"


def _months_between(a: str, b: str) -> int:
    """b - a in whole months (b >= a)."""
    return (int(b[:4]) - int(a[:4])) * 12 + (int(b[5:7]) - int(a[5:7]))


def _month_label(ym: str) -> str:
    return datetime(int(ym[:4]), int(ym[5:7]), 1).strftime("%b %Y")


def _rs(paise: int) -> str:
    return f"₹{paise / 100:,.0f}" if paise % 100 == 0 else f"₹{paise / 100:,.2f}"


def _active_goal(session: Session, user_id: str) -> Goal | None:
    return session.execute(
        select(Goal).where(Goal.user_id == user_id, Goal.status == GoalStatus.ACTIVE).order_by(Goal.created_at)
    ).scalars().first()


def _contributed_since(session: Session, user_id: str, since: datetime) -> int:
    return int(session.execute(
        select(func.coalesce(func.sum(LedgerEvent.amount_paise), 0)).where(
            LedgerEvent.user_id == user_id, LedgerEvent.type == LedgerEventType.CONTRIBUTION,
            LedgerEvent.created_at >= since.replace(tzinfo=None) if False else LedgerEvent.created_at >= since,
        )
    ).scalar_one())


def _pending_contribution(session: Session, user_id: str) -> ActionIntent | None:
    rows = session.execute(
        select(ActionIntent).where(
            ActionIntent.user_id == user_id, ActionIntent.type == mas.IntentType.CONTRIBUTION,
            ActionIntent.status.in_([IntentStatus.ALLOWED, IntentStatus.APPROVED, IntentStatus.EXECUTING,
                                     IntentStatus.AWAITING_APPROVAL]),
        ).order_by(ActionIntent.created_at.desc())
    ).scalars().all()
    return rows[0] if rows else None


# --------------------------------------------------------------------------
# 1. Monthly plan
# --------------------------------------------------------------------------

def monthly_plan(session: Session, user_id: str, *, now: datetime | None = None) -> dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    if session.get(User, user_id) is None:
        raise LookupError(user_id)
    cfg = policy_engine.load_config()
    goal = _active_goal(session, user_id)
    saved = ledger_service.get_balance(session, user_id, Bucket.EMERGENCY_SAVINGS)
    reasons: list[str] = []

    lo, hi = cfg.contribution_min_paise, cfg.contribution_max_paise
    if goal is None:
        recommended = lo
        remaining = 0
        reasons.append("No active savings goal, so the plan keeps the habit at the minimum contribution.")
    else:
        remaining = max(0, goal.target_amount_paise - saved)
        recommended = lo if remaining <= 0 else min(hi, max(lo, remaining))
        reasons.append(f"Goal '{goal.label}' targets {_rs(goal.target_amount_paise)}; {_rs(saved)} saved, {_rs(remaining)} to go.")
        if remaining > hi:
            reasons.append(f"Capped at the maximum single contribution of {_rs(hi)} (PRD contribution band {_rs(lo)}–{_rs(hi)}).")
        elif 0 < remaining < lo:
            reasons.append(f"Rounded up to the minimum contribution of {_rs(lo)}.")
        elif remaining <= 0:
            reasons.append("Goal reached — recommending the minimum to keep the streak alive.")

    # Pool membership raises the floor to the cycle's contribution.
    pool = pool_service.cycle_summary(session, user_id)
    if pool and pool["contribution_amount_paise"] > recommended and pool["contribution_amount_paise"] <= hi:
        recommended = pool["contribution_amount_paise"]
        reasons.append(f"You're in '{pool['label']}', which asks {_rs(recommended)} per round.")

    month_start = ledger_service.current_month_start()
    done = _contributed_since(session, user_id, month_start)
    pending = _pending_contribution(session, user_id)
    status = "done" if done >= recommended else ("pending" if pending else "due")

    months_to_goal = None
    goal_month = None
    if goal is not None and remaining > 0:
        months_to_goal = math.ceil(remaining / recommended)
        goal_month = _add_months(_ym(now), months_to_goal)
        reasons.append(f"At {_rs(recommended)} a month the goal lands around {_month_label(goal_month)}.")

    policy = policy_engine.check_policy(
        session, user_id=user_id, action="CONTRIBUTION", amount_paise=recommended,
        purpose=f"savings_goal:{goal.id if goal else 'none'}",
    ).as_dict()

    if status == "done":
        headline = f"This month is covered — {_rs(done)} already in. Nothing to do."
    elif status == "pending":
        headline = f"A {_rs(pending.amount_paise)} contribution is waiting for your payment."
    else:
        headline = f"Put in {_rs(recommended)} this month."

    return {
        "month": _ym(now), "status": status, "headline": headline,
        "recommended_paise": recommended, "contributed_this_month_paise": done,
        "goal": None if goal is None else {
            "goal_id": goal.id, "label": goal.label, "target_paise": goal.target_amount_paise,
            "saved_paise": saved, "remaining_paise": remaining,
            "months_to_goal": months_to_goal, "goal_month": goal_month,
        },
        "pending_intent": None if pending is None else {"intent_id": pending.id, "amount_paise": pending.amount_paise, "status": pending.status.value},
        "policy_preview": policy, "reasons": reasons,
        "band": {"min_paise": lo, "max_paise": hi},
        "demo_notice": "Synthetic demo. Payments run in Razorpay Test Mode only.",
    }


def agree_to_plan(session: Session, user_id: str) -> dict[str, Any]:
    """The one tap. Creates the CONTRIBUTION intent for the recommended
    amount (a structured USER action, not the model's) and returns it; the
    frontend then runs the normal execute -> Razorpay Checkout flow."""
    plan = monthly_plan(session, user_id)
    if plan["status"] == "done":
        raise ValueError("this month's contribution is already complete")
    if plan["pending_intent"]:
        return {"intent_id": plan["pending_intent"]["intent_id"], "status": plan["pending_intent"]["status"],
                "amount_paise": plan["pending_intent"]["amount_paise"], "reused": True}
    goal_id = plan["goal"]["goal_id"] if plan["goal"] else "none"
    r = mas.create(
        session, user_id=user_id, action="CONTRIBUTION", amount_paise=plan["recommended_paise"],
        purpose=f"savings_goal:{goal_id}", actor=AuditActor.USER,
    )
    d = r.as_dict()
    return {"intent_id": d["intent_id"], "status": d["status"], "amount_paise": d["amount_paise"], "reused": False,
            "policy": d.get("policy")}


# --------------------------------------------------------------------------
# 2. Needs
# --------------------------------------------------------------------------

def list_needs(session: Session, user_id: str) -> list[dict[str, Any]]:
    rows = session.execute(select(Need).where(Need.user_id == user_id).order_by(Need.month, Need.created_at)).scalars().all()
    return [{"need_id": n.id, "label": n.label, "month": n.month, "amount_paise": n.amount_paise, "category": n.category} for n in rows]


def add_need(session: Session, user_id: str, *, label: str, month: str, amount_paise: int, category: str | None = None) -> dict[str, Any]:
    if session.get(User, user_id) is None:
        raise LookupError(user_id)
    n = Need(user_id=user_id, label=label.strip()[:120], month=month, amount_paise=int(amount_paise), category=category)
    session.add(n); session.flush()
    return {"need_id": n.id, "label": n.label, "month": n.month, "amount_paise": n.amount_paise, "category": n.category}


def delete_need(session: Session, user_id: str, need_id: str) -> None:
    n = session.get(Need, need_id)
    if n is None or n.user_id != user_id:
        raise LookupError(need_id)
    session.delete(n); session.flush()


# --------------------------------------------------------------------------
# 3. Pool timeline + recommendation
# --------------------------------------------------------------------------

def _member_label(session: Session, member_id: str, me: str) -> str:
    if member_id == me:
        return "You"
    u = session.get(User, member_id)
    if u is not None:
        return u.name
    tail = member_id.rsplit("_", 1)[-1]
    return f"Member {tail} (demo)"


def _my_draw_allocation(session: Session, user_id: str, cycle: PoolCycle) -> PoolAllocation | None:
    amount = cycle.size * cycle.contribution_amount_paise
    rows = session.execute(
        select(PoolAllocation).where(
            PoolAllocation.cycle_id == cycle.id, PoolAllocation.user_id == user_id,
            PoolAllocation.amount_paise == amount,
        ).order_by(PoolAllocation.created_at.desc())
    ).scalars().all()
    return rows[0] if rows else None


def pool_view(session: Session, user_id: str, *, now: datetime | None = None) -> dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    if session.get(User, user_id) is None:
        raise LookupError(user_id)
    summary = pool_service.cycle_summary(session, user_id)
    if summary is None:
        return {"in_pool": False, "message": "You're not in a savings cycle yet."}
    cycle = session.get(PoolCycle, summary["cycle_id"])
    members: list[str] = list(cycle.members or [])
    round_amount = cycle.size * cycle.contribution_amount_paise
    start_ym = _ym(cycle.created_at if cycle.created_at.tzinfo else cycle.created_at.replace(tzinfo=timezone.utc))
    this_ym = _ym(now)

    mine = _my_draw_allocation(session, user_id, cycle)
    my_round_month = None
    if mine is not None and "round:" in (mine.reason or ""):
        my_round_month = mine.reason.split("round:", 1)[1][:7]

    # Draw order: past rounds went to synthetic members in list order (simulated
    # history); future rounds are open. The user's own round is wherever they
    # requested it, else the recommendation.
    rounds: list[dict[str, Any]] = []
    others = [m for m in members if m != user_id]
    for i in range(cycle.size):
        ym = _add_months(start_ym, i)
        past = _months_between(this_ym, ym) < 0 if ym < this_ym else False
        rounds.append({"index": i + 1, "month": ym, "label": _month_label(ym), "amount_paise": round_amount,
                       "past": ym < this_ym, "current": ym == this_ym,
                       "drawer": _member_label(session, others[i % len(others)], user_id) if ym < this_ym else None,
                       "status": "drawn" if ym < this_ym else "open"})

    # ---- recommendation: the latest open round on/before the first month the
    # user's needs outrun their projected savings; else the last round.
    plan = monthly_plan(session, user_id, now=now)
    monthly = plan["recommended_paise"]
    saved_now = ledger_service.get_balance(session, user_id, Bucket.EMERGENCY_SAVINGS)
    needs = list_needs(session, user_id)
    reasons: list[str] = []
    open_rounds = [r for r in rounds if not r["past"]]
    if not open_rounds:
        rec = None
        reasons.append("Every round of this cycle has already been drawn.")
    else:
        shortfall_month = None
        cumulative_needs = 0
        for n in sorted(needs, key=lambda x: x["month"]):
            if n["month"] < this_ym:
                continue
            months_ahead = _months_between(this_ym, n["month"])
            projected = saved_now + monthly * months_ahead
            cumulative_needs += n["amount_paise"]
            if cumulative_needs > projected:
                shortfall_month = n["month"]
                reasons.append(
                    f"By {_month_label(n['month'])} you'll need {_rs(cumulative_needs)} ('{n['label']}' and earlier needs) "
                    f"but will have saved about {_rs(projected)} at {_rs(monthly)} a month — a gap of {_rs(cumulative_needs - projected)}."
                )
                break
        if shortfall_month is None:
            rec = open_rounds[-1]
            if needs:
                reasons.append("Your savings cover every need you've listed, so there's no reason to draw early.")
            else:
                reasons.append("You haven't listed any upcoming needs yet, so the plan assumes you don't need early access.")
            reasons.append(f"Taking the last round ({rec['label']}) keeps your contribution streak — and the consistency reward — intact.")
        else:
            candidates = [r for r in open_rounds if r["month"] <= shortfall_month]
            rec = candidates[-1] if candidates else open_rounds[0]
            reasons.append(
                f"Drawing in {rec['label']} puts {_rs(round_amount)} in hand just before that gap, without idling money for months you don't need it."
            )
    if rec is not None:
        rec_month = rec["month"]
        for r in rounds:
            if r["month"] == rec_month:
                r["recommended"] = True
            if my_round_month and r["month"] == my_round_month:
                r["requested_by_you"] = True
                r["drawer"] = "You"
                r["status"] = "requested"

    my_alloc_view = None if mine is None else {
        "allocation_id": mine.id, "status": mine.status.value, "amount_paise": mine.amount_paise,
        "round_month": my_round_month, "reason": mine.reason,
    }
    return {
        "in_pool": True, "cycle": summary, "round_amount_paise": round_amount,
        "rounds": rounds, "this_month": this_ym,
        "recommendation": None if rec is None else {"month": rec["month"], "label": rec["label"], "amount_paise": round_amount, "reasons": reasons},
        "my_draw": my_alloc_view,
        "benefits": [a for a in summary["my_allocations"] if a["amount_paise"] != round_amount],
        "needs": needs,
        "assumed_monthly_contribution_paise": monthly,
        "saved_now_paise": saved_now,
        "can_simulate_draw": bool(config.DEBUG),
    }


def request_round(session: Session, user_id: str, *, month: str) -> dict[str, Any]:
    """Record the user's request. The allocation's `reason` is the written,
    human-readable justification the policy engine will later show verbatim
    when authorising the payout (PRD s4.1)."""
    view = pool_view(session, user_id)
    if not view["in_pool"]:
        raise ValueError("not in a pool")
    target = next((r for r in view["rounds"] if r["month"] == month), None)
    if target is None or target["past"]:
        raise ValueError("that round is not open")
    cycle = session.get(PoolCycle, view["cycle"]["cycle_id"])
    amount = view["round_amount_paise"]

    # Replace any earlier unpaid request for this cycle.
    for a in session.execute(select(PoolAllocation).where(
        PoolAllocation.cycle_id == cycle.id, PoolAllocation.user_id == user_id, PoolAllocation.amount_paise == amount,
        PoolAllocation.status.in_([AllocationStatus.PROPOSED, AllocationStatus.CONFIRMED]),
    )).scalars().all():
        a.status = AllocationStatus.CANCELLED

    rec = view["recommendation"]
    why = " ".join(rec["reasons"]) if rec and rec["month"] == month else "Chosen by the member against the agent's recommendation."
    reason = (
        f"Member requested the {target['label']} draw (round:{month}) of {_rs(amount)} from '{cycle.label}'. "
        f"Agent's assessment: {why} DEMO — simulated cycle, no pooled money."
    )
    alloc = PoolAllocation(cycle_id=cycle.id, user_id=user_id, amount_paise=amount, reason=reason,
                           status=AllocationStatus.CONFIRMED)
    session.add(alloc); session.flush()
    return {"allocation_id": alloc.id, "round_month": month, "amount_paise": amount, "status": alloc.status.value,
            "reason": reason, "followed_recommendation": bool(rec and rec["month"] == month)}


def simulate_draw(session: Session, user_id: str) -> dict[str, Any]:
    """DEBUG only: run the requested draw through the real policy-gated
    TEST_PAYOUT path and settle it with evidence marked simulated. Refused
    (by the policy engine, not by us) if no allocation authorises it."""
    if not config.DEBUG:
        raise PermissionError("simulated payouts are available only in DEBUG mode")
    view = pool_view(session, user_id)
    if not view["in_pool"] or not view["my_draw"]:
        raise ValueError("no requested draw to pay out")
    amount = view["round_amount_paise"]
    r = mas.create(session, user_id=user_id, action="TEST_PAYOUT", amount_paise=amount,
                   purpose=f"pool_payout:{view['cycle']['cycle_id']}:{view['my_draw']['round_month']}", actor=AuditActor.SYSTEM)
    d = r.as_dict()
    if d["status"] != IntentStatus.ALLOWED.value:
        return {"executed": False, "intent_id": d["intent_id"], "status": d["status"], "policy": d.get("policy")}
    intent = mas.get(session, d["intent_id"])
    mas.begin_execution(session, intent, evidence={"simulated": True, "provider": "none"})
    mas.settle_success(session, intent, provider_evidence={"simulated": True, "provider": "none", "id": f"sim_{intent.id}"},
                       source=f"pool_payout:simulated:{intent.id}", actor=AuditActor.SYSTEM)
    return {"executed": True, "intent_id": intent.id, "status": intent.status.value, "amount_paise": amount,
            "note": "SIMULATED payout. The policy engine authorised it against your allocation; no real transfer occurred."}
