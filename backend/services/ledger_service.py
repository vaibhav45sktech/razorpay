"""The append-only financial ledger. The source of truth for all money state.

DESIGN RULE, and the reason this module looks the way it does:

    There is no update function. There is no delete function. There is no
    set_balance function. The append-only guarantee holds because the
    capability to violate it does not exist here - not because a future
    developer remembers to be careful.

Corrections are made by APPENDING a REVERSAL event (see append_reversal), the
way a real double-entry ledger works. History is never edited, so the trail
always explains how the current balance came to be.

BALANCES ARE DERIVED. No balance is stored anywhere in the schema; every read
sums the events. That is slightly more work per query and completely immune to
the class of bug where a cached or stored balance drifts from its events.

SIGN CONVENTION: amount_paise is signed. Positive credits the bucket, negative
debits it. A contribution to savings is +50000; a purchase is -40000.

TRANSACTIONS: functions here flush but never commit. The caller owns the
transaction boundary (see models.db.session_scope), so a failure mid-way rolls
back the whole financial operation rather than leaving half of it applied.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from backend.models.entities import (
    AuditActor,
    Bucket,
    LedgerEvent,
    LedgerEventType,
)
from backend.services import audit_service

logger = logging.getLogger("campuspool.ledger")


class LedgerError(Exception):
    """Raised when a ledger operation is invalid.

    Deliberately loud (Playbook A.4): a rejected financial write must surface,
    never be swallowed and silently skipped.
    """


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------


def append(
    session: Session,
    *,
    user_id: str,
    type: LedgerEventType,
    amount_paise: int,
    bucket: Bucket,
    source: str,
    intent_id: str | None = None,
) -> LedgerEvent:
    """Append one financial event. The only way money state ever changes.

    Args:
        user_id: whose ledger this belongs to.
        type: what kind of movement (CONTRIBUTION, PURCHASE, REWARD, ...).
        amount_paise: SIGNED integer paise. Positive credits, negative debits.
        bucket: which bucket the amount moves in or out of.
        source: provenance, e.g. "razorpay_payment:pay_abc", "seed:opening".
            Required, because an event nobody can trace is not auditable.
        intent_id: the ActionIntent that authorised this, when there was one.

    Raises:
        LedgerError: on a zero amount, a non-integer amount, or a blank source.
    """
    if not isinstance(amount_paise, int) or isinstance(amount_paise, bool):
        raise LedgerError(
            f"amount_paise must be an int of paise, got {type_name(amount_paise)}. "
            "Money is never a float in this codebase."
        )
    if amount_paise == 0:
        raise LedgerError("A zero-amount ledger event is always a bug; refusing to append.")
    if not source or not source.strip():
        raise LedgerError("source is required: an untraceable ledger event is not auditable.")

    event = LedgerEvent(
        user_id=user_id,
        type=type,
        amount_paise=amount_paise,
        bucket=bucket,
        source=source.strip(),
        intent_id=intent_id,
    )
    session.add(event)
    session.flush()

    # Every money movement is traceable (PRD s6). The audit entry is part of
    # the same transaction, so a rolled-back append leaves no orphan trail.
    audit_service.write(
        session,
        actor=AuditActor.BACKEND,
        action=f"ledger_append:{type.value}",
        user_id=user_id,
        intent_id=intent_id,
        inputs={
            "amount_paise": amount_paise,
            "bucket": bucket.value,
            "source": source,
        },
    )

    logger.info(
        "ledger append user=%s %s %+d %s (%s)",
        user_id,
        type.value,
        amount_paise,
        bucket.value,
        source,
    )
    return event


def type_name(value: object) -> str:
    """Readable type name for error messages."""
    return type(value).__name__


def append_reversal(
    session: Session,
    *,
    original_event_id: str,
    reason: str,
    intent_id: str | None = None,
) -> LedgerEvent:
    """Reverse an earlier event by appending its opposite. Never edits history.

    This is the clawback / dispute / chargeback path (see
    CampusPool_Production_Readiness.md s1.3). The original row stays exactly as
    written; the reversal sits beside it, and the derived balance nets to the
    corrected figure.

    Raises:
        LedgerError: if the original does not exist, is itself a reversal, or
            has already been reversed. Double-reversal would silently re-credit
            money, so it is refused rather than deduplicated.
    """
    original = session.get(LedgerEvent, original_event_id)
    if original is None:
        raise LedgerError(f"Cannot reverse unknown ledger event {original_event_id!r}.")

    if original.type is LedgerEventType.REVERSAL:
        raise LedgerError(
            f"Event {original_event_id!r} is itself a reversal; reversing a reversal "
            "would re-apply the original. Append a fresh corrective event instead."
        )

    reversal_source = f"reversal:{original.id}"
    already = session.execute(
        select(LedgerEvent).where(
            LedgerEvent.type == LedgerEventType.REVERSAL,
            LedgerEvent.source == reversal_source,
        )
    ).scalar_one_or_none()
    if already is not None:
        raise LedgerError(
            f"Event {original_event_id!r} was already reversed by {already.id!r}; "
            "refusing to reverse twice."
        )

    reversal = append(
        session,
        user_id=original.user_id,
        type=LedgerEventType.REVERSAL,
        amount_paise=-original.amount_paise,
        bucket=original.bucket,
        source=reversal_source,
        intent_id=intent_id or original.intent_id,
    )

    audit_service.write(
        session,
        actor=AuditActor.BACKEND,
        action="ledger_reversal",
        user_id=original.user_id,
        intent_id=reversal.intent_id,
        inputs={"original_event_id": original.id, "reason": reason},
    )
    logger.info("ledger reversal of %s (%s)", original.id, reason)
    return reversal


# ---------------------------------------------------------------------------
# Reading — every balance is derived here, never stored
# ---------------------------------------------------------------------------


def get_balance(session: Session, user_id: str, bucket: Bucket) -> int:
    """Current balance of one bucket, in paise. Zero when there are no events.

    coalesce matters: SUM over no rows returns NULL, and a balance of None
    would propagate into arithmetic as a crash or, worse, a wrong number.
    """
    return int(
        session.execute(
            select(func.coalesce(func.sum(LedgerEvent.amount_paise), 0)).where(
                LedgerEvent.user_id == user_id,
                LedgerEvent.bucket == bucket,
            )
        ).scalar_one()
    )


def get_balances(session: Session, user_id: str) -> dict[str, int]:
    """All bucket balances in one query, keyed by bucket value.

    Every bucket is present even with no events, so callers never have to
    handle a missing key. The agent's observe() step reads this every turn.
    """
    rows = session.execute(
        select(LedgerEvent.bucket, func.coalesce(func.sum(LedgerEvent.amount_paise), 0))
        .where(LedgerEvent.user_id == user_id)
        .group_by(LedgerEvent.bucket)
    ).all()

    balances = {bucket.value: 0 for bucket in Bucket}
    for bucket, total in rows:
        key = bucket.value if hasattr(bucket, "value") else str(bucket)
        balances[key] = int(total)
    return balances


def _current_month_start() -> datetime:
    """First instant of the current UTC calendar month."""
    now = datetime.now(timezone.utc)
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def month_spend(session: Session, user_id: str, bucket: Bucket) -> int:
    """Money spent OUT of a bucket this calendar month, as a POSITIVE number.

    Counts only debits (negative events); contributions and rewards arriving
    this month must not offset spending, or a user could contribute their way
    around a spending limit. The policy engine relies on this.
    """
    total = session.execute(
        select(func.coalesce(func.sum(LedgerEvent.amount_paise), 0)).where(
            LedgerEvent.user_id == user_id,
            LedgerEvent.bucket == bucket,
            LedgerEvent.amount_paise < 0,
            LedgerEvent.created_at >= _current_month_start(),
        )
    ).scalar_one()
    return abs(int(total))


def get_recent_events(session: Session, user_id: str, limit: int = 20) -> list[LedgerEvent]:
    """Most recent events first. Backs the get_transactions agent tool."""
    return list(
        session.execute(
            select(LedgerEvent)
            .where(LedgerEvent.user_id == user_id)
            .order_by(LedgerEvent.created_at.desc(), LedgerEvent.id.desc())
            .limit(limit)
        )
        .scalars()
        .all()
    )


def get_event_count(session: Session, user_id: str) -> int:
    """Number of events for a user. Used by reward eligibility rules."""
    return int(
        session.execute(
            select(func.count(LedgerEvent.id)).where(LedgerEvent.user_id == user_id)
        ).scalar_one()
    )
