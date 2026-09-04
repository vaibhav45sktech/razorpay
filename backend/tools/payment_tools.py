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


_NEXT_BY_STATUS = {
    "ALLOWED": (
        "An intent has been RECORDED only. Nothing has been paid or moved. The user must "
        "confirm and complete the payment in the app before anything happens. Tell the "
        "user it is pending their confirmation."
    ),
    "AWAITING_APPROVAL": (
        "Nothing has been paid or moved. This amount is above the approval threshold, so the "
        "user must explicitly approve it in the app first. Tell the user it needs their approval."
    ),
    "DENIED": "Policy denied this. Nothing was created that can be executed. Tell the user why.",
    "CLOSED": "Policy denied this. Nothing was created that can be executed. Tell the user why.",
}


def _what_happens_next(status: str, duplicate: bool) -> str:
    text = _NEXT_BY_STATUS.get(status, "Nothing has been paid or moved. Report the status to the user as-is.")
    if duplicate:
        text = "This is the SAME intent as before (no second one was created). " + text
    return text


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
    data = result.as_dict()
    return CreateIntentOut(**data, what_happens_next=_what_happens_next(data["status"], data["duplicate"]))
