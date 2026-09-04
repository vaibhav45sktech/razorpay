"""Pydantic schemas for every agent tool (HLD s2.3), written before any handler.

Playbook rule: creating any tool not represented here requires writing its
args schema, its output schema, and its permitted caller down first. This
file is that contract, frozen ahead of agent/tool_registry.py and every
tools/*.py handler.

DESIGN NOTE — why "args" schema, not "input" schema
-----------------------------------------------------
The HLD s2.3 table lists `user_id` as part of every tool's input. In this
build user_id is ALWAYS injected server-side from the session and is NEVER
part of what the model supplies or sees (Playbook non-negotiable, master
build plan Phase 4 Step 6). Rather than merge user_id into one Pydantic model
and strip it back out when building the model-visible JSON schema, every
"*Args" model below holds ONLY the fields the model may actually supply.
Handlers take (session, user_id, args) as three separate parameters. This
keeps "what the model can see" and "what the code needs" structurally
distinct instead of relying on a convention to hide a field.

DESIGN NOTE — why args schemas double as the LLM's structured-output contract
-------------------------------------------------------------------------------
Per the Phase 4 Pre-Build Research Brief (Sept 2026), Ollama's native `tools`
field is not schema-guaranteed: it just inlines a tool's schema into the
prompt as text. This build never sends `tools`; instead agent/llm_client.py
uses Ollama's `format` parameter (real grammar-constrained decoding) with
each Args model's own `model_json_schema()` to force the model's arguments
to be shaped correctly at the token level. That is the reason these models
are kept flat and simple (str / int / float / bool / literal / optional) —
a schema llama.cpp's grammar compiler can represent cleanly is also a schema
a small local model can reliably fill in.

Output schemas here are documentation and test scaffolding, not an external
input boundary: every value they carry comes from our own trusted service
code (ledger, policy engine, pool/reward services), never from the model.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

# ---------------------------------------------------------------------------
# Shared building blocks
# ---------------------------------------------------------------------------


class NoArgs(BaseModel):
    """The model supplies nothing; user_id (injected server-side) is enough.

    Used by every tool whose only real input, per the HLD table, is user_id.
    """

    model_config = ConfigDict(extra="forbid")


class ToolOutput(BaseModel):
    """Base class for output schemas. `extra="allow"` because these describe
    our own trusted return values (documentation + test scaffolding), not an
    external boundary that needs strict rejection."""

    model_config = ConfigDict(extra="allow")


# ---------------------------------------------------------------------------
# get_user_profile
# ---------------------------------------------------------------------------


class GetUserProfileOut(ToolOutput):
    user_id: str
    name: str
    status: str
    is_synthetic: bool
    spend_policy: dict[str, Any] | None
    flags: dict[str, Any]


# ---------------------------------------------------------------------------
# get_wallet_or_ledger
# ---------------------------------------------------------------------------


class GetWalletOrLedgerOut(ToolOutput):
    balances_paise: dict[str, int]
    spending_this_month: dict[str, Any] | None
    reserved_pending_paise: int
    recent_events: list[dict[str, Any]]


# ---------------------------------------------------------------------------
# get_transactions
# ---------------------------------------------------------------------------


class GetTransactionsArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    period: Literal["this_month", "all"] = Field(
        "this_month",
        description="Which window of transactions to summarize.",
    )


class GetTransactionsOut(ToolOutput):
    period: str
    event_count: int
    total_by_type: dict[str, int]
    total_by_bucket: dict[str, int]
    events: list[dict[str, Any]]


# ---------------------------------------------------------------------------
# calculate_safe_contribution
# ---------------------------------------------------------------------------


class CalculateSafeContributionArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    goal_id: str = Field(..., description="The goal to recommend a contribution for.")


class CalculateSafeContributionOut(ToolOutput):
    goal_id: str
    recommended_amount_paise: int
    min_paise: int
    max_paise: int
    reasons: list[str]


# ---------------------------------------------------------------------------
# get_pool_status
# ---------------------------------------------------------------------------


class GetPoolStatusOut(ToolOutput):
    in_a_cycle: bool
    cycle: dict[str, Any] | None
    note: str


# ---------------------------------------------------------------------------
# get_eligible_rewards
# ---------------------------------------------------------------------------


class GetEligibleRewardsArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Reserved for future ranking use (e.g. "what can I redeem toward X?").
    # PRD does not define how context should influence ranking yet.
    # TODO: confirm ranking-by-context behaviour with product owner.
    context: str | None = Field(
        None, description="Optional free-text hint about what the user is asking for."
    )


class GetEligibleRewardsOut(ToolOutput):
    eligible: list[dict[str, Any]]
    locked: list[dict[str, Any]]
    redeemed: list[dict[str, Any]]


# ---------------------------------------------------------------------------
# get_offers
# ---------------------------------------------------------------------------


class GetOffersArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    intent: str | None = Field(None, description="Free-text: what the user wants to buy.")
    category: str | None = Field(None, description="Offer category, e.g. 'electronics', 'food'.")
    budget_paise: int | None = Field(None, ge=0, description="Optional budget ceiling in paise.")


class GetOffersOut(ToolOutput):
    offers: list[dict[str, Any]]
    note: str


# ---------------------------------------------------------------------------
# check_policy
# ---------------------------------------------------------------------------


# Shared field descriptions for the two model-visible money-shaped tools.
# Written for a 7B model reading them as prompt text (see agent/prompts.py
# render_tool_catalog): the manual adversarial run on 2026-09-04 showed
# qwen2.5:7b-instruct sending 5,000,000 paise for "₹5,000" and choosing
# TEST_PAYOUT for a purchase when these were terser, so the descriptions now
# spell the conversion and the action choice out explicitly.
_ACTION_DESC = (
    "PURCHASE = the user wants to buy or spend on something. "
    "CONTRIBUTION = the user wants to add money to their savings."
)
_AMOUNT_DESC = (
    "Amount as a positive integer number of PAISE, never rupees. "
    "1 rupee = 100 paise, so ₹300 = 30000 and ₹5,000 = 500000."
)
_BUCKET_DESC = (
    "For PURCHASE only: which bucket the money would leave. Leave null to use "
    "discretionary. Never choose emergency_savings for spending — it is protected."
)


class CheckPolicyArgs(BaseModel):
    """The model may only ask about the two actions it can also propose
    through create_payment_intent. TEST_PAYOUT exists in policy_engine for
    the backend-only payout path (Phase 5) and is deliberately not offered
    here: offering it only gave the model a wrong branch to pick.
    """

    model_config = ConfigDict(extra="forbid")

    action: Literal["PURCHASE", "CONTRIBUTION"] = Field(..., description=_ACTION_DESC)
    amount_paise: int = Field(..., gt=0, description=_AMOUNT_DESC)
    purpose: str = Field(
        ..., min_length=1,
        description="Short structured purpose, e.g. 'purchase:laptop_bag' or 'savings_goal:gol_abc'.",
    )
    bucket: Literal["emergency_savings", "discretionary", "rewards"] | None = Field(None, description=_BUCKET_DESC)


class CheckPolicyOut(ToolOutput):
    decision: Literal["ALLOW", "DENY", "REQUIRE_APPROVAL"]
    reason: str
    rule: str
    details: dict[str, Any]


# ---------------------------------------------------------------------------
# create_payment_intent
# ---------------------------------------------------------------------------


class CreateIntentArgs(BaseModel):
    """DEVIATION FROM THE HLD SKETCH, noted deliberately: HLD s2.9's sketch of
    create_payment_intent's input omits an action/type field entirely, but the
    real money_action_service.create() (Phases 1-3) requires one to know which
    state-machine rules and policy branch apply. TEST_PAYOUT is deliberately
    excluded here — HLD s2.3 marks process_test_payout backend-only and
    policy-gated, so a payout must never be reachable through an LLM-visible
    tool, only through a backend-only path (Phase 5).
    """

    model_config = ConfigDict(extra="forbid")

    action: Literal["PURCHASE", "CONTRIBUTION"] = Field(..., description=_ACTION_DESC)
    amount_paise: int = Field(..., gt=0, description=_AMOUNT_DESC)
    purpose: str = Field(
        ..., min_length=1,
        description="Short structured purpose, e.g. 'purchase:laptop_bag' or 'savings_goal:gol_abc'. "
        "Use the SAME purpose you passed to check_policy.",
    )
    bucket: Literal["emergency_savings", "discretionary", "rewards"] | None = Field(None, description=_BUCKET_DESC)


class CreateIntentOut(ToolOutput):
    intent_id: str
    status: str
    duplicate: bool
    policy: dict[str, Any] | None
    amount_paise: int
    type: str
    purpose: str


# ---------------------------------------------------------------------------
# update_goal
# ---------------------------------------------------------------------------


class UpdateGoalArgs(BaseModel):
    """PRD does not define the full vocabulary of goal "events". Only pause
    and resume are implemented here; anything else is an honest
    "unsupported_event" refusal rather than a guess.
    TODO: confirm the rest of the event vocabulary with product owner.
    """

    model_config = ConfigDict(extra="forbid")

    goal_id: str = Field(..., description="The goal to update.")
    event: Literal["pause", "resume"] = Field(..., description="The lifecycle event to apply.")


class UpdateGoalOut(ToolOutput):
    goal_id: str
    status: str


# ---------------------------------------------------------------------------
# Backend-only tools (HLD s2.3) — schemas exist so the contract is documented
# and the registry's caller enforcement has something concrete to protect.
# Handlers are NotImplemented until Phase 5 (Razorpay Test Mode); no LLM path
# and no Phase 4 backend path can reach them yet.
# ---------------------------------------------------------------------------


class CreateRazorpayPaymentArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    intent_id: str


class CreateRazorpayPaymentOut(ToolOutput):
    order_ref: str | None
    error: str | None


class GetPaymentStatusArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    payment_id: str


class GetPaymentStatusOut(ToolOutput):
    status: str


class ProcessTestPayoutArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    recipient: str
    amount_paise: int = Field(..., gt=0)
    reason: str


class ProcessTestPayoutOut(ToolOutput):
    payout_ref: str | None
    status: str


# ---------------------------------------------------------------------------
# write_audit_event — System service (auto). Listed for documentation parity
# with the HLD table only. It is never dispatched through the tool loop: it
# is audit_service.write(), called directly by orchestrator/services on every
# tool call (including refused ones) and every state transition, not
# something any caller "requests" as a tool.
# ---------------------------------------------------------------------------


class WriteAuditEventArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event: dict[str, Any]


class WriteAuditEventOut(ToolOutput):
    audit_id: str
