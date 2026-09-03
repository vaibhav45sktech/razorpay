"""Phase 3 — money state machine tests.

The state machine is where "the LLM may only request" becomes enforceable:
nothing reaches EXECUTING except through ALLOWED or APPROVED, and nothing
reaches the ledger except through settle_success. These tests try every way
around that and confirm each is refused loudly.
"""

from __future__ import annotations

import pytest

from backend.models.entities import (
    ActionIntent,
    AllocationStatus,
    Approval,
    ApprovalStatus,
    AuditEvent,
    Bucket,
    IntentStatus as S,
    IntentType,
    LedgerEvent,
    LedgerEventType,
    PoolAllocation,
    PoolCycle,
    PoolCycleStatus,
    Reward,
    RewardSource,
    RewardStatus,
    SpendPolicy,
    User,
)
from backend.services import audit_service, ledger_service
from backend.services import money_action_service as mas
from backend.services.policy_engine import check_policy

RUPEE = 100
EVIDENCE = {"debug": True, "id": "fake_pay_1"}


@pytest.fixture()
def user(db) -> User:
    u = User(name="State Machine Student (demo)")
    db.add(u)
    db.flush()
    db.add(SpendPolicy(user_id=u.id, monthly_limit_paise=1000 * RUPEE, approval_threshold_paise=500 * RUPEE,
                       protected_buckets=[Bucket.EMERGENCY_SAVINGS.value]))
    db.commit()
    return u


def contribution(db, user, paise=500 * RUPEE, purpose="savings_goal:g1") -> ActionIntent:
    r = mas.create(db, user_id=user.id, action="CONTRIBUTION", amount_paise=paise, purpose=purpose)
    db.commit()
    return r.intent


def purchase(db, user, paise, purpose="purchase:item") -> mas.CreateResult:
    r = mas.create(db, user_id=user.id, action="PURCHASE", amount_paise=paise, purpose=purpose)
    db.commit()
    return r


def settle(db, intent) -> ActionIntent:
    mas.begin_execution(db, intent, evidence=EVIDENCE)
    mas.settle_success(db, intent, provider_evidence=EVIDENCE, source="test:settle")
    db.commit()
    return intent


# ---------------------------------------------------------------------------
# The transition table itself
# ---------------------------------------------------------------------------


def test_every_status_has_a_row_in_the_table() -> None:
    """A status the table forgot would raise KeyError at runtime - catch it here."""
    assert set(mas.LEGAL) == set(S)


def test_terminal_states_have_no_exits() -> None:
    assert mas.TERMINAL_STATUSES == {S.LEDGER_UPDATED, S.CLOSED}


def test_nothing_reaches_executing_except_allowed_or_approved() -> None:
    sources = {s for s, nxt in mas.LEGAL.items() if S.EXECUTING in nxt}
    assert sources == {S.ALLOWED, S.APPROVED}


def test_nothing_reaches_ledger_updated_except_verified() -> None:
    sources = {s for s, nxt in mas.LEGAL.items() if S.LEDGER_UPDATED in nxt}
    assert sources == {S.VERIFIED}


def test_illegal_transition_raises_and_changes_nothing(db, user) -> None:
    intent = contribution(db, user)
    assert intent.status is S.ALLOWED
    before = db.query(AuditEvent).count()

    with pytest.raises(mas.IllegalTransition):
        mas.transition(db, intent, S.LEDGER_UPDATED)   # skipping the whole middle

    assert intent.status is S.ALLOWED
    assert db.query(AuditEvent).count() == before


def test_terminal_intent_cannot_move(db, user) -> None:
    intent = settle(db, contribution(db, user))
    assert intent.status is S.LEDGER_UPDATED
    for target in S:
        with pytest.raises(mas.IllegalTransition):
            mas.transition(db, intent, target)


def test_every_legal_transition_is_audited(db, user) -> None:
    intent = settle(db, contribution(db, user))
    actions = [a.action for a in db.query(AuditEvent).filter_by(intent_id=intent.id).all()]
    for step in ("PROPOSED->POLICY_CHECK", "POLICY_CHECK->ALLOWED", "ALLOWED->EXECUTING",
                 "EXECUTING->SUCCESS", "SUCCESS->VERIFIED", "VERIFIED->LEDGER_UPDATED"):
        assert f"intent:{step}" in actions, f"missing audit for {step}"
    assert audit_service.verify_chain(db).ok


# ---------------------------------------------------------------------------
# create(): outcomes
# ---------------------------------------------------------------------------


def test_allowed_intent_lands_in_allowed_with_frozen_policy(db, user) -> None:
    r = purchase(db, user, 300 * RUPEE)
    assert r.duplicate is False
    assert r.intent.status is S.ALLOWED
    assert r.intent.policy_result["decision"] == "ALLOW"
    assert r.intent.policy_result["details"]["monthly_limit_paise"] == 1000 * RUPEE


def test_approval_needed_lands_in_awaiting_with_a_pending_approval_row(db, user) -> None:
    r = purchase(db, user, 600 * RUPEE)
    assert r.intent.status is S.AWAITING_APPROVAL
    approval = db.query(Approval).filter_by(intent_id=r.intent.id).one()
    assert approval.status is ApprovalStatus.PENDING
    assert approval.user_id == user.id


def test_denied_intent_is_persisted_and_closed(db, user) -> None:
    """Unauthorised attempts must be traceable (PRD s6.1), not silently dropped."""
    r = purchase(db, user, 5000 * RUPEE)
    assert r.intent.status is S.CLOSED
    assert r.intent.policy_result["decision"] == "DENY"
    assert r.intent.policy_result["rule"] == "monthly_limit"
    actions = [a.action for a in db.query(AuditEvent).filter_by(intent_id=r.intent.id).all()]
    assert "intent:POLICY_CHECK->DENIED" in actions


def test_policy_result_is_frozen_at_decision_time(db, user) -> None:
    """Raising the limit later must not rewrite what was decided then."""
    r = purchase(db, user, 300 * RUPEE)
    user.spend_policy.monthly_limit_paise = 99_999 * RUPEE
    db.commit()
    db.refresh(r.intent)
    assert r.intent.policy_result["details"]["monthly_limit_paise"] == 1000 * RUPEE


def test_unknown_action_is_refused_and_never_persisted(db, user) -> None:
    with pytest.raises(mas.NotPermitted):
        mas.create(db, user_id=user.id, action="TRANSFER_ALL", amount_paise=100, purpose="x")
    assert db.query(ActionIntent).count() == 0


def test_unknown_user_is_refused(db) -> None:
    with pytest.raises(mas.NotPermitted):
        mas.create(db, user_id="usr_ghost", action="PURCHASE", amount_paise=100, purpose="x")


# ---------------------------------------------------------------------------
# Idempotency and retries (PRD s9 case D: "Pay Rs.800 again")
# ---------------------------------------------------------------------------


def test_identical_live_request_returns_the_existing_intent(db, user) -> None:
    first = purchase(db, user, 300 * RUPEE, purpose="purchase:hoodie")
    second = purchase(db, user, 300 * RUPEE, purpose="purchase:hoodie")
    assert second.duplicate is True
    assert second.intent.id == first.intent.id
    assert db.query(ActionIntent).count() == 1


def test_completed_request_is_still_a_duplicate(db, user) -> None:
    """'Pay Rs.800 again' after it already went through -> no second charge."""
    first = purchase(db, user, 300 * RUPEE, purpose="purchase:hoodie")
    settle(db, first.intent)
    again = purchase(db, user, 300 * RUPEE, purpose="purchase:hoodie")
    assert again.duplicate is True
    assert again.intent.status is S.LEDGER_UPDATED
    assert db.query(ActionIntent).count() == 1


def test_retry_after_failure_creates_a_fresh_intent(db, user) -> None:
    """A failed payment must not lock the user out for the rest of the month."""
    first = purchase(db, user, 300 * RUPEE, purpose="purchase:hoodie")
    mas.begin_execution(db, first.intent, evidence=EVIDENCE)
    mas.settle_failure(db, first.intent, provider_evidence={"error": "card declined"})
    db.commit()

    retry = purchase(db, user, 300 * RUPEE, purpose="purchase:hoodie")
    assert retry.duplicate is False
    assert retry.intent.id != first.intent.id
    assert retry.intent.status is S.ALLOWED
    assert retry.intent.client_ref.startswith(first.intent.client_ref)
    assert "#retry" in retry.intent.client_ref


def test_retry_after_denial_is_re_evaluated(db, user) -> None:
    """Denied because of pending cap; pending clears; the same ask should be re-decided."""
    denied = purchase(db, user, 5000 * RUPEE, purpose="purchase:laptop")
    assert denied.intent.status is S.CLOSED
    user.spend_policy.monthly_limit_paise = 10_000 * RUPEE
    db.commit()
    again = purchase(db, user, 5000 * RUPEE, purpose="purchase:laptop")
    assert again.duplicate is False
    assert again.intent.status in (S.ALLOWED, S.AWAITING_APPROVAL)


def test_different_purpose_same_amount_is_not_a_duplicate(db, user) -> None:
    a = purchase(db, user, 300 * RUPEE, purpose="purchase:hoodie")
    b = purchase(db, user, 300 * RUPEE, purpose="purchase:shoes")
    assert b.duplicate is False and a.intent.id != b.intent.id


# ---------------------------------------------------------------------------
# Approval flow
# ---------------------------------------------------------------------------


def test_approve_moves_to_approved_and_records_grant(db, user) -> None:
    r = purchase(db, user, 600 * RUPEE)
    mas.approve(db, intent_id=r.intent.id, user_id=user.id)
    db.commit()
    assert r.intent.status is S.APPROVED
    assert db.query(Approval).filter_by(intent_id=r.intent.id).one().status is ApprovalStatus.GRANTED


def test_only_the_owner_can_approve(db, user) -> None:
    other = User(name="Someone Else (demo)")
    db.add(other)
    db.commit()
    r = purchase(db, user, 600 * RUPEE)
    with pytest.raises(mas.NotPermitted):
        mas.approve(db, intent_id=r.intent.id, user_id=other.id)
    assert r.intent.status is S.AWAITING_APPROVAL


def test_cannot_approve_something_not_awaiting_approval(db, user) -> None:
    r = purchase(db, user, 300 * RUPEE)   # ALLOWED, no approval needed
    with pytest.raises(mas.IllegalTransition):
        mas.approve(db, intent_id=r.intent.id, user_id=user.id)


def test_denying_approval_closes_and_releases_the_limit(db, user) -> None:
    r = purchase(db, user, 600 * RUPEE)
    # while awaiting, the Rs.600 is reserved: a Rs.500 ask would breach Rs.1,000
    assert check_policy(db, user_id=user.id, action="PURCHASE", amount_paise=500 * RUPEE, purpose="p").rule == "monthly_limit"

    mas.deny_approval(db, intent_id=r.intent.id, user_id=user.id)
    db.commit()
    assert r.intent.status is S.CLOSED
    assert db.query(Approval).filter_by(intent_id=r.intent.id).one().status is ApprovalStatus.DENIED
    # released
    assert check_policy(db, user_id=user.id, action="PURCHASE", amount_paise=500 * RUPEE, purpose="p").decision.value == "ALLOW"


def test_approved_intent_can_then_execute(db, user) -> None:
    r = purchase(db, user, 600 * RUPEE)
    mas.approve(db, intent_id=r.intent.id, user_id=user.id)
    settle(db, r.intent)
    assert r.intent.status is S.LEDGER_UPDATED


# ---------------------------------------------------------------------------
# Execution gate and settlement
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("status", [S.PROPOSED, S.POLICY_CHECK, S.AWAITING_APPROVAL, S.DENIED, S.CLOSED, S.LEDGER_UPDATED])
def test_execution_refused_from_unauthorised_states(db, user, status) -> None:
    intent = ActionIntent(user_id=user.id, type=IntentType.PURCHASE, amount_paise=100, purpose="x",
                          client_ref=f"gate-{status.value}", status=status)
    db.add(intent)
    db.commit()
    with pytest.raises(mas.IllegalTransition):
        mas.begin_execution(db, intent)


def test_settlement_writes_exactly_one_ledger_event_linked_to_the_intent(db, user) -> None:
    intent = settle(db, contribution(db, user, 500 * RUPEE))
    events = db.query(LedgerEvent).filter_by(intent_id=intent.id).all()
    assert len(events) == 1
    assert events[0].type is LedgerEventType.CONTRIBUTION
    assert events[0].bucket is Bucket.EMERGENCY_SAVINGS
    assert events[0].amount_paise == +500 * RUPEE
    assert ledger_service.get_balance(db, user.id, Bucket.EMERGENCY_SAVINGS) == 500 * RUPEE


def test_settlement_is_idempotent(db, user) -> None:
    """Webhook after checkout fast-path, or a webhook delivered twice: one ledger event."""
    intent = settle(db, contribution(db, user))
    mas.settle_success(db, intent, provider_evidence=EVIDENCE, source="test:again")
    db.commit()
    assert db.query(LedgerEvent).filter_by(intent_id=intent.id).count() == 1


def test_settlement_cannot_skip_execution(db, user) -> None:
    intent = contribution(db, user)   # ALLOWED, never began executing
    with pytest.raises(mas.IllegalTransition):
        mas.settle_success(db, intent, provider_evidence=EVIDENCE, source="x")
    assert db.query(LedgerEvent).count() == 0


def test_failure_closes_without_touching_the_ledger(db, user) -> None:
    intent = contribution(db, user)
    mas.begin_execution(db, intent, evidence=EVIDENCE)
    mas.settle_failure(db, intent, provider_evidence={"error": "declined"})
    db.commit()
    assert intent.status is S.CLOSED
    assert db.query(LedgerEvent).count() == 0
    assert ledger_service.get_balance(db, user.id, Bucket.EMERGENCY_SAVINGS) == 0


def test_unknown_can_resolve_either_way(db, user) -> None:
    a = contribution(db, user, purpose="g:a")
    mas.begin_execution(db, a, evidence=EVIDENCE)
    mas.mark_unknown(db, a, evidence={"timeout": True})
    mas.settle_success(db, a, provider_evidence={"status": "captured"}, source="test:reconciled")
    assert a.status is S.LEDGER_UPDATED

    b = contribution(db, user, purpose="g:b")
    mas.begin_execution(db, b, evidence=EVIDENCE)
    mas.mark_unknown(db, b, evidence={"timeout": True})
    mas.settle_failure(db, b, provider_evidence={"status": "failed"})
    assert b.status is S.CLOSED


def test_exception_escalation_and_human_resolution(db, user) -> None:
    intent = contribution(db, user)
    mas.begin_execution(db, intent, evidence=EVIDENCE)
    mas.mark_unknown(db, intent, evidence={"timeout": True})
    mas.escalate_exception(db, intent, evidence={"reconciliation": "indeterminate"})
    assert intent.status is S.EXCEPTION
    # still reserves the limit while unresolved (conservative)
    assert intent.status in mas.PENDING_STATUSES
    mas.settle_success(db, intent, provider_evidence={"human": "confirmed captured"}, source="test:human")
    assert intent.status is S.LEDGER_UPDATED


def test_purchase_debits_discretionary_and_shows_in_spend_summary(db, user) -> None:
    r = purchase(db, user, 240 * RUPEE)
    settle(db, r.intent)
    summary = ledger_service.get_month_spend_summary(db, user.id, monthly_limit_paise=1000 * RUPEE)
    assert summary["used_paise"] == 240 * RUPEE
    assert ledger_service.get_balances(db, user.id)  # discretionary absent from balances (D2.1)
    assert Bucket.DISCRETIONARY.value not in ledger_service.get_balances(db, user.id)


def test_no_intent_type_can_debit_emergency_savings() -> None:
    """Structural: 'spend from emergency fund' is unrepresentable, not merely denied."""
    for t in IntentType:
        fake = ActionIntent(user_id="u", type=t, amount_paise=100, purpose="x", client_ref="c")
        event_type, bucket, signed = mas.ledger_effect(fake)
        if bucket is Bucket.EMERGENCY_SAVINGS:
            assert signed > 0, f"{t.value} would debit emergency savings"


# ---------------------------------------------------------------------------
# Reversal (dispute / clawback path)
# ---------------------------------------------------------------------------


def test_reversal_nets_the_balance_and_keeps_history(db, user) -> None:
    intent = settle(db, contribution(db, user, 500 * RUPEE))
    assert ledger_service.get_balance(db, user.id, Bucket.EMERGENCY_SAVINGS) == 500 * RUPEE

    mas.reverse_settled(db, intent_id=intent.id, reason="test dispute")
    db.commit()

    assert ledger_service.get_balance(db, user.id, Bucket.EMERGENCY_SAVINGS) == 0
    assert db.query(LedgerEvent).filter_by(intent_id=intent.id).count() == 2   # original + reversal
    assert intent.status is S.LEDGER_UPDATED                                     # it DID complete
    assert mas.is_reversed(db, intent) is True


def test_reversal_refused_twice(db, user) -> None:
    intent = settle(db, contribution(db, user))
    mas.reverse_settled(db, intent_id=intent.id, reason="once")
    db.commit()
    with pytest.raises(ledger_service.LedgerError):
        mas.reverse_settled(db, intent_id=intent.id, reason="twice")


def test_cannot_reverse_an_unsettled_intent(db, user) -> None:
    intent = contribution(db, user)
    with pytest.raises(mas.IllegalTransition):
        mas.reverse_settled(db, intent_id=intent.id, reason="nope")


# ---------------------------------------------------------------------------
# Payouts and rewards
# ---------------------------------------------------------------------------


def _allocation(db, user, paise):
    cycle = PoolCycle(size=10, contribution_amount_paise=500 * RUPEE, members=[user.id],
                      rules={"description": "test"}, status=PoolCycleStatus.ACTIVE)
    db.add(cycle)
    db.flush()
    alloc = PoolAllocation(cycle_id=cycle.id, user_id=user.id, amount_paise=paise,
                           reason="Consistent contributor for three cycles; eligible for the consistency reward (test).",
                           status=AllocationStatus.CONFIRMED)
    db.add(alloc)
    db.commit()
    return alloc


def test_payout_credits_rewards_bucket_and_marks_allocation_paid(db, user) -> None:
    alloc = _allocation(db, user, 200 * RUPEE)
    r = mas.create(db, user_id=user.id, action="TEST_PAYOUT", amount_paise=200 * RUPEE, purpose="pool_payout:c1")
    db.commit()
    assert r.intent.status is S.ALLOWED
    assert r.intent.policy_result["reason"] == alloc.reason

    settle(db, r.intent)
    assert ledger_service.get_balance(db, user.id, Bucket.REWARDS) == 200 * RUPEE
    db.refresh(alloc)
    assert alloc.status is AllocationStatus.PAID


def test_second_payout_for_same_allocation_is_denied(db, user) -> None:
    _allocation(db, user, 200 * RUPEE)
    first = mas.create(db, user_id=user.id, action="TEST_PAYOUT", amount_paise=200 * RUPEE, purpose="pool_payout:c1")
    db.commit()
    settle(db, first.intent)

    again = mas.create(db, user_id=user.id, action="TEST_PAYOUT", amount_paise=200 * RUPEE, purpose="pool_payout:c1")
    # exact same request -> duplicate of the completed one
    assert again.duplicate is True
    # a differently-worded request for the same money -> policy denies: already paid
    other = mas.create(db, user_id=user.id, action="TEST_PAYOUT", amount_paise=200 * RUPEE, purpose="pool_payout:c1:retry")
    assert other.intent.status is S.CLOSED
    assert other.intent.policy_result["rule"] == "payout_already_paid"


def test_settlement_recomputes_reward_eligibility(db, user) -> None:
    db.add(Reward(user_id=user.id, label="Reach Rs.500 (demo)", source=RewardSource.PLATFORM_FUNDED,
                  amount_paise=50 * RUPEE, eligibility={"target_balance_paise": 500 * RUPEE},
                  status=RewardStatus.LOCKED))
    db.commit()

    intent = settle(db, contribution(db, user, 500 * RUPEE))
    reward = db.query(Reward).one()
    assert reward.status is RewardStatus.ELIGIBLE

    mas.reverse_settled(db, intent_id=intent.id, reason="dispute")
    db.commit()
    db.refresh(reward)
    assert reward.status is RewardStatus.LOCKED   # eligibility follows the ledger, both ways
