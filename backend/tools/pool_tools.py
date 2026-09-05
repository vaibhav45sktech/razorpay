"""get_pool_status — the user's own view of their community pool cycle.

Reuses pool_service.cycle_summary exactly, the same function GET /api/state
calls, so the agent's numbers and the UI's numbers can never diverge (HLD
s2.2). This never reports a pooled balance because there is none: PRD s4.1,
enforced structurally by test_pool_invariant.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy.orm import Session

from backend.models.schemas import GetAutopilotPlanOut, GetPoolStatusOut, NoArgs
from backend.services import pool_service

if TYPE_CHECKING:
    # Imported for annotations only. The handlers below import these at call
    # time to keep this module's import graph free of the Agentic Card service,
    # but an annotation still has to name something resolvable.
    from backend.models.schemas import (
        CreatePurchaseRuleArgs, CreatePurchaseRuleOut, GetAgentCardOut,
    )


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


def get_agent_card(session: Session, user_id: str, args: NoArgs) -> "GetAgentCardOut":
    """Read-only view of the Agentic Card (Phase 6b) so the chat can explain
    it: limits, catalogue prices, the user's rules with the monitor's latest
    checks, and unread notifications. The model cannot fire, approve or pay."""
    from backend.models.schemas import GetAgentCardOut
    from backend.services import agent_card_service as card

    v = card.card_view(session, user_id)
    products = [{"product_id": p["product_id"], "name": p["name"], "category": p["category"],
                 "list_price_paise": p["list_price_paise"],
                 "best_price_paise": p["best"]["price_paise"] if p["best"] else None,
                 "best_platform": p["best"]["platform_label"] if p["best"] else None,
                 "in_stock": bool(p["best"])} for p in v["products"]]
    rules = []
    for r in v["rules"]:
        le = r.get("last_eval") or {}
        rules.append({"rule_id": r["rule_id"], "product": r["product"]["name"], "status": r["status"],
                      "target_price_paise": r["target_price_paise"], "approval_mode": r["approval_mode"],
                      "conditions": [{"rule": c["label"], "value": c["value"]} for c in r["conditions"]],
                      "last_check": {"price_seen_paise": (le.get("price") or {}).get("price_paise"),
                                     "price_met": (le.get("price") or {}).get("met"),
                                     "conditions_met": [c["met"] for c in le.get("checks", [])],
                                     "why_not_yet": None if le.get("all_met") else
                                     "; ".join([le.get("price", {}).get("detail", "")] +
                                               [c["detail"] for c in le.get("checks", []) if not c["met"]]).strip("; "),
                                     "blocked_reason": le.get("blocked_reason")} if le else None,
                      "seconds_left_to_answer": r.get("seconds_left"), "result": r.get("result")})
    unread = [{"kind": n["kind"], "title": n["title"], "body": n["body"], "actionable": n["actionable"]}
              for n in v["notifications"]["items"] if not n["read"]][:5]
    c = v["card"]
    return GetAgentCardOut(
        card={"last4": c["last4"], "frozen": c["frozen"], "monthly_cap_paise": c["monthly_limit_paise"],
              "per_purchase_cap_paise": c["per_tx_limit_paise"], "ask_me_above_paise": c["approval_threshold_paise"],
              "spent_this_month_paise": c["spent_this_month_paise"], "headroom_paise": c["headroom_paise"]},
        products=products, rules=rules, unread_notifications=unread,
        note=("Rules are evaluated by deterministic code on a synthetic price feed. When one fires, the policy "
              "engine decides and the user taps YES on the Card screen; you can explain, not act."),
    )


def create_purchase_rule(session: Session, user_id: str, args: "CreatePurchaseRuleArgs") -> "CreatePurchaseRuleOut":
    """Create a watch rule from the chat. Only the user's own numbers go in
    (the args schema is grammar-constrained); nothing is bought here."""
    from backend.models.entities import AuditActor
    from backend.models.schemas import CreatePurchaseRuleOut
    from backend.services import agent_card_service as card

    conds = []
    if args.only_after_date:
        conds.append({"type": "date_after", "value": args.only_after_date})
    if args.min_discount_pct is not None:
        conds.append({"type": "min_discount_pct", "value": args.min_discount_pct})
    r = card.create_rule(session, user_id, product_id=args.product_id, target_price_paise=args.target_price_paise,
                         conditions=conds, approval_mode=args.approval_mode, actor=AuditActor.LLM)
    le = r.get("last_eval") or {}
    return CreatePurchaseRuleOut(
        rule_id=r["rule_id"], status=r["status"], product=r["product"]["name"],
        target_price_paise=r["target_price_paise"],
        conditions=[{"rule": c["label"], "value": c["value"]} for c in r["conditions"]],
        approval_mode=r["approval_mode"],
        last_check={"price_seen_paise": (le.get("price") or {}).get("price_paise"), "all_met": le.get("all_met")} if le else None,
        what_happens_next=("A RULE was saved; nothing was bought. The card checks the price every few seconds. When the "
                           "target and every condition hold, the policy engine decides and the user gets an in-app "
                           "notification to tap YES (or, in auto mode, an in-limit purchase settles by itself). "
                           "Tell the user it is now watching and what it saw just now."),
    )
