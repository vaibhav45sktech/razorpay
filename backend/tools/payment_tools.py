"""create_payment_intent — the ceiling of the LLM's power (HLD s2.9).

This tool can cause an ActionIntent ROW to exist. Nothing more. It cannot
move money: money_action_service.create() only ever walks
PROPOSED -> POLICY_CHECK -> (ALLOWED | AWAITING_APPROVAL | DENIED -> CLOSED).
The only functions that may ever post a ledger entry are settle_success /
settle_failure (Phase 3), reached exclusively via a verified webhook, a
verified checkout signature, or (before Phase 5) the DEBUG-only fake
settler — never from this tool, never from anything an LLM can request.

TEST_PAYOUT is deliberately not reachable through this tool at all (see
CreateIntentArgs's docstring) — only PURCHASE and CONTRIBUTION are.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from backend.models.entities import AuditActor
from backend.models.schemas import CreateIntentArgs, CreateIntentOut
from backend.services import money_action_service


def create_payment_intent(session: Session, user_id: str, args: CreateIntentArgs) -> CreateIntentOut:
    result = money_action_service.create(
        session,
        user_id=user_id,
        action=args.action,
        amount_paise=args.amount_paise,
        purpose=args.purpose,
        bucket=args.bucket,
        actor=AuditActor.LLM,
    )
    return CreateIntentOut(**result.as_dict())
