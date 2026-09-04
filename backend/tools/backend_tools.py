"""Backend-only tools (HLD s2.3): create_razorpay_payment, get_payment_status,
process_test_payout.

Registered now so the tool registry's caller enforcement has something
concrete to protect — the single most important safety claim in this system
is that these three names are structurally ABSENT from
agent.tool_registry.llm_visible_tools(), and that only holds if they exist
in TOOLS at all (see the master build plan Phase 4 Step 2 test).

Their real implementations are Phase 5 work (Razorpay Test Mode) and do not
exist yet. Nothing in Phase 4 calls them: there is no LLM path (they are not
LLM-visible) and no backend path either (no webhook/execute-intent endpoint
exists until Phase 5). Calling one today is a bug, not a normal runtime
condition, hence NotImplementedError rather than a soft error return.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from backend.models.schemas import (
    CreateRazorpayPaymentArgs,
    CreateRazorpayPaymentOut,
    GetPaymentStatusArgs,
    GetPaymentStatusOut,
    ProcessTestPayoutArgs,
    ProcessTestPayoutOut,
)

_NOT_YET = "Phase 5 (Razorpay Test Mode) has not been built yet; this is a backend-only stub."


def create_razorpay_payment(
    session: Session, user_id: str, args: CreateRazorpayPaymentArgs
) -> CreateRazorpayPaymentOut:
    raise NotImplementedError(_NOT_YET)


def get_payment_status(session: Session, user_id: str, args: GetPaymentStatusArgs) -> GetPaymentStatusOut:
    raise NotImplementedError(_NOT_YET)


def process_test_payout(session: Session, user_id: str, args: ProcessTestPayoutArgs) -> ProcessTestPayoutOut:
    raise NotImplementedError(_NOT_YET)
