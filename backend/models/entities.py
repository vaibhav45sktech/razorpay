"""SQLAlchemy models: the structural source of truth for financial state.

Source-of-truth hierarchy (PRD s8.1): the PRD outranks this file, this file
outranks the policy config, and LLM output is never a source of truth at all.

Three conventions here are load-bearing across the whole codebase:

1. MONEY IS INTEGER PAISE. Never floats, never rupees. Rs.500 == 50000 paise.
   Every money column is named *_paise so the unit cannot be misread at a
   glance. Razorpay's API also speaks paise, which removes a whole class of
   conversion bugs at the integration boundary.

2. BALANCES ARE DERIVED, NEVER STORED. There is deliberately no balance column
   anywhere in this file. A balance is always SUM(ledger_events.amount_paise)
   for a given user + bucket. A stored balance can silently drift out of sync
   with the events that produced it; a derived one cannot.

3. IDS ARE PREFIXED STRINGS ("usr_a1b2...", "int_c3d4..."). Slightly more
   verbose than integers, but it means an ID in a log line, an audit record or
   a Razorpay notes field tells you what kind of thing it refers to, and an ID
   from the wrong table can never be silently accepted.

DATA CLASSIFICATION (for DPDP Act 2023 readiness)
-------------------------------------------------
This prototype processes ONLY synthetic data, so India's DPDP Act does not
currently bite - it governs personal data of identifiable individuals. But the
Act's substantive obligations commence 13 May 2027, and the classification
below is what makes that a configuration exercise rather than a rewrite. See
CampusPool_Production_Readiness.md s1.2 for the phase-in timeline.

    SYNTHETIC-ONLY (no personal data even in production)
        Offer, PoolCycle          - catalogue and rule definitions

    WOULD-BE-PERSONAL-DATA (holds identifiable info once real users exist)
        User                      - name, and later contact details
        Approval                  - links a person to an authorisation decision
        PoolAllocation            - links a person to a benefit

    FINANCIAL-RECORD (retention likely mandated, may CONFLICT with erasure)
        LedgerEvent, ActionIntent - the money trail
        AuditEvent                - the decision trail; stores only a HASH of
                                    tool inputs, never raw argument text, so it
                                    stays outside the scope of most erasure
                                    requests by design

The tension between DPDP erasure rights and financial record-keeping duties is
a legal question, not an engineering one, and is escalated in the master build
plan Part D. Retention VALUES are therefore left unset here rather than guessed.
"""

from __future__ import annotations

import enum
from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    Enum as SAEnum,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    """Declarative base for all CampusPool tables."""


def _utcnow() -> datetime:
    """Timezone-aware UTC now. Naive datetimes cause real bugs; avoid them."""
    return datetime.now(timezone.utc)


def new_id(prefix: str) -> str:
    """Generate a prefixed, URL-safe identifier, e.g. new_id('usr')."""
    return f"{prefix}_{uuid4().hex[:20]}"


# ---------------------------------------------------------------------------
# Enumerations
#
# Declared as Python enums (typed, autocompletable, greppable) but persisted as
# plain strings via native_enum=False, so the SQLite file stays human-readable
# and adding a value later does not require a schema migration dance.
# ---------------------------------------------------------------------------


def _str_enum(enum_cls: type[enum.Enum], name: str) -> SAEnum:
    return SAEnum(enum_cls, name=name, native_enum=False, values_callable=lambda e: [m.value for m in e])


class UserStatus(str, enum.Enum):
    ACTIVE = "active"
    PAUSED = "paused"


class GoalStatus(str, enum.Enum):
    ACTIVE = "active"
    ACHIEVED = "achieved"
    PAUSED = "paused"


class Bucket(str, enum.Enum):
    """Where money sits, or how it flows.

    DESIGN DECISION - two kinds of bucket, and it matters:

    EMERGENCY_SAVINGS and REWARDS are true BALANCES. Money accumulates in them
    and a positive number is meaningful to a user: "you have Rs.1,500 saved".

    DISCRETIONARY is a SPEND TRACKER, not a balance. Nothing credits it; only
    purchases debit it, so its running sum is always negative and grows more
    negative over time. That is correct and intentional: the PRD governs
    discretionary spending by a MONTHLY LIMIT (s4.3), not by a stored spending
    balance, so what is meaningful is "Rs.240 of Rs.1,000 used this month" -
    never "your discretionary balance is -Rs.240", which reads as a debt and is
    not a claim this product makes.

    This is enforced structurally rather than by convention: ledger_service
    exposes discretionary only through month_spend()/get_month_spend_summary(),
    and get_balances() returns BALANCE_BUCKETS only, so a negative spend
    tracker cannot leak into a balance display. get_raw_bucket_totals() exists
    for reconciliation and is named to signal it is not for display.

    Alternatives considered and rejected: adding a monthly "allowance" credit
    to make the number positive would invent a funding flow the PRD does not
    describe; dropping the bucket would lose per-category spend tracking.
    """

    EMERGENCY_SAVINGS = "emergency_savings"
    DISCRETIONARY = "discretionary"
    REWARDS = "rewards"


#: Buckets whose running sum is a real, user-facing balance.
BALANCE_BUCKETS: tuple[Bucket, ...] = (Bucket.EMERGENCY_SAVINGS, Bucket.REWARDS)

#: Buckets that only ever accumulate outflow, reported as spend-against-limit.
SPEND_TRACKING_BUCKETS: tuple[Bucket, ...] = (Bucket.DISCRETIONARY,)


class LedgerEventType(str, enum.Enum):
    CONTRIBUTION = "CONTRIBUTION"
    PURCHASE = "PURCHASE"
    POOL_PAYOUT = "POOL_PAYOUT"
    REWARD = "REWARD"
    REVERSAL = "REVERSAL"


class PoolCycleStatus(str, enum.Enum):
    FORMING = "forming"
    ACTIVE = "active"
    SETTLED = "settled"


class AllocationStatus(str, enum.Enum):
    PROPOSED = "proposed"
    CONFIRMED = "confirmed"
    PAID = "paid"
    CANCELLED = "cancelled"


class RewardSource(str, enum.Enum):
    """PRD s4.2: reward funding source must always be explicit, never implied."""

    PLATFORM_FUNDED = "platform_funded"
    PARTNER_FUNDED = "partner_funded"
    POOL_FUNDED = "pool_funded"


class RewardStatus(str, enum.Enum):
    ELIGIBLE = "eligible"
    LOCKED = "locked"
    REDEEMED = "redeemed"
    EXPIRED = "expired"


class IntentType(str, enum.Enum):
    CONTRIBUTION = "CONTRIBUTION"
    PURCHASE = "PURCHASE"
    TEST_PAYOUT = "TEST_PAYOUT"


class IntentStatus(str, enum.Enum):
    """States from the PRD s5.5 / HLD s2.5 state machine.

    money_action_service owns the legal transitions between these. Nothing
    else may assign this column directly.
    """

    PROPOSED = "PROPOSED"
    POLICY_CHECK = "POLICY_CHECK"
    DENIED = "DENIED"
    NEEDS_APPROVAL = "NEEDS_APPROVAL"
    AWAITING_APPROVAL = "AWAITING_APPROVAL"
    APPROVED = "APPROVED"
    ALLOWED = "ALLOWED"
    EXECUTING = "EXECUTING"
    SUCCESS = "SUCCESS"
    FAILURE = "FAILURE"
    UNKNOWN = "UNKNOWN"
    VERIFIED = "VERIFIED"
    LEDGER_UPDATED = "LEDGER_UPDATED"
    CLOSED = "CLOSED"
    EXCEPTION = "EXCEPTION"


class ApprovalStatus(str, enum.Enum):
    PENDING = "pending"
    GRANTED = "granted"
    DENIED = "denied"
    EXPIRED = "expired"


class PolicyDecision(str, enum.Enum):
    """The only three answers the policy engine may return."""

    ALLOW = "ALLOW"
    DENY = "DENY"
    REQUIRE_APPROVAL = "REQUIRE_APPROVAL"


class AuditActor(str, enum.Enum):
    LLM = "llm"
    BACKEND = "backend"
    WEBHOOK = "webhook"
    USER = "user"
    SYSTEM = "system"


class ExceptionKind(str, enum.Enum):
    UNKNOWN_PAYMENT_STATE = "unknown_payment_state"
    AMBIGUOUS_POOL_RULE = "ambiguous_pool_rule"
    INVALID_WEBHOOK_SIGNATURE = "invalid_webhook_signature"
    INVALID_CHECKOUT_SIGNATURE = "invalid_checkout_signature"
    WEBHOOK_FOR_UNKNOWN_ORDER = "webhook_for_unknown_order"
    RECONCILIATION_TIMEOUT = "reconciliation_timeout"


class ExceptionStatus(str, enum.Enum):
    OPEN = "open"
    RESOLVED = "resolved"


# ---------------------------------------------------------------------------
# Core entities (PRD s8.4)
# ---------------------------------------------------------------------------


class User(Base):
    """A demo student. All users in this prototype are synthetic."""

    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=lambda: new_id("usr"))
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    status: Mapped[UserStatus] = mapped_column(
        _str_enum(UserStatus, "user_status"), nullable=False, default=UserStatus.ACTIVE
    )
    is_synthetic: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)

    # ---- DPDP-readiness fields (see the data classification in the module docstring) ----
    #
    # Unenforced by design at this stage. Their value now is structural: a
    # retention column and a stated purpose are cheap to add today and painful
    # to retrofit across a live schema later. Deletion workflows are post-MVP
    # (Production Readiness s4.9) and the retention PERIOD is a legal decision
    # (master build plan Part D), so no default is invented here.
    #
    # purpose records WHY this record is held, which is what a consent notice
    # and a data-principal access request both have to be able to answer.
    purpose: Mapped[str] = mapped_column(
        String(80), nullable=False, default="demo_account"
    )
    retention_until: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, default=None
    )

    goals: Mapped[list[Goal]] = relationship(back_populates="user", cascade="all, delete-orphan")
    spend_policy: Mapped[SpendPolicy | None] = relationship(
        back_populates="user", uselist=False, cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:
        return f"<User {self.id} {self.name!r} {self.status.value}>"


class SpendPolicy(Base):
    """Per-user spending rules. Read by the policy engine, never by the LLM directly.

    These are the user's own chosen rules (PRD s4.3), which is why they live per
    user rather than only in policy_config.yaml. The YAML holds product defaults;
    this table holds what this user actually set.
    """

    __tablename__ = "spend_policies"
    __table_args__ = (
        CheckConstraint("monthly_limit_paise >= 0", name="ck_monthly_limit_non_negative"),
        CheckConstraint("approval_threshold_paise >= 0", name="ck_approval_threshold_non_negative"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=lambda: new_id("pol"))
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), nullable=False, unique=True, index=True)

    monthly_limit_paise: Mapped[int] = mapped_column(Integer, nullable=False)
    approval_threshold_paise: Mapped[int] = mapped_column(Integer, nullable=False)
    # Deliberately nullable: the PRD does not define a per-transaction cap.
    # TODO: confirm per-transaction limit with product owner before using it.
    per_tx_limit_paise: Mapped[int | None] = mapped_column(Integer, nullable=True, default=None)

    # Buckets the agent may never spend from, e.g. ["emergency_savings"].
    protected_buckets: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    paused: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, nullable=False
    )

    user: Mapped[User] = relationship(back_populates="spend_policy")

    def __repr__(self) -> str:
        return (
            f"<SpendPolicy {self.user_id} limit={self.monthly_limit_paise} "
            f"approval_above={self.approval_threshold_paise} paused={self.paused}>"
        )


class Goal(Base):
    """A savings goal. Progress is DERIVED from the ledger, not stored here."""

    __tablename__ = "goals"
    __table_args__ = (CheckConstraint("target_amount_paise > 0", name="ck_goal_target_positive"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=lambda: new_id("gol"))
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), nullable=False, index=True)
    label: Mapped[str] = mapped_column(String(120), nullable=False, default="Emergency cushion")
    target_amount_paise: Mapped[int] = mapped_column(Integer, nullable=False)
    cadence: Mapped[str] = mapped_column(String(20), nullable=False, default="monthly")
    status: Mapped[GoalStatus] = mapped_column(
        _str_enum(GoalStatus, "goal_status"), nullable=False, default=GoalStatus.ACTIVE
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)

    user: Mapped[User] = relationship(back_populates="goals")

    def __repr__(self) -> str:
        return f"<Goal {self.id} target={self.target_amount_paise} {self.status.value}>"


class LedgerEvent(Base):
    """APPEND-ONLY record of every money movement. The financial source of truth.

    Nothing in this codebase may UPDATE or DELETE a row here. Corrections are
    made by appending a REVERSAL event, exactly as a real double-entry ledger
    would. ledger_service intentionally exposes no update or delete function,
    so the capability to violate this does not exist in the code.

    amount_paise is SIGNED: positive credits the bucket, negative debits it.
    """

    __tablename__ = "ledger_events"
    __table_args__ = (
        CheckConstraint("amount_paise != 0", name="ck_ledger_amount_non_zero"),
        Index("ix_ledger_user_bucket", "user_id", "bucket"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=lambda: new_id("led"))
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), nullable=False, index=True)
    type: Mapped[LedgerEventType] = mapped_column(_str_enum(LedgerEventType, "ledger_event_type"), nullable=False)
    amount_paise: Mapped[int] = mapped_column(Integer, nullable=False)
    bucket: Mapped[Bucket] = mapped_column(_str_enum(Bucket, "bucket"), nullable=False)

    # Provenance: where this event came from, e.g. "razorpay_payment:pay_abc123",
    # "pool_cycle:pcy_x", "reward:rwd_y". Every event must be traceable.
    source: Mapped[str] = mapped_column(String(120), nullable=False)
    intent_id: Mapped[str | None] = mapped_column(ForeignKey("action_intents.id"), nullable=True, index=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)

    def __repr__(self) -> str:
        sign = "+" if self.amount_paise >= 0 else ""
        return f"<LedgerEvent {self.id} {self.type.value} {sign}{self.amount_paise} {self.bucket.value}>"


class PoolCycle(Base):
    """A simulated community-savings cycle (chit-fund INSPIRED, not a chit fund).

    PRD s4.1: each participant keeps an individual ledger. This row describes the
    cycle's rules and membership; it never holds pooled user money.
    """

    __tablename__ = "pool_cycles"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=lambda: new_id("pcy"))
    label: Mapped[str] = mapped_column(String(120), nullable=False, default="Demo cycle")
    size: Mapped[int] = mapped_column(Integer, nullable=False)
    contribution_amount_paise: Mapped[int] = mapped_column(Integer, nullable=False)

    members: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    # Human-readable allocation rules. PRD s4.1 requires every allocation to be
    # explainable, so the rule text lives with the cycle that applied it.
    rules: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)

    status: Mapped[PoolCycleStatus] = mapped_column(
        _str_enum(PoolCycleStatus, "pool_cycle_status"), nullable=False, default=PoolCycleStatus.FORMING
    )
    is_synthetic: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)

    allocations: Mapped[list[PoolAllocation]] = relationship(back_populates="cycle", cascade="all, delete-orphan")

    def __repr__(self) -> str:
        return f"<PoolCycle {self.id} size={self.size} {self.status.value}>"


class PoolAllocation(Base):
    """One participant's share/benefit in a cycle, with the reason it was granted."""

    __tablename__ = "pool_allocations"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=lambda: new_id("pal"))
    cycle_id: Mapped[str] = mapped_column(ForeignKey("pool_cycles.id"), nullable=False, index=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), nullable=False, index=True)
    amount_paise: Mapped[int] = mapped_column(Integer, nullable=False)

    # PRD s4.1: "Every pool allocation must be explainable". This is not optional
    # decoration - the policy engine surfaces it as the authorization reason for
    # any payout, and the UI shows it to the user verbatim.
    reason: Mapped[str] = mapped_column(Text, nullable=False)

    status: Mapped[AllocationStatus] = mapped_column(
        _str_enum(AllocationStatus, "allocation_status"), nullable=False, default=AllocationStatus.PROPOSED
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)

    cycle: Mapped[PoolCycle] = relationship(back_populates="allocations")

    def __repr__(self) -> str:
        return f"<PoolAllocation {self.id} user={self.user_id} {self.amount_paise} {self.status.value}>"


class Reward(Base):
    """A milestone/streak reward. Funding source is always explicit (PRD s4.2)."""

    __tablename__ = "rewards"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=lambda: new_id("rwd"))
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), nullable=False, index=True)
    label: Mapped[str] = mapped_column(String(160), nullable=False)
    source: Mapped[RewardSource] = mapped_column(_str_enum(RewardSource, "reward_source"), nullable=False)
    amount_paise: Mapped[int] = mapped_column(Integer, nullable=False)

    # Machine-checkable conditions, e.g. {"min_contributions": 3}. The reward
    # service evaluates these deterministically; the LLM only reads the result.
    eligibility: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    status: Mapped[RewardStatus] = mapped_column(
        _str_enum(RewardStatus, "reward_status"), nullable=False, default=RewardStatus.LOCKED
    )
    is_synthetic: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)

    def __repr__(self) -> str:
        return f"<Reward {self.id} {self.source.value} {self.amount_paise} {self.status.value}>"


class Offer(Base):
    """A synthetic partner offer.

    PRD s11 forbids scraped or invented "real" merchant data, so every row here
    is explicitly synthetic and the UI must label it as demo content. Exactly one
    of discount_paise / discount_pct should be set.
    """

    __tablename__ = "offers"
    __table_args__ = (
        CheckConstraint(
            "(discount_paise IS NOT NULL) OR (discount_pct IS NOT NULL)",
            name="ck_offer_has_a_discount",
        ),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=lambda: new_id("off"))
    merchant: Mapped[str] = mapped_column(String(120), nullable=False)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    category: Mapped[str] = mapped_column(String(60), nullable=False, index=True)

    list_price_paise: Mapped[int | None] = mapped_column(Integer, nullable=True)
    discount_paise: Mapped[int | None] = mapped_column(Integer, nullable=True)
    discount_pct: Mapped[float | None] = mapped_column(nullable=True)

    expiry: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    funding_source: Mapped[RewardSource] = mapped_column(_str_enum(RewardSource, "offer_funding_source"), nullable=False)
    eligibility: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)

    # Always True in this prototype. Kept as a column (not a constant) so the API
    # and UI can read it and label the offer as demo content.
    is_synthetic: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)

    def __repr__(self) -> str:
        return f"<Offer {self.id} {self.merchant} {self.title!r}>"


class ActionIntent(Base):
    """The unit that flows through the money state machine (PRD s5.5).

    This is the ceiling of the LLM's power: the agent may cause an intent ROW to
    exist, and nothing more. Only backend-only code turns an intent into a real
    Razorpay call, and only a verified provider response may mark it successful.
    """

    __tablename__ = "action_intents"
    __table_args__ = (
        CheckConstraint("amount_paise > 0", name="ck_intent_amount_positive"),
        Index("ix_intent_user_status", "user_id", "status"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=lambda: new_id("int"))
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), nullable=False, index=True)
    type: Mapped[IntentType] = mapped_column(_str_enum(IntentType, "intent_type"), nullable=False)
    amount_paise: Mapped[int] = mapped_column(Integer, nullable=False)

    # Structured purpose, e.g. "savings_goal:gol_abc" or "purchase:off_xyz".
    purpose: Mapped[str] = mapped_column(String(160), nullable=False)

    # A frozen copy of the policy decision that authorized (or refused) this
    # intent. Frozen on purpose: the audit trail must show what was decided at
    # the time, even if limits change afterwards.
    policy_result: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    # Razorpay order_id / payment_id / payout_id once one exists.
    provider_ref: Mapped[str | None] = mapped_column(String(80), nullable=True, index=True)

    # IDEMPOTENCY KEY (Guardrail 4). Unique, so a duplicate request cannot
    # create a second intent - the database refuses it even if the code slips.
    client_ref: Mapped[str] = mapped_column(String(80), nullable=False, unique=True, index=True)

    status: Mapped[IntentStatus] = mapped_column(
        _str_enum(IntentStatus, "intent_status"), nullable=False, default=IntentStatus.PROPOSED
    )

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, nullable=False
    )

    def __repr__(self) -> str:
        return f"<ActionIntent {self.id} {self.type.value} {self.amount_paise} {self.status.value}>"


class Approval(Base):
    """An explicit user authorization for one intent.

    PRD s5.4: authorization is never inferred from conversation. It exists only
    as a row here, created by a structured user action on a dedicated endpoint.
    """

    __tablename__ = "approvals"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=lambda: new_id("apr"))
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), nullable=False, index=True)
    intent_id: Mapped[str] = mapped_column(ForeignKey("action_intents.id"), nullable=False, unique=True, index=True)
    status: Mapped[ApprovalStatus] = mapped_column(
        _str_enum(ApprovalStatus, "approval_status"), nullable=False, default=ApprovalStatus.PENDING
    )
    # TODO: confirm approval expiry window with product owner (PRD leaves it open).
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    def __repr__(self) -> str:
        return f"<Approval {self.id} intent={self.intent_id} {self.status.value}>"


class AuditEvent(Base):
    """APPEND-ONLY trace of every agent decision and financial action (PRD s6).

    "Every money action traceable" is a stated acceptance criterion, so this
    table is written on every tool call - including refused ones - and on every
    state transition. Never updated, never deleted.
    """

    __tablename__ = "audit_events"
    __table_args__ = (Index("ix_audit_actor_created", "actor", "created_at"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=lambda: new_id("aud"))
    actor: Mapped[AuditActor] = mapped_column(_str_enum(AuditActor, "audit_actor"), nullable=False)
    action: Mapped[str] = mapped_column(String(160), nullable=False)

    user_id: Mapped[str | None] = mapped_column(ForeignKey("users.id"), nullable=True, index=True)
    intent_id: Mapped[str | None] = mapped_column(ForeignKey("action_intents.id"), nullable=True, index=True)

    # Hash rather than raw inputs: keeps the trail small and avoids copying
    # user content into a second place, while still proving what was passed.
    inputs_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    policy_result: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    provider_result: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    # ---- Tamper-evidence: hash chain ----
    #
    # "Append-only" as a code convention protects against accidents, not against
    # anyone with database access. A hash chain makes tampering DETECTABLE:
    # each entry commits to its own content and to the previous entry's hash,
    # so altering or deleting any historical row breaks every hash after it.
    #
    # This does not make the log immutable (nothing in a single database can),
    # but it means forgery cannot be silent - audit_service.verify_chain()
    # reports the exact index where the chain first breaks.
    #
    # seq is a monotonic counter giving the chain a defined order that does not
    # depend on timestamp collisions or string-sorted ids.
    #
    # Assigned by audit_service, NOT by the database: SQLite only autoincrements
    # an INTEGER PRIMARY KEY, and our primary key is a prefixed string. The
    # service is the sole writer and already reads the last entry to obtain
    # prev_hash, so it has the previous seq for free. UNIQUE still guarantees
    # the database rejects a duplicate if that logic ever regresses.
    seq: Mapped[int] = mapped_column(Integer, nullable=False, unique=True)
    prev_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    entry_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)

    def __repr__(self) -> str:
        return f"<AuditEvent seq={self.seq} {self.actor.value} {self.action!r}>"


class WebhookEvent(Base):
    """One row per Razorpay webhook delivery we have ACCEPTED (signature
    verified), keyed by Razorpay's event id. Duplicate deliveries are normal
    (dashboard resends, retries on slow 2xx); this table makes the second one
    a no-op instead of a double settlement. Rejected deliveries (bad
    signature) are NOT recorded here - they go to the audit trail only, so a
    forger cannot "claim" an event id."""

    __tablename__ = "webhook_events"

    #: Razorpay's x-razorpay-event-id header.
    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    event: Mapped[str] = mapped_column(String(60), nullable=False)
    order_id: Mapped[str | None] = mapped_column(String(80), nullable=True, index=True)
    payment_id: Mapped[str | None] = mapped_column(String(80), nullable=True, index=True)
    body_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    outcome: Mapped[str] = mapped_column(String(60), nullable=False)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)


class ExceptionRecord(Base):
    """An ambiguous or unsupported situation, surfaced instead of guessed.

    PRD s6 and s10: when the system cannot determine the truth (unknown payment
    state, ambiguous pool rule, webhook for an unrecognised order), it opens one
    of these for human review rather than inventing an outcome. An empty
    exception queue in the demo means the happy path ran; a populated one is
    the system being honest, not broken.
    """

    __tablename__ = "exception_records"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=lambda: new_id("exc"))
    kind: Mapped[ExceptionKind] = mapped_column(_str_enum(ExceptionKind, "exception_kind"), nullable=False)
    intent_id: Mapped[str | None] = mapped_column(ForeignKey("action_intents.id"), nullable=True, index=True)
    detail: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    status: Mapped[ExceptionStatus] = mapped_column(
        _str_enum(ExceptionStatus, "exception_status"), nullable=False, default=ExceptionStatus.OPEN
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    def __repr__(self) -> str:
        return f"<ExceptionRecord {self.id} {self.kind.value} {self.status.value}>"
