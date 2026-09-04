"""check_policy and create_payment_intent tool handlers, tested directly
against seeded data with zero LLM involved (same spirit as
test_tools_readonly.py — these two are just the LLM-callable tools that
happen to touch money-adjacent state)."""

from __future__ import annotations

import pytest
from sqlalchemy import select

from backend.models.entities import Bucket, IntentStatus, User
from backend.models.schemas import CheckPolicyArgs, CreateIntentArgs
from backend.seed import demo_data
from backend.tools import payment_tools, policy_tools


@pytest.fixture()
def aarav(db) -> User:
    demo_data.seed_all(db)
    db.commit()
    return db.execute(select(User).where(User.name == demo_data.PRIMARY_DEMO_USER)).scalar_one()


def test_check_policy_allows_a_small_purchase(db, aarav) -> None:
    out = policy_tools.check_policy(
        db, aarav.id, CheckPolicyArgs(action="PURCHASE", amount_paise=100 * 100, purpose="purchase:test")
    )
    assert out.decision == "ALLOW"


def test_check_policy_denies_spending_from_emergency_savings(db, aarav) -> None:
    out = policy_tools.check_policy(
        db,
        aarav.id,
        CheckPolicyArgs(
            action="PURCHASE", amount_paise=100 * 100, purpose="purchase:test", bucket="emergency_savings"
        ),
    )
    assert out.decision == "DENY"
    assert out.rule == "protected_bucket"


def test_check_policy_requires_approval_above_threshold(db, aarav) -> None:
    out = policy_tools.check_policy(
        db, aarav.id, CheckPolicyArgs(action="PURCHASE", amount_paise=600 * 100, purpose="purchase:big_thing")
    )
    assert out.decision == "REQUIRE_APPROVAL"


def test_create_payment_intent_allowed_contribution_reaches_allowed(db, aarav) -> None:
    out = payment_tools.create_payment_intent(
        db, aarav.id, CreateIntentArgs(action="CONTRIBUTION", amount_paise=200 * 100, purpose="savings_goal:test")
    )
    assert out.status == IntentStatus.ALLOWED.value
    assert out.duplicate is False
    assert out.policy["decision"] == "ALLOW"


def test_create_payment_intent_denied_purchase_never_touches_the_ledger(db, aarav) -> None:
    from backend.services import ledger_service

    before = ledger_service.get_balance(db, aarav.id, Bucket.EMERGENCY_SAVINGS)
    out = payment_tools.create_payment_intent(
        db,
        aarav.id,
        CreateIntentArgs(action="PURCHASE", amount_paise=100 * 100, purpose="purchase:test", bucket="emergency_savings"),
    )
    assert out.status == IntentStatus.CLOSED.value
    assert out.policy["decision"] == "DENY"
    after = ledger_service.get_balance(db, aarav.id, Bucket.EMERGENCY_SAVINGS)
    assert before == after


def test_create_payment_intent_is_idempotent_within_the_period(db, aarav) -> None:
    first = payment_tools.create_payment_intent(
        db, aarav.id, CreateIntentArgs(action="CONTRIBUTION", amount_paise=300 * 100, purpose="savings_goal:dup_test")
    )
    second = payment_tools.create_payment_intent(
        db, aarav.id, CreateIntentArgs(action="CONTRIBUTION", amount_paise=300 * 100, purpose="savings_goal:dup_test")
    )
    assert second.duplicate is True
    assert second.intent_id == first.intent_id
