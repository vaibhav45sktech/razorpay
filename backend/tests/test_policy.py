"""Phase 2 — policy engine tests. WRITTEN BEFORE THE IMPLEMENTATION.

The policy engine is the actual safety mechanism of this product: the LLM may
only request; this code decides. So the test table was enumerated first, to
force every ALLOW / DENY / REQUIRE_APPROVAL boundary to be chosen deliberately
rather than discovered by accident while coding.

Every expected value traces to the PRD (s1 contribution band, s4.3 limits and
protected buckets) or to policy_config.yaml where the PRD is silent.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from backend.models.entities import (
    ActionIntent,
    AllocationStatus,
    Bucket,
    IntentStatus,
    IntentType,
    LedgerEventType,
    PolicyDecision,
    PoolAllocation,
    PoolCycle,
    PoolCycleStatus,
    SpendPolicy,
    User,
)
from backend.services import ledger_service, policy_engine
from backend.services.policy_engine import PolicyConfig, PolicyResult, check_policy

RUPEE = 100
LIMIT = 1_000 * RUPEE       # Rs.1,000  PRD s4.3
THRESHOLD = 500 * RUPEE     # Rs.500    PRD s4.3


# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------


@pytest.fixture()
def user(db) -> User:
    """A demo student with the PRD s4.3 rules configured."""
    u = User(name="Policy Test Student (demo)")
    db.add(u)
    db.flush()
    db.add(
        SpendPolicy(
            user_id=u.id,
            monthly_limit_paise=LIMIT,
            approval_threshold_paise=THRESHOLD,
            protected_buckets=[Bucket.EMERGENCY_SAVINGS.value],
            paused=False,
        )
    )
    db.commit()
    return u


@pytest.fixture()
def config() -> PolicyConfig:
    return policy_engine.load_config()


def spent_this_month(db, user: User, paise: int) -> None:
    ledger_service.append(
        db,
        user_id=user.id,
        type=LedgerEventType.PURCHASE,
        amount_paise=-paise,
        bucket=Bucket.DISCRETIONARY,
        source="test:prior_purchase",
    )
    db.commit()


def pending_purchase(db, user: User, paise: int, status: IntentStatus, ref: str) -> ActionIntent:
    intent = ActionIntent(
        user_id=user.id,
        type=IntentType.PURCHASE,
        amount_paise=paise,
        purpose=f"purchase:{ref}",
        client_ref=f"ref-{ref}",
        status=status,
    )
    db.add(intent)
    db.commit()
    return intent


def purchase(db, user: User, paise: int, **kw) -> PolicyResult:
    return check_policy(
        db, user_id=user.id, action="PURCHASE", amount_paise=paise, purpose="purchase:test", **kw
    )


def contribution(db, user: User, paise: int, **kw) -> PolicyResult:
    return check_policy(
        db,
        user_id=user.id,
        action="CONTRIBUTION",
        amount_paise=paise,
        purpose="savings_goal:test",
        **kw,
    )


# ---------------------------------------------------------------------------
# THE TABLE — HLD s5.2, extended. Read this before reading the implementation.
# ---------------------------------------------------------------------------

CASES = [
    # id,                          action,         amount,   spent,  expected decision,                 rule
    ("purchase_fresh_month",       "PURCHASE",     300*RUPEE,   0,   PolicyDecision.ALLOW,             "ok"),
    ("purchase_above_threshold",   "PURCHASE",     600*RUPEE,   0,   PolicyDecision.REQUIRE_APPROVAL,  "approval_threshold"),
    ("purchase_at_threshold",      "PURCHASE",     500*RUPEE,   0,   PolicyDecision.ALLOW,             "ok"),            # "above" is strict
    ("purchase_one_paise_over",    "PURCHASE",   500*RUPEE+1,   0,   PolicyDecision.REQUIRE_APPROVAL,  "approval_threshold"),
    ("purchase_exceeds_monthly",   "PURCHASE",     300*RUPEE, 800*RUPEE, PolicyDecision.DENY,          "monthly_limit"),
    ("purchase_exactly_to_limit",  "PURCHASE",     200*RUPEE, 800*RUPEE, PolicyDecision.ALLOW,         "ok"),            # 800+200 == 1000
    ("purchase_one_paise_past",    "PURCHASE",  200*RUPEE+1, 800*RUPEE, PolicyDecision.DENY,           "monthly_limit"),
    ("purchase_limit_beats_approval", "PURCHASE",  700*RUPEE, 500*RUPEE, PolicyDecision.DENY,          "monthly_limit"), # DENY outranks REQUIRE_APPROVAL
    ("contribution_min",           "CONTRIBUTION", 100*RUPEE,   0,   PolicyDecision.ALLOW,             "ok"),
    ("contribution_max",           "CONTRIBUTION", 500*RUPEE,   0,   PolicyDecision.ALLOW,             "ok"),
    ("contribution_mid",           "CONTRIBUTION", 250*RUPEE,   0,   PolicyDecision.ALLOW,             "ok"),
    ("contribution_below_band",    "CONTRIBUTION",  99*RUPEE,   0,   PolicyDecision.DENY,              "contribution_band"),
    ("contribution_above_band",    "CONTRIBUTION", 501*RUPEE,   0,   PolicyDecision.DENY,              "contribution_band"),
    ("unknown_action",             "WITHDRAW_ALL",  1*RUPEE,    0,   PolicyDecision.DENY,              "unknown_action"),
    ("unknown_action_lowercase",   "transfer",     10*RUPEE,    0,   PolicyDecision.DENY,              "unknown_action"),
    ("zero_amount",                "PURCHASE",           0,     0,   PolicyDecision.DENY,              "invalid_amount"),
    ("negative_amount",            "PURCHASE",     -50*RUPEE,   0,   PolicyDecision.DENY,              "invalid_amount"),
]


@pytest.mark.parametrize(
    "case_id,action,amount,spent,expected,rule",
    CASES,
    ids=[c[0] for c in CASES],
)
def test_policy_table(db, user, case_id, action, amount, spent, expected, rule) -> None:
    if spent:
        spent_this_month(db, user, spent)

    result = check_policy(
        db, user_id=user.id, action=action, amount_paise=amount, purpose="table:test"
    )

    assert result.decision is expected, f"{case_id}: {result}"
    assert result.rule == rule, f"{case_id}: fired {result.rule!r}, expected {rule!r}"
    assert result.reason, "every decision carries a human-readable reason"


# ---------------------------------------------------------------------------
# Protected buckets — PRD s4.3 "Never touch emergency savings"
# ---------------------------------------------------------------------------


def test_purchase_from_emergency_savings_is_denied(db, user) -> None:
    result = purchase(db, user, 100 * RUPEE, bucket=Bucket.EMERGENCY_SAVINGS)
    assert result.decision is PolicyDecision.DENY
    assert result.rule == "protected_bucket"
    assert "emergency" in result.reason.lower()


def test_protected_bucket_accepts_string_form(db, user) -> None:
    """The LLM will pass a string; the engine must not be fooled by the type."""
    result = purchase(db, user, 100 * RUPEE, bucket="emergency_savings")
    assert result.decision is PolicyDecision.DENY
    assert result.rule == "protected_bucket"


def test_protection_outranks_every_other_rule(db, user) -> None:
    """Even a tiny, in-limit, under-threshold amount is refused from a protected bucket."""
    result = purchase(db, user, 1, bucket=Bucket.EMERGENCY_SAVINGS)
    assert result.decision is PolicyDecision.DENY
    assert result.rule == "protected_bucket"


def test_purchase_from_discretionary_is_the_default(db, user) -> None:
    assert purchase(db, user, 100 * RUPEE).decision is PolicyDecision.ALLOW
    assert purchase(db, user, 100 * RUPEE, bucket=Bucket.DISCRETIONARY).decision is PolicyDecision.ALLOW


# ---------------------------------------------------------------------------
# Pause — PRD s4.3 "Pause spending -> stop new agentic purchase actions"
# ---------------------------------------------------------------------------


def test_paused_user_cannot_purchase(db, user) -> None:
    user.spend_policy.paused = True
    db.commit()

    result = purchase(db, user, 100 * RUPEE)
    assert result.decision is PolicyDecision.DENY
    assert result.rule == "paused"


def test_paused_user_can_still_contribute(db, user) -> None:
    """Pausing SPENDING must not stop a student from SAVING (PRD wording is 'purchase actions')."""
    user.spend_policy.paused = True
    db.commit()

    assert contribution(db, user, 300 * RUPEE).decision is PolicyDecision.ALLOW


def test_pause_has_no_memory_of_persuasion(db, user) -> None:
    """PRD s5.4: asking repeatedly changes nothing. Same input, same answer, every time."""
    user.spend_policy.paused = True
    db.commit()

    results = {purchase(db, user, 100 * RUPEE).decision for _ in range(5)}
    assert results == {PolicyDecision.DENY}


# ---------------------------------------------------------------------------
# Monthly limit counts committed-but-unsettled spend (PRD s4.3 "committed + completed")
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "status",
    [
        IntentStatus.ALLOWED,
        IntentStatus.APPROVED,
        IntentStatus.EXECUTING,
        IntentStatus.SUCCESS,
        IntentStatus.VERIFIED,
        IntentStatus.UNKNOWN,
        IntentStatus.NEEDS_APPROVAL,
        IntentStatus.AWAITING_APPROVAL,
        IntentStatus.EXCEPTION,
    ],
)
def test_pending_intents_reserve_the_limit(db, user, status) -> None:
    """Rs.800 pending + Rs.300 requested breaches Rs.1,000 even with nothing settled."""
    pending_purchase(db, user, 800 * RUPEE, status, ref=f"pending-{status.value}")

    result = purchase(db, user, 300 * RUPEE)
    assert result.decision is PolicyDecision.DENY, f"{status.value} should reserve the limit"
    assert result.rule == "monthly_limit"


@pytest.mark.parametrize(
    "status",
    [
        IntentStatus.PROPOSED,        # not yet through policy - nothing committed
        IntentStatus.POLICY_CHECK,
        IntentStatus.DENIED,
        IntentStatus.FAILURE,
        IntentStatus.CLOSED,
        IntentStatus.LEDGER_UPDATED,  # already counted via the ledger; counting again would double
    ],
)
def test_terminal_or_uncommitted_intents_do_not_reserve_the_limit(db, user, status) -> None:
    pending_purchase(db, user, 800 * RUPEE, status, ref=f"nonpending-{status.value}")

    assert purchase(db, user, 300 * RUPEE).decision is PolicyDecision.ALLOW


def test_settled_and_pending_combine(db, user) -> None:
    spent_this_month(db, user, 400 * RUPEE)
    pending_purchase(db, user, 400 * RUPEE, IntentStatus.EXECUTING, ref="combo")

    assert purchase(db, user, 200 * RUPEE).decision is PolicyDecision.ALLOW       # 400+400+200 == 1000
    assert purchase(db, user, 201 * RUPEE).decision is PolicyDecision.DENY        # one rupee over


def test_stacking_approvals_cannot_evade_the_limit(db, user) -> None:
    """Five Rs.400 purchases each look fine alone; awaiting-approval ones must still count."""
    pending_purchase(db, user, 400 * RUPEE, IntentStatus.AWAITING_APPROVAL, ref="stack1")
    pending_purchase(db, user, 400 * RUPEE, IntentStatus.AWAITING_APPROVAL, ref="stack2")

    result = purchase(db, user, 400 * RUPEE)
    assert result.decision is PolicyDecision.DENY
    assert result.rule == "monthly_limit"


def test_reason_states_the_numbers(db, user) -> None:
    """The user (and the audit trail) should see WHY, with figures, not just 'denied'."""
    spent_this_month(db, user, 800 * RUPEE)
    result = purchase(db, user, 300 * RUPEE)
    assert "1,000" in result.reason or "1000" in result.reason
    assert result.details["monthly_limit_paise"] == LIMIT
    assert result.details["settled_this_month_paise"] == 800 * RUPEE
    assert result.details["requested_paise"] == 300 * RUPEE


# ---------------------------------------------------------------------------
# Per-transaction cap — only when the user has set one (PRD leaves it undefined)
# ---------------------------------------------------------------------------


def test_per_tx_limit_is_off_by_default(db, user) -> None:
    assert user.spend_policy.per_tx_limit_paise is None
    assert purchase(db, user, 500 * RUPEE).decision is PolicyDecision.ALLOW


def test_per_tx_limit_applies_when_set(db, user) -> None:
    user.spend_policy.per_tx_limit_paise = 200 * RUPEE
    db.commit()

    result = purchase(db, user, 201 * RUPEE)
    assert result.decision is PolicyDecision.DENY
    assert result.rule == "per_tx_limit"
    assert purchase(db, user, 200 * RUPEE).decision is PolicyDecision.ALLOW


# ---------------------------------------------------------------------------
# Velocity controls — Production Readiness s3.3
# ---------------------------------------------------------------------------


def _config_with_velocity(base: PolicyConfig, **overrides) -> PolicyConfig:
    return base.with_velocity(**overrides)


def test_hourly_velocity_limit(db, user, config) -> None:
    tight = _config_with_velocity(config, max_intents_per_hour=2)
    pending_purchase(db, user, 10 * RUPEE, IntentStatus.LEDGER_UPDATED, ref="v1")
    pending_purchase(db, user, 10 * RUPEE, IntentStatus.LEDGER_UPDATED, ref="v2")

    result = purchase(db, user, 10 * RUPEE, config=tight)
    assert result.decision is PolicyDecision.DENY
    assert result.rule == "velocity_hourly"


def test_daily_velocity_limit(db, user, config) -> None:
    tight = _config_with_velocity(config, max_intents_per_hour=100, max_intents_per_day=3)
    for i in range(3):
        intent = pending_purchase(db, user, 10 * RUPEE, IntentStatus.LEDGER_UPDATED, ref=f"d{i}")
        intent.created_at = datetime.now(timezone.utc) - timedelta(hours=5)  # outside the hour, inside the day
    db.commit()

    result = purchase(db, user, 10 * RUPEE, config=tight)
    assert result.decision is PolicyDecision.DENY
    assert result.rule == "velocity_daily"


def test_old_intents_fall_out_of_the_velocity_window(db, user, config) -> None:
    tight = _config_with_velocity(config, max_intents_per_hour=1, max_intents_per_day=1)
    old = pending_purchase(db, user, 10 * RUPEE, IntentStatus.LEDGER_UPDATED, ref="old")
    old.created_at = datetime.now(timezone.utc) - timedelta(days=2)
    db.commit()

    assert purchase(db, user, 10 * RUPEE, config=tight).decision is PolicyDecision.ALLOW


def test_pending_intent_cap(db, user, config) -> None:
    """An abuser opens many intents and settles none, tying up the limit."""
    tight = _config_with_velocity(config, max_pending_intents=2)
    pending_purchase(db, user, 10 * RUPEE, IntentStatus.EXECUTING, ref="p1")
    pending_purchase(db, user, 10 * RUPEE, IntentStatus.AWAITING_APPROVAL, ref="p2")

    result = purchase(db, user, 10 * RUPEE, config=tight)
    assert result.decision is PolicyDecision.DENY
    assert result.rule == "pending_cap"


def test_velocity_applies_to_contributions_too(db, user, config) -> None:
    """Abuse controls are per user, not per action type."""
    tight = _config_with_velocity(config, max_intents_per_hour=1)
    pending_purchase(db, user, 10 * RUPEE, IntentStatus.LEDGER_UPDATED, ref="c1")

    assert contribution(db, user, 300 * RUPEE, config=tight).decision is PolicyDecision.DENY


def test_default_velocity_values_do_not_trip_a_normal_user(db, user, config) -> None:
    """A student making three purchases in an afternoon is not an attacker."""
    for i in range(3):
        pending_purchase(db, user, 50 * RUPEE, IntentStatus.LEDGER_UPDATED, ref=f"normal{i}")

    assert purchase(db, user, 50 * RUPEE).decision is PolicyDecision.ALLOW


# ---------------------------------------------------------------------------
# Test payouts — must be authorised by an explainable pool allocation (PRD s4.1)
# ---------------------------------------------------------------------------


def _allocation(db, user: User, paise: int, status: AllocationStatus) -> PoolAllocation:
    cycle = PoolCycle(size=10, contribution_amount_paise=500 * RUPEE, members=[user.id],
                      rules={"description": "test"}, status=PoolCycleStatus.ACTIVE)
    db.add(cycle)
    db.flush()
    alloc = PoolAllocation(
        cycle_id=cycle.id, user_id=user.id, amount_paise=paise,
        reason="Contributed on time for three cycles; eligible for the consistency reward (test).",
        status=status,
    )
    db.add(alloc)
    db.commit()
    return alloc


def payout(db, user: User, paise: int) -> PolicyResult:
    return check_policy(
        db, user_id=user.id, action="TEST_PAYOUT", amount_paise=paise, purpose="pool_payout:test"
    )


def test_payout_without_allocation_is_denied(db, user) -> None:
    result = payout(db, user, 200 * RUPEE)
    assert result.decision is PolicyDecision.DENY
    assert result.rule == "no_pool_authorization"


def test_payout_with_matching_allocation_is_allowed_and_explained(db, user) -> None:
    alloc = _allocation(db, user, 200 * RUPEE, AllocationStatus.CONFIRMED)

    result = payout(db, user, 200 * RUPEE)
    assert result.decision is PolicyDecision.ALLOW
    assert result.rule == "ok"
    assert result.reason == alloc.reason              # the allocation's reason IS the authorisation
    assert result.details["allocation_id"] == alloc.id


def test_payout_amount_must_match_the_allocation(db, user) -> None:
    _allocation(db, user, 200 * RUPEE, AllocationStatus.CONFIRMED)
    assert payout(db, user, 201 * RUPEE).decision is PolicyDecision.DENY


def test_already_paid_allocation_cannot_pay_twice(db, user) -> None:
    _allocation(db, user, 200 * RUPEE, AllocationStatus.PAID)
    result = payout(db, user, 200 * RUPEE)
    assert result.decision is PolicyDecision.DENY
    assert result.rule == "payout_already_paid"


def test_cancelled_allocation_does_not_authorise(db, user) -> None:
    _allocation(db, user, 200 * RUPEE, AllocationStatus.CANCELLED)
    assert payout(db, user, 200 * RUPEE).decision is PolicyDecision.DENY


# ---------------------------------------------------------------------------
# Fail-closed behaviour
# ---------------------------------------------------------------------------


def test_user_without_a_spend_policy_is_denied(db) -> None:
    """No rules configured means no permission, not default permission."""
    u = User(name="No Policy (demo)")
    db.add(u)
    db.commit()

    result = check_policy(db, user_id=u.id, action="PURCHASE", amount_paise=100, purpose="x")
    assert result.decision is PolicyDecision.DENY
    assert result.rule == "no_spend_policy"


def test_unknown_user_is_denied(db) -> None:
    result = check_policy(db, user_id="usr_ghost", action="PURCHASE", amount_paise=100, purpose="x")
    assert result.decision is PolicyDecision.DENY


def test_float_amount_is_denied_not_rounded(db, user) -> None:
    result = check_policy(
        db, user_id=user.id, action="PURCHASE", amount_paise=100.5, purpose="x"  # type: ignore[arg-type]
    )
    assert result.decision is PolicyDecision.DENY
    assert result.rule == "invalid_amount"


def test_action_matching_is_case_insensitive_but_exact(db, user) -> None:
    assert check_policy(db, user_id=user.id, action="purchase", amount_paise=100 * RUPEE, purpose="x").decision is PolicyDecision.ALLOW
    assert check_policy(db, user_id=user.id, action="PURCHASES", amount_paise=100 * RUPEE, purpose="x").rule == "unknown_action"


# ---------------------------------------------------------------------------
# Result shape — what the audit trail and the agent consume
# ---------------------------------------------------------------------------


def test_result_is_serialisable_and_frozen(db, user) -> None:
    result = purchase(db, user, 100 * RUPEE)
    as_dict = result.as_dict()
    assert as_dict["decision"] == "ALLOW"
    assert set(as_dict) >= {"decision", "reason", "rule", "details"}
    with pytest.raises(Exception):
        result.decision = PolicyDecision.DENY  # type: ignore[misc]


def test_engine_is_pure_and_writes_nothing(db, user) -> None:
    """check_policy decides; it does not record. Callers audit (orchestrator, intent creation)."""
    from backend.models.entities import AuditEvent

    before = db.query(AuditEvent).count()
    purchase(db, user, 100 * RUPEE)
    purchase(db, user, 5_000 * RUPEE)
    assert db.query(AuditEvent).count() == before
    assert db.query(ActionIntent).count() == 0
