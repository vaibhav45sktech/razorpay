"""Money action intents: the state machine and its read helpers.

PHASE 2 SCOPE: only the read-side helpers the policy engine needs. The
transition table, create(), execute() and settle_success() arrive in Phase 3.
They are deliberately not stubbed here - a stub that "works" is how half-built
state machines end up in demos.

The one design decision this file already makes is WHICH intent statuses count
as committed spend, because the policy engine's monthly-limit check depends on
it (PRD s4.3: "track committed + completed spend").
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from backend.models.entities import ActionIntent, IntentStatus, IntentType

#: Statuses in which an intent has RESERVED money that has not yet reached the
#: ledger. The policy engine adds these to settled spend when checking limits.
#:
#: Included on purpose:
#:   NEEDS_APPROVAL / AWAITING_APPROVAL - not yet approved, but counted, so a
#:       user cannot stack five in-limit purchases awaiting approval and then
#:       approve them all. A denied approval closes the intent and releases it.
#:   UNKNOWN / EXCEPTION - the provider's answer is unclear. Counting them is
#:       the conservative choice: an ambiguous payment might have succeeded.
#:
#: Excluded on purpose:
#:   PROPOSED / POLICY_CHECK - transient, not yet authorised, nothing committed.
#:   DENIED / FAILURE / CLOSED - nothing will move.
#:   LEDGER_UPDATED - already in the ledger; counting it again would double.
PENDING_STATUSES: frozenset[IntentStatus] = frozenset(
    {
        IntentStatus.ALLOWED,
        IntentStatus.APPROVED,
        IntentStatus.EXECUTING,
        IntentStatus.SUCCESS,
        IntentStatus.VERIFIED,
        IntentStatus.UNKNOWN,
        IntentStatus.EXCEPTION,
        IntentStatus.NEEDS_APPROVAL,
        IntentStatus.AWAITING_APPROVAL,
    }
)


def committed_pending_paise(
    session: Session, user_id: str, *, intent_type: IntentType = IntentType.PURCHASE
) -> int:
    """Sum of amounts reserved by unsettled intents of one type."""
    return int(
        session.execute(
            select(func.coalesce(func.sum(ActionIntent.amount_paise), 0)).where(
                ActionIntent.user_id == user_id,
                ActionIntent.type == intent_type,
                ActionIntent.status.in_(PENDING_STATUSES),
            )
        ).scalar_one()
    )


def count_pending(session: Session, user_id: str) -> int:
    """Number of unsettled intents of ANY type. Backs the pending-cap velocity rule."""
    return int(
        session.execute(
            select(func.count(ActionIntent.id)).where(
                ActionIntent.user_id == user_id,
                ActionIntent.status.in_(PENDING_STATUSES),
            )
        ).scalar_one()
    )


def count_created_since(session: Session, user_id: str, since: datetime) -> int:
    """Number of intents of ANY type created at or after `since`. Backs velocity limits."""
    return int(
        session.execute(
            select(func.count(ActionIntent.id)).where(
                ActionIntent.user_id == user_id,
                ActionIntent.created_at >= since,
            )
        ).scalar_one()
    )
