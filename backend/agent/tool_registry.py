"""name -> (args schema, output schema, handler, caller_permission) — HLD s2.3.

This registry is THE safety mechanism behind "the model cannot request what
it cannot see": llm_visible_tools() filters to Caller.LLM before anything is
ever shown to the model (in the tool catalog text and in the enum of names
agent/llm_client.decide() is grammar-constrained to). A tool registered here
with Caller.BACKEND or Caller.SYSTEM is structurally absent from that filter
— not merely disallowed at call time, but never nameable by the model at all.

Playbook rule (Phase 4, Step 2): creating any tool not represented here
requires writing its args schema, output schema and permitted caller down in
models/schemas.py first. See that module for the full contract.
"""

from __future__ import annotations

import dataclasses
from enum import Enum
from typing import Any, Callable

from pydantic import BaseModel
from sqlalchemy.orm import Session

from backend.models import schemas as s
from backend.tools import (
    backend_tools,
    ledger_tools,
    offer_tools,
    payment_tools,
    policy_tools,
    pool_tools,
    profile_tools,
    savings_tools,
)


class Caller(str, Enum):
    LLM = "llm"
    BACKEND = "backend"
    SYSTEM = "system"


HandlerFn = Callable[[Session, str, BaseModel], BaseModel]


@dataclasses.dataclass(frozen=True)
class ToolDef:
    name: str
    description: str
    args_schema: type[BaseModel]
    output_schema: type[BaseModel]
    handler: HandlerFn
    caller: Caller

    def args_json_schema(self) -> dict[str, Any]:
        """The JSON schema agent/llm_client.py constrains Ollama's `format`
        parameter to when filling in this tool's arguments (Phase 4 Pre-Build
        Research Brief: constrained decoding against the tool's own Pydantic
        schema, not Ollama's native `tools` field)."""
        return self.args_schema.model_json_schema()


def _write_audit_event_stub(session: Session, user_id: str, args: BaseModel) -> BaseModel:
    raise NotImplementedError(
        "write_audit_event is not dispatched through the tool loop; it is "
        "audit_service.write(), called directly by the orchestrator and "
        "services on every tool call and every state transition. This entry "
        "exists only for documentation parity with the HLD s2.3 table."
    )


TOOLS: dict[str, ToolDef] = {
    "get_user_profile": ToolDef(
        name="get_user_profile",
        description=(
            "Get this user's profile: name, account status, and their own spending rules."
        ),
        args_schema=s.NoArgs,
        output_schema=s.GetUserProfileOut,
        handler=profile_tools.get_user_profile,
        caller=Caller.LLM,
    ),
    "get_wallet_or_ledger": ToolDef(
        name="get_wallet_or_ledger",
        description=(
            "Get this user's current verified balances (emergency savings, rewards), "
            "this month's spending against their limit, and their most recent ledger "
            "events. Always call this before stating any balance or spending figure."
        ),
        args_schema=s.NoArgs,
        output_schema=s.GetWalletOrLedgerOut,
        handler=ledger_tools.get_wallet_or_ledger,
        caller=Caller.LLM,
    ),
    "get_transactions": ToolDef(
        name="get_transactions",
        description=(
            "Get a categorized summary (by type and by bucket) of this user's past "
            "ledger transactions, for this month or all time."
        ),
        args_schema=s.GetTransactionsArgs,
        output_schema=s.GetTransactionsOut,
        handler=ledger_tools.get_transactions,
        caller=Caller.LLM,
    ),
    "calculate_safe_contribution": ToolDef(
        name="calculate_safe_contribution",
        description=(
            "Get a deterministic recommended contribution amount (in paise) toward one "
            "of this user's savings goals, with the reasons behind the number."
        ),
        args_schema=s.CalculateSafeContributionArgs,
        output_schema=s.CalculateSafeContributionOut,
        handler=savings_tools.calculate_safe_contribution,
        caller=Caller.LLM,
    ),
    "get_pool_status": ToolDef(
        name="get_pool_status",
        description=(
            "Get this user's community pool cycle status: rules, membership size, and "
            "their own explained allocations, if they are in a cycle. No pooled balance "
            "exists — every member keeps an individual ledger."
        ),
        args_schema=s.NoArgs,
        output_schema=s.GetPoolStatusOut,
        handler=pool_tools.get_pool_status,
        caller=Caller.LLM,
    ),
    "get_autopilot_plan": ToolDef(
        name="get_autopilot_plan",
        description=(
            "Get what the Autopilot has already decided for this user: this month's proposed "
            "contribution and why, the recommended pool draw round and why, and the upcoming "
            "needs they listed. Use it to explain the plan — e.g. 'why this month for my draw?' "
            "You cannot change the plan; the user acts on it from the Autopilot screen."
        ),
        args_schema=s.NoArgs,
        output_schema=s.GetAutopilotPlanOut,
        handler=pool_tools.get_autopilot_plan,
        caller=Caller.LLM,
    ),
    "get_agent_card": ToolDef(
        name="get_agent_card",
        description=(
            "Get this user's Agent Card: its limits (monthly cap, per-purchase cap, 'ask me above' line, frozen), "
            "the demo product catalogue with current prices, the user's purchase rules with what the price monitor "
            "last checked and why a rule has or hasn't fired, and unread notifications. Call this for anything "
            "about the card, a watched product, a price, or 'why hasn't it bought yet'."
        ),
        args_schema=s.NoArgs,
        output_schema=s.GetAgentCardOut,
        handler=pool_tools.get_agent_card,
        caller=Caller.LLM,
    ),
    "create_purchase_rule": ToolDef(
        name="create_purchase_rule",
        description=(
            "Set a watch rule on the Agent Card: buy a catalogue product when its price is at or below the "
            "user's target, optionally only after a date or only at a minimum discount. Creates a RULE only - "
            "no purchase, no payment. Use ONLY when the user explicitly asks to watch/track/auto-buy something "
            "and has named the product and the target price. Get the product_id from get_agent_card first."
        ),
        args_schema=s.CreatePurchaseRuleArgs,
        output_schema=s.CreatePurchaseRuleOut,
        handler=pool_tools.create_purchase_rule,
        caller=Caller.LLM,
    ),
    "get_eligible_rewards": ToolDef(
        name="get_eligible_rewards",
        description=(
            "Get this user's milestone/streak rewards, grouped by eligible, locked and "
            "redeemed, with the facts behind each status."
        ),
        args_schema=s.GetEligibleRewardsArgs,
        output_schema=s.GetEligibleRewardsOut,
        handler=offer_tools.get_eligible_rewards,
        caller=Caller.LLM,
    ),
    "get_offers": ToolDef(
        name="get_offers",
        description=(
            "Get synthetic partner offers this user is eligible for, optionally "
            "filtered by category or budget, ranked by savings. These are promotions "
            "from partners, not financial advice — always say so when recommending one."
        ),
        args_schema=s.GetOffersArgs,
        output_schema=s.GetOffersOut,
        handler=offer_tools.get_offers,
        caller=Caller.LLM,
    ),
    "check_policy": ToolDef(
        name="check_policy",
        description=(
            "Check whether a proposed PURCHASE or CONTRIBUTION would be ALLOW, DENY, "
            "or REQUIRE_APPROVAL, with the reason. Always call this BEFORE proposing "
            "any payment, contribution, or purchase. Amounts are in rupees, as the user said them."
        ),
        args_schema=s.CheckPolicyArgs,
        output_schema=s.CheckPolicyOut,
        handler=policy_tools.check_policy,
        caller=Caller.LLM,
    ),
    "create_payment_intent": ToolDef(
        name="create_payment_intent",
        description=(
            "Propose a PURCHASE or CONTRIBUTION money action. This only creates an "
            "internal pending record and runs it through policy — it never moves real "
            "money and never guarantees success. Only call this after check_policy has "
            "returned ALLOW for the same amount and purpose."
        ),
        args_schema=s.CreateIntentArgs,
        output_schema=s.CreateIntentOut,
        handler=payment_tools.create_payment_intent,
        caller=Caller.LLM,
    ),
    "update_goal": ToolDef(
        name="update_goal",
        description=(
            "Pause or resume one of this user's savings goals. ONLY when the user explicitly asks to "
            "pause/resume a goal - never as part of answering a question."
        ),
        args_schema=s.UpdateGoalArgs,
        output_schema=s.UpdateGoalOut,
        handler=savings_tools.update_goal,
        caller=Caller.LLM,
    ),
    # ---- Backend only: structurally invisible to the model (Phase 5 stubs) ----
    "create_razorpay_payment": ToolDef(
        name="create_razorpay_payment",
        description="(Backend only, Phase 5.) Create a real Razorpay order for an already-allowed intent.",
        args_schema=s.CreateRazorpayPaymentArgs,
        output_schema=s.CreateRazorpayPaymentOut,
        handler=backend_tools.create_razorpay_payment,
        caller=Caller.BACKEND,
    ),
    "get_payment_status": ToolDef(
        name="get_payment_status",
        description="(Backend only, Phase 5.) Fetch the authoritative status of a Razorpay payment.",
        args_schema=s.GetPaymentStatusArgs,
        output_schema=s.GetPaymentStatusOut,
        handler=backend_tools.get_payment_status,
        caller=Caller.BACKEND,
    ),
    "process_test_payout": ToolDef(
        name="process_test_payout",
        description=(
            "(Backend only, policy-gated, Phase 5.) Execute a RazorpayX test payout "
            "authorised by a pool allocation."
        ),
        args_schema=s.ProcessTestPayoutArgs,
        output_schema=s.ProcessTestPayoutOut,
        handler=backend_tools.process_test_payout,
        caller=Caller.BACKEND,
    ),
    # ---- System service (auto): never dispatched through the tool loop ----
    "write_audit_event": ToolDef(
        name="write_audit_event",
        description=(
            "(System only.) Documentation parity with the HLD table; this is "
            "audit_service.write(), called directly everywhere, never requested by anyone."
        ),
        args_schema=s.WriteAuditEventArgs,
        output_schema=s.WriteAuditEventOut,
        handler=_write_audit_event_stub,
        caller=Caller.SYSTEM,
    ),
}


def llm_visible_tools() -> list[ToolDef]:
    """Only tools with Caller.LLM are ever serialized toward the model.

    Backend-only and system tools are invisible to the model — it cannot
    even name them, because they never appear in the tool catalog text or
    in the enum this drives in agent/llm_client.py."""
    return [t for t in TOOLS.values() if t.caller is Caller.LLM]


def get(name: str) -> ToolDef | None:
    return TOOLS.get(name)
