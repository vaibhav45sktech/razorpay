"""Phase 4 Step 3 — each read-only (and update_goal) tool handler, tested
directly against seeded data with zero LLM involved, per the master build
plan's "each independently testable" instruction.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from backend.models.entities import Goal, GoalStatus, User
from backend.models.schemas import (
    CalculateSafeContributionArgs,
    GetEligibleRewardsArgs,
    GetOffersArgs,
    GetTransactionsArgs,
    NoArgs,
    UpdateGoalArgs,
)
from backend.seed import demo_data
from backend.tools import ledger_tools, offer_tools, pool_tools, profile_tools, savings_tools


@pytest.fixture()
def seeded(db):
    demo_data.seed_all(db)
    db.commit()
    aarav = db.execute(select(User).where(User.name == demo_data.PRIMARY_DEMO_USER)).scalar_one()
    diya = db.execute(select(User).where(User.name.like("Diya%"))).scalar_one()
    return {"aarav": aarav, "diya": diya}


# ---------------------------------------------------------------------------
# get_user_profile
# ---------------------------------------------------------------------------


def test_get_user_profile_shows_rules_and_flags(db, seeded) -> None:
    aarav = seeded["aarav"]
    out = profile_tools.get_user_profile(db, aarav.id, NoArgs())
    assert out.user_id == aarav.id
    assert out.status == "active"
    assert out.spend_policy is not None
    assert out.spend_policy["monthly_limit_paise"] == 100_000
    assert out.flags == {"paused": False}


# ---------------------------------------------------------------------------
# get_wallet_or_ledger
# ---------------------------------------------------------------------------


def test_get_wallet_or_ledger_matches_seeded_history(db, seeded) -> None:
    aarav = seeded["aarav"]
    out = ledger_tools.get_wallet_or_ledger(db, aarav.id, NoArgs())
    assert out.balances_paise["emergency_savings"] == 1_500 * 100
    assert out.spending_this_month["used_paise"] == 240 * 100
    assert out.reserved_pending_paise == 0
    assert len(out.recent_events) > 0


def test_get_wallet_or_ledger_empty_state_for_new_user(db, seeded) -> None:
    """Diya has a policy but no ledger history — empty states must not crash."""
    diya = seeded["diya"]
    out = ledger_tools.get_wallet_or_ledger(db, diya.id, NoArgs())
    assert out.balances_paise["emergency_savings"] == 0
    assert out.spending_this_month["used_paise"] == 0
    assert out.recent_events == []


# ---------------------------------------------------------------------------
# get_transactions
# ---------------------------------------------------------------------------


def test_get_transactions_this_month_only_sees_current_purchase(db, seeded) -> None:
    aarav = seeded["aarav"]
    out = ledger_tools.get_transactions(db, aarav.id, GetTransactionsArgs(period="this_month"))
    # Seed data: 3 contributions backdated 1-3 months ago, 1 purchase this month.
    assert out.period == "this_month"
    assert out.event_count == 1
    assert out.total_by_type == {"PURCHASE": -240 * 100}


def test_get_transactions_all_sees_full_history(db, seeded) -> None:
    aarav = seeded["aarav"]
    out = ledger_tools.get_transactions(db, aarav.id, GetTransactionsArgs(period="all"))
    assert out.event_count == 4  # 3 contributions + 1 purchase
    assert out.total_by_type["CONTRIBUTION"] == 3 * 500 * 100
    assert out.total_by_type["PURCHASE"] == -240 * 100


# ---------------------------------------------------------------------------
# calculate_safe_contribution
# ---------------------------------------------------------------------------


def test_calculate_safe_contribution_recommends_within_band(db, seeded) -> None:
    aarav = seeded["aarav"]
    goal = db.execute(select(Goal).where(Goal.user_id == aarav.id)).scalars().first()
    out = savings_tools.calculate_safe_contribution(db, aarav.id, CalculateSafeContributionArgs(goal_id=goal.id))
    assert out.min_paise <= out.recommended_amount_paise <= out.max_paise
    assert out.reasons  # never an unexplained number


def test_calculate_safe_contribution_rejects_goal_owned_by_someone_else(db, seeded) -> None:
    aarav, diya = seeded["aarav"], seeded["diya"]
    diyas_goal = db.execute(select(Goal).where(Goal.user_id == diya.id)).scalars().first()
    with pytest.raises(PermissionError):
        savings_tools.calculate_safe_contribution(db, aarav.id, CalculateSafeContributionArgs(goal_id=diyas_goal.id))


def test_calculate_safe_contribution_unknown_goal_raises(db, seeded) -> None:
    aarav = seeded["aarav"]
    with pytest.raises(LookupError):
        savings_tools.calculate_safe_contribution(db, aarav.id, CalculateSafeContributionArgs(goal_id="gol_nope"))


# ---------------------------------------------------------------------------
# update_goal
# ---------------------------------------------------------------------------


def test_update_goal_pause_then_resume(db, seeded) -> None:
    aarav = seeded["aarav"]
    goal = db.execute(select(Goal).where(Goal.user_id == aarav.id)).scalars().first()

    paused = savings_tools.update_goal(db, aarav.id, UpdateGoalArgs(goal_id=goal.id, event="pause"))
    assert paused.status == "paused"
    assert goal.status is GoalStatus.PAUSED

    resumed = savings_tools.update_goal(db, aarav.id, UpdateGoalArgs(goal_id=goal.id, event="resume"))
    assert resumed.status == "active"
    assert goal.status is GoalStatus.ACTIVE


# ---------------------------------------------------------------------------
# get_pool_status
# ---------------------------------------------------------------------------


def test_get_pool_status_reports_membership_and_never_a_pooled_balance(db, seeded) -> None:
    aarav = seeded["aarav"]
    out = pool_tools.get_pool_status(db, aarav.id, NoArgs())
    assert out.in_a_cycle is True
    assert out.cycle["member_count"] == 10
    assert "pooled_balance_paise" not in out.cycle
    assert len(out.cycle["my_allocations"]) == 1


def test_get_pool_status_absent_for_user_not_in_a_cycle(db) -> None:
    lone = User(name="No Pool Student (demo)")
    db.add(lone)
    db.commit()
    out = pool_tools.get_pool_status(db, lone.id, NoArgs())
    assert out.in_a_cycle is False
    assert out.cycle is None


# ---------------------------------------------------------------------------
# get_eligible_rewards
# ---------------------------------------------------------------------------


def test_get_eligible_rewards_groups_by_status(db, seeded) -> None:
    aarav = seeded["aarav"]
    out = offer_tools.get_eligible_rewards(db, aarav.id, GetEligibleRewardsArgs())
    assert len(out.eligible) == 1  # 3-month streak reward, seeded ELIGIBLE
    assert len(out.locked) == 1    # Rs.5,000 cushion reward, seeded LOCKED


# ---------------------------------------------------------------------------
# get_offers
# ---------------------------------------------------------------------------


def test_get_offers_excludes_expired_and_filters_by_category(db, seeded) -> None:
    aarav = seeded["aarav"]
    out = offer_tools.get_offers(db, aarav.id, GetOffersArgs(category="electronics"))
    titles = [o["title"] for o in out.offers]
    assert all("EXPIRED" not in t for t in titles)
    assert any("headphones" in t for t in titles)


def test_get_offers_ranks_highest_saving_first(db, seeded) -> None:
    aarav = seeded["aarav"]
    out = offer_tools.get_offers(db, aarav.id, GetOffersArgs())
    savings = [o["effective_discount_paise"] for o in out.offers]
    assert savings == sorted(savings, reverse=True)


def test_get_offers_is_labelled_synthetic(db, seeded) -> None:
    aarav = seeded["aarav"]
    out = offer_tools.get_offers(db, aarav.id, GetOffersArgs())
    assert out.offers, "expected at least one eligible offer in seed data"
    assert all(o["is_synthetic"] for o in out.offers)
    assert "synthetic" in out.note.lower()
