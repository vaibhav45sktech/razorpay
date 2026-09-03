"""Money action intents: the state machine that every money movement passes through.

An ActionIntent is the ceiling of the LLM's power. The agent may cause an intent
ROW to exist; nothing more. From there, every step to a real effect is this
module's deterministic code, and the only things that may mark an intent
successful are a verified provider response (Phase 5) or - in development
only - the explicitly DEBUG-gated fake settler.

STATE MACHINE (PRD s5.5 / HLD s2.5)

    PROPOSED -> POLICY_CHECK -+-> DENIED ---------------------------------> CLOSED
                              +-> NEEDS_APPROVAL -> AWAITING_APPROVAL -+-> APPROVED -> EXECUTING
                              |                                        +-> CLOSED  (denied/expired)
                              +-> ALLOWED ----------------------------------> EXECUTING

    EXECUTING -+-> SUCCESS -> VERIFIED -> LEDGER_UPDATED
               +-> FAILURE -> CLOSED
               +-> UNKNOWN -+-> VERIFIED -> LEDGER_UPDATED     (reconciliation found it captured)
                            +-> FAILURE -> CLOSED              (reconciliation found it failed)
                            +-> EXCEPTION -+-> VERIFIED ...    (human review resolved it)
                                           +-> CLOSED

Two completions of the HLD table, both noted here so they are not mistaken for
drift: UNKNOWN -> FAILURE (reconciliation can discover a failure, not only a
success) and EXCEPTION -> VERIFIED | CLOSED (the exception queue must have a
way out once a human decides).

IDEMPOTENCY (Guardrail 4, PRD s9 case D "Pay Rs.800 again")
    A deterministic base_ref = hash(user | purpose | amount | period) identifies
    "this logical action this period". If a LIVE intent (pending or completed)
    exists for it, create() returns that intent with duplicate=True and makes
    nothing new. If only CLOSED intents exist (denied, failed, approval refused)
    the user may try again: a retry suffix keeps client_ref unique while the
    base stays recognisable. Without that distinction, one failed payment would
    lock the user out of retrying for the rest of the month.
"""

from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from backend.models.entities import (
    ActionIntent,
    Approval,
    ApprovalStatus,
    AuditActor,
    Bucket,
    IntentStatus,
    IntentType,
    LedgerEventType,
    PolicyDecision,
    User,
)
from backend.services import audit_service, ledger_service, policy_engine, pool_service, reward_service

logger = logging.getLogger("campuspool.money")


# ---------------------------------------------------------------------------
# Errors - loud by design (Playbook A.4)
# ---------------------------------------------------------------------------


class IllegalTransition(Exception):
    """A state change the machine does not permit. Always a bug or an attack."""

    def __init__(self, intent_id: str, current: IntentStatus, requested: IntentStatus) -> None:
        self.intent_id, self.current, self.requested = intent_id, current, requested
        super().__init__(
            f"Intent {intent_id}: illegal transition {current.value} -> {requested.value}"
        )


class IntentNotFound(Exception):
    pass


class NotPermitted(Exception):
    """An action refused for authorisation reasons (wrong user, wrong state)."""


# ---------------------------------------------------------------------------
# The transition table - the single source of truth for what may follow what
# ---------------------------------------------------------------------------

S = IntentStatus

LEGAL: dict[IntentStatus, frozenset[IntentStatus]] = {
    S.PROPOSED: frozenset({S.POLICY_CHECK}),
    S.POLICY_CHECK: frozenset({S.DENIED, S.NEEDS_APPROVAL, S.ALLOWED}),
    S.DENIED: frozenset({S.CLOSED}),
    S.NEEDS_APPROVAL: frozenset({S.AWAITING_APPROVAL}),
    S.AWAITING_APPROVAL: frozenset({S.APPROVED, S.CLOSED}),
    S.APPROVED: frozenset({S.EXECUTING}),
    S.ALLOWED: frozenset({S.EXECUTING}),
    S.EXECUTING: frozenset({S.SUCCESS, S.FAILURE, S.UNKNOWN}),
    S.SUCCESS: frozenset({S.VERIFIED}),
    S.VERIFIED: frozenset({S.LEDGER_UPDATED}),
    S.UNKNOWN: frozenset({S.VERIFIED, S.FAILURE, S.EXCEPTION}),
    S.EXCEPTION: frozenset({S.VERIFIED, S.CLOSED}),
    S.FAILURE: frozenset({S.CLOSED}),
    S.LEDGER_UPDATED: frozenset(),  # terminal
    S.CLOSED: frozenset(),          # terminal
}

TERMINAL_STATUSES: frozenset[IntentStatus] = frozenset(s for s, nxt in LEGAL.items() if not nxt)

#: Statuses in which an intent has RESERVED money that has not yet reached the
#: ledger. The policy engine adds these to settled spend when checking limits.
#:
#: Included on purpose:
#:   NEEDS_APPROVAL / AWAITING_APPROVAL - not yet approved, but counted, so a
#:       user cannot stack five in-limit purchases awaiting approval and then
#:       approve them all. A refused approval closes the intent and releases it.
#:   UNKNOWN / EXCEPTION - the provider's answer is unclear. Counting them is
#:       the conservative choice: an ambiguous payment might have succeeded.
#: Excluded on purpose:
#:   PROPOSED / POLICY_CHECK - transient, not yet authorised, nothing committed.
#:   DENIED / FAILURE / CLOSED - nothing will move.
#:   LEDGER_UPDATED - already in the ledger; counting it again would double.
PENDING_STATUSES: frozenset[IntentStatus] = frozenset(
    {S.ALLOWED, S.APPROVED, S.EXECUTING, S.SUCCESS, S.VERIFIED, S.UNKNOWN, S.EXCEPTION,
     S.NEEDS_APPROVAL, S.AWAITING_APPROVAL}
)

#: Statuses that make a later identical request a DUPLICATE rather than a retry.
LIVE_STATUSES: frozenset[IntentStatus] = PENDING_STATUSES | {S.LEDGER_UPDATED}

#: Statuses from which settlement may proceed (see settle_success).
SETTLEABLE_FROM: frozenset[IntentStatus] = frozenset({S.EXECUTING, S.UNKNOWN, S.EXCEPTION})


def transition(
    session: Session,
    intent: ActionIntent,
    to: IntentStatus,
    *,
    evidence: dict[str, Any] | None = None,
    actor: AuditActor = AuditActor.BACKEND,
) -> ActionIntent:
    """Move an intent to a new state, or raise. Every legal move is audited.

    This is the ONLY place intent.status may be assigned. Grep for
    `.status =` outside this function and you have found a bug.
    """
    current = intent.status
    if to not in LEGAL[current]:
        raise IllegalTransition(intent.id, current, to)

    audit_service.write(
        session,
        actor=actor,
        action=f"intent:{current.value}->{to.value}",
        user_id=intent.user_id,
        intent_id=intent.id,
        provider_result=evidence,
    )
    intent.status = to
    intent.updated_at = datetime.now(timezone.utc)
    session.flush()
    logger.info("intent %s %s -> %s", intent.id, current.value, to.value)
    return intent


# ---------------------------------------------------------------------------
# Read helpers (used by the policy engine)
# ---------------------------------------------------------------------------


def get(session: Session, intent_id: str) -> ActionIntent:
    intent = session.get(ActionIntent, intent_id)
    if intent is None:
        raise IntentNotFound(intent_id)
    return intent


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
    return int(
        session.execute(
            select(func.count(ActionIntent.id)).where(
                ActionIntent.user_id == user_id, ActionIntent.status.in_(PENDING_STATUSES)
            )
        ).scalar_one()
    )


def count_created_since(session: Session, user_id: str, since: datetime) -> int:
    return int(
        session.execute(
            select(func.count(ActionIntent.id)).where(
                ActionIntent.user_id == user_id, ActionIntent.created_at >= since
            )
        ).scalar_one()
    )


def list_pending(session: Session, user_id: str) -> list[ActionIntent]:
    return list(
        session.execute(
            select(ActionIntent)
            .where(ActionIntent.user_id == user_id, ActionIntent.status.in_(PENDING_STATUSES))
            .order_by(ActionIntent.created_at.desc())
        ).scalars().all()
    )


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------


def current_period() -> str:
    """Idempotency period. Monthly, matching the contribution cadence and the
    monthly spending limit: the same purchase requested twice in one month is
    a duplicate; next month it is a new decision."""
    return datetime.now(timezone.utc).strftime("%Y-%m")


def base_ref(user_id: str, purpose: str, amount_paise: int, period: str | None = None) -> str:
    raw = f"{user_id}|{purpose.strip().lower()}|{amount_paise}|{period or current_period()}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:40]


def find_live_duplicate(session: Session, ref: str) -> ActionIntent | None:
    """The live (pending or completed) intent for this base_ref, if any."""
    return session.execute(
        select(ActionIntent)
        .where(ActionIntent.client_ref.like(f"{ref}%"), ActionIntent.status.in_(LIVE_STATUSES))
        .order_by(ActionIntent.created_at.desc())
        .limit(1)
    ).scalar_one_or_none()


def _next_client_ref(session: Session, ref: str) -> str:
    """Unique client_ref for a new attempt: the base, plus a retry suffix if
    earlier attempts were closed."""
    prior = int(
        session.execute(
            select(func.count(ActionIntent.id)).where(ActionIntent.client_ref.like(f"{ref}%"))
        ).scalar_one()
    )
    return ref if prior == 0 else f"{ref}#retry{prior}"


# ---------------------------------------------------------------------------
# create(): PROPOSED -> POLICY_CHECK -> (ALLOWED | AWAITING_APPROVAL | CLOSED)
# ---------------------------------------------------------------------------


class CreateResult:
    """What create() hands back: the intent, plus whether it was pre-existing."""

    def __init__(self, intent: ActionIntent, *, duplicate: bool, policy: policy_engine.PolicyResult | None) -> None:
        self.intent, self.duplicate, self.policy = intent, duplicate, policy

    def as_dict(self) -> dict[str, Any]:
        return {
            "intent_id": self.intent.id,
            "status": self.intent.status.value,
            "duplicate": self.duplicate,
            "policy": self.policy.as_dict() if self.policy else self.intent.policy_result,
            "amount_paise": self.intent.amount_paise,
            "type": self.intent.type.value,
            "purpose": self.intent.purpose,
        }


def create(
    session: Session,
    *,
    user_id: str,
    action: str | IntentType,
    amount_paise: int,
    purpose: str,
    bucket: Bucket | str | None = None,
    actor: AuditActor = AuditActor.BACKEND,
) -> CreateResult:
    """Propose a money action and run it through policy. Never executes anything.

    Policy is evaluated BEFORE the row is inserted so that velocity counts do
    not include the very request being evaluated (an off-by-one that would
    deny the Nth allowed action instead of the N+1th). The row then walks
    PROPOSED -> POLICY_CHECK -> outcome, so the audit trail shows the states.
    DENIED intents are persisted (and closed): unauthorised attempts must be
    traceable (PRD s6.1), and they count toward velocity limits, which is the
    right behaviour for a script hammering denied requests.
    """
    if session.get(User, user_id) is None:
        raise NotPermitted(f"unknown user {user_id!r}")

    try:
        intent_type = action if isinstance(action, IntentType) else IntentType(str(action).strip().upper())
    except ValueError:
        # Let the policy engine produce the DENY with its reason, but never
        # persist an intent of a type the schema does not know.
        result = policy_engine.check_policy(
            session, user_id=user_id, action=action, amount_paise=amount_paise, purpose=purpose, bucket=bucket
        )
        audit_service.write(session, actor=actor, action="intent_refused:unknown_action",
                            user_id=user_id, inputs={"action": str(action), "amount_paise": amount_paise,
                                                    "purpose": purpose}, policy_result=result.as_dict())
        raise NotPermitted(result.reason)

    ref = base_ref(user_id, purpose, amount_paise)
    existing = find_live_duplicate(session, ref)
    if existing is not None:
        audit_service.write(
            session, actor=actor, action="intent_duplicate_blocked", user_id=user_id,
            intent_id=existing.id,
            inputs={"purpose": purpose, "amount_paise": amount_paise, "existing_status": existing.status.value},
        )
        logger.info("duplicate blocked: %s already %s", existing.id, existing.status.value)
        return CreateResult(existing, duplicate=True, policy=None)

    # Pre-flight policy (row not yet inserted - see docstring).
    decision = policy_engine.check_policy(
        session, user_id=user_id, action=intent_type, amount_paise=amount_paise, purpose=purpose, bucket=bucket
    )

    intent = ActionIntent(
        user_id=user_id,
        type=intent_type,
        amount_paise=amount_paise,
        purpose=purpose,
        client_ref=_next_client_ref(session, ref),
        status=S.PROPOSED,
    )
    session.add(intent)
    session.flush()
    audit_service.write(session, actor=actor, action="intent_proposed", user_id=user_id, intent_id=intent.id,
                        inputs={"action": intent_type.value, "amount_paise": amount_paise, "purpose": purpose,
                                "bucket": str(bucket) if bucket else None})

    transition(session, intent, S.POLICY_CHECK, actor=actor)
    intent.policy_result = decision.as_dict()  # frozen: what was decided, when, on what numbers

    if decision.decision is PolicyDecision.ALLOW:
        transition(session, intent, S.ALLOWED, evidence=decision.as_dict(), actor=actor)
    elif decision.decision is PolicyDecision.REQUIRE_APPROVAL:
        transition(session, intent, S.NEEDS_APPROVAL, evidence=decision.as_dict(), actor=actor)
        session.add(Approval(user_id=user_id, intent_id=intent.id, status=ApprovalStatus.PENDING,
                             expires_at=None))  # TODO: confirm approval expiry window with product owner
        transition(session, intent, S.AWAITING_APPROVAL, actor=actor)
    else:
        transition(session, intent, S.DENIED, evidence=decision.as_dict(), actor=actor)
        transition(session, intent, S.CLOSED, actor=actor)

    session.flush()
    return CreateResult(intent, duplicate=False, policy=decision)


# ---------------------------------------------------------------------------
# Approval: a structured user action, never inferred from chat (PRD s5.4)
# ---------------------------------------------------------------------------


def _approval_row(session: Session, intent: ActionIntent) -> Approval:
    row = session.execute(select(Approval).where(Approval.intent_id == intent.id)).scalar_one_or_none()
    if row is None:
        raise NotPermitted(f"intent {intent.id} has no approval request")
    return row


def approve(session: Session, *, intent_id: str, user_id: str) -> ActionIntent:
    """Grant a REQUIRE_APPROVAL intent. Only the intent's own user may do so."""
    intent = get(session, intent_id)
    if intent.user_id != user_id:
        raise NotPermitted("only the account owner can approve this action")
    if intent.status is not S.AWAITING_APPROVAL:
        raise IllegalTransition(intent.id, intent.status, S.APPROVED)

    approval = _approval_row(session, intent)
    now = datetime.now(timezone.utc)
    if approval.expires_at is not None:
        exp = approval.expires_at if approval.expires_at.tzinfo else approval.expires_at.replace(tzinfo=timezone.utc)
        if exp < now:
            approval.status = ApprovalStatus.EXPIRED
            approval.decided_at = now
            transition(session, intent, S.CLOSED, evidence={"approval": "expired"}, actor=AuditActor.SYSTEM)
            raise NotPermitted("this approval request has expired; please ask again")

    approval.status = ApprovalStatus.GRANTED
    approval.decided_at = now
    transition(session, intent, S.APPROVED, evidence={"approval_id": approval.id}, actor=AuditActor.USER)
    return intent


def deny_approval(session: Session, *, intent_id: str, user_id: str) -> ActionIntent:
    """Refuse a REQUIRE_APPROVAL intent. Closes it and releases its reserved limit."""
    intent = get(session, intent_id)
    if intent.user_id != user_id:
        raise NotPermitted("only the account owner can decide this action")
    if intent.status is not S.AWAITING_APPROVAL:
        raise IllegalTransition(intent.id, intent.status, S.CLOSED)

    approval = _approval_row(session, intent)
    approval.status = ApprovalStatus.DENIED
    approval.decided_at = datetime.now(timezone.utc)
    transition(session, intent, S.CLOSED, evidence={"approval_id": approval.id, "approval": "denied"},
               actor=AuditActor.USER)
    return intent


# ---------------------------------------------------------------------------
# Execution boundary
# ---------------------------------------------------------------------------


def begin_execution(session: Session, intent: ActionIntent, *, evidence: dict[str, Any] | None = None) -> ActionIntent:
    """ALLOWED/APPROVED -> EXECUTING. The policy gate, enforced again by the
    transition table: nothing else can reach EXECUTING."""
    if intent.status not in (S.ALLOWED, S.APPROVED):
        raise IllegalTransition(intent.id, intent.status, S.EXECUTING)
    return transition(session, intent, S.EXECUTING, evidence=evidence)


def ledger_effect(intent: ActionIntent) -> tuple[LedgerEventType, Bucket, int]:
    """What settling this intent writes to the ledger: (type, bucket, signed paise).

    This mapping is the only place intent types meet buckets, and it is why
    "spend from emergency savings" is not merely denied by policy but
    UNREPRESENTABLE: no intent type debits EMERGENCY_SAVINGS.
    """
    if intent.type is IntentType.CONTRIBUTION:
        return LedgerEventType.CONTRIBUTION, Bucket.EMERGENCY_SAVINGS, +intent.amount_paise
    if intent.type is IntentType.PURCHASE:
        return LedgerEventType.PURCHASE, Bucket.DISCRETIONARY, -intent.amount_paise
    if intent.type is IntentType.TEST_PAYOUT:
        return LedgerEventType.POOL_PAYOUT, Bucket.REWARDS, +intent.amount_paise
    raise IllegalTransition(intent.id, intent.status, S.LEDGER_UPDATED)


def settle_success(
    session: Session,
    intent: ActionIntent,
    *,
    provider_evidence: dict[str, Any],
    source: str,
    actor: AuditActor = AuditActor.BACKEND,
) -> ActionIntent:
    """The single settlement path: EXECUTING -> SUCCESS -> VERIFIED -> ledger -> LEDGER_UPDATED.

    Written once here; the Phase 5 webhook, the checkout-verify route and the
    reconciliation job all call this same function. Idempotent: an intent that
    is already LEDGER_UPDATED returns unchanged, so a webhook arriving after
    the checkout fast-path (or twice) cannot double-credit.

    Args:
        provider_evidence: the authoritative record this settlement rests on
            (a Razorpay payment entity, or {"debug": ...} in development).
        source: ledger provenance, e.g. "razorpay_payment:pay_abc".
    """
    if intent.status is S.LEDGER_UPDATED:
        logger.info("settle_success: %s already settled; no-op", intent.id)
        return intent
    if intent.status not in SETTLEABLE_FROM:
        raise IllegalTransition(intent.id, intent.status, S.SUCCESS)

    if intent.status is S.EXECUTING:
        transition(session, intent, S.SUCCESS, evidence=provider_evidence, actor=actor)
    transition(session, intent, S.VERIFIED, evidence=provider_evidence, actor=actor)

    event_type, bucket, signed = ledger_effect(intent)
    ledger_service.append(
        session, user_id=intent.user_id, type=event_type, amount_paise=signed,
        bucket=bucket, source=source, intent_id=intent.id,
    )
    if intent.type is IntentType.TEST_PAYOUT:
        pool_service.mark_allocation_paid(session, user_id=intent.user_id, amount_paise=intent.amount_paise)

    reward_service.recompute_eligibility(session, intent.user_id)
    return transition(session, intent, S.LEDGER_UPDATED, actor=actor)


def settle_failure(
    session: Session,
    intent: ActionIntent,
    *,
    provider_evidence: dict[str, Any],
    actor: AuditActor = AuditActor.BACKEND,
) -> ActionIntent:
    """EXECUTING/UNKNOWN -> FAILURE -> CLOSED. The ledger is never touched
    (PRD s10: "Payment failed. Your savings goal was not increased")."""
    if intent.status not in (S.EXECUTING, S.UNKNOWN):
        raise IllegalTransition(intent.id, intent.status, S.FAILURE)
    transition(session, intent, S.FAILURE, evidence=provider_evidence, actor=actor)
    return transition(session, intent, S.CLOSED, actor=actor)


def mark_unknown(session: Session, intent: ActionIntent, *, evidence: dict[str, Any]) -> ActionIntent:
    """EXECUTING -> UNKNOWN: the provider's answer was ambiguous. Reconciliation resolves it."""
    return transition(session, intent, S.UNKNOWN, evidence=evidence)


def escalate_exception(session: Session, intent: ActionIntent, *, evidence: dict[str, Any]) -> ActionIntent:
    """UNKNOWN -> EXCEPTION: could not be reconciled; a human must decide. Never guessed."""
    return transition(session, intent, S.EXCEPTION, evidence=evidence, actor=AuditActor.SYSTEM)


# ---------------------------------------------------------------------------
# Reversal - the clawback / dispute path
# ---------------------------------------------------------------------------


def reverse_settled(session: Session, *, intent_id: str, reason: str, actor: AuditActor = AuditActor.BACKEND) -> ActionIntent:
    """Reverse a completed intent's ledger effect. History is never edited.

    The intent stays LEDGER_UPDATED - it DID complete - and the reversal is a
    new REVERSAL ledger event linked to the same intent, plus an audit entry.
    The PRD state machine defines no REVERSED state, and inventing one would be
    scope creep; the ledger and the audit trail carry the fact instead.
    ledger_service refuses a second reversal of the same event.
    """
    intent = get(session, intent_id)
    if intent.status is not S.LEDGER_UPDATED:
        raise IllegalTransition(intent.id, intent.status, S.LEDGER_UPDATED)

    original = session.execute(
        select(ledger_service.LedgerEvent)
        .where(ledger_service.LedgerEvent.intent_id == intent.id,
               ledger_service.LedgerEvent.type != LedgerEventType.REVERSAL)
        .order_by(ledger_service.LedgerEvent.created_at.asc())
        .limit(1)
    ).scalar_one_or_none()
    if original is None:
        raise NotPermitted(f"intent {intent.id} has no ledger event to reverse")

    ledger_service.append_reversal(session, original_event_id=original.id, reason=reason, intent_id=intent.id)
    audit_service.write(session, actor=actor, action="intent_reversed", user_id=intent.user_id,
                        intent_id=intent.id, inputs={"reason": reason, "original_event_id": original.id})
    reward_service.recompute_eligibility(session, intent.user_id)
    return intent


def is_reversed(session: Session, intent: ActionIntent) -> bool:
    return session.execute(
        select(func.count(ledger_service.LedgerEvent.id)).where(
            ledger_service.LedgerEvent.intent_id == intent.id,
            ledger_service.LedgerEvent.type == LedgerEventType.REVERSAL,
        )
    ).scalar_one() > 0
