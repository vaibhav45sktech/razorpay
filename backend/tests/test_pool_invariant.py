"""THE POOL INVARIANT: no code path may ever produce a pooled balance.

Why this test exists (CampusPool_Production_Readiness.md s2): a community
savings pool that holds real money is, absent the right legal structure, an
unregulated deposit scheme under India's BUDS Act 2019, and chit funds require
state registration under the Chit Funds Act 1982. The PRD therefore keeps the
pool SIMULATED with individual ledgers (s4.1). This file turns that legal
constraint into a failing build: if anyone ever adds a pooled balance, these
tests go red.

Three angles:
  1. Schema: no table carries a balance-like column.
  2. Conservation: all money in the system is attributable to individual user
     ledgers - there is no residual "pool account" anywhere.
  3. Behaviour: a full cycle with a settled payout still satisfies 1 and 2, and
     the payout lands in the RECIPIENT's own ledger.
"""

from __future__ import annotations

from sqlalchemy import func, inspect, select

from backend.models.entities import (
    AllocationStatus,
    Bucket,
    LedgerEvent,
    LedgerEventType,
    PoolAllocation,
    PoolCycle,
    PoolCycleStatus,
    SpendPolicy,
    User,
)
from backend.seed import demo_data
from backend.services import ledger_service
from backend.services import money_action_service as mas

RUPEE = 100
BALANCE_WORDS = ("balance", "pooled", "pool_total", "corpus", "fund_total", "kitty")


def _all_users(db) -> list[User]:
    return db.query(User).all()


def _total_money_in_ledgers(db) -> int:
    return sum(
        sum(ledger_service.get_raw_bucket_totals(db, u.id).values()) for u in _all_users(db)
    )


def _total_money_in_events(db) -> int:
    return int(db.execute(select(func.coalesce(func.sum(LedgerEvent.amount_paise), 0))).scalar_one())


# ---------------------------------------------------------------------------
# 1. Schema
# ---------------------------------------------------------------------------


def test_no_table_has_a_balance_like_column(db) -> None:
    insp = inspect(db.get_bind())
    offenders = []
    for table in insp.get_table_names():
        for col in insp.get_columns(table):
            name = col["name"].lower()
            if any(w in name for w in BALANCE_WORDS):
                offenders.append(f"{table}.{col['name']}")
    assert offenders == [], f"balance-like columns found: {offenders}"


def test_pool_tables_hold_rules_and_membership_only() -> None:
    cycle_cols = {c.name for c in PoolCycle.__table__.columns}
    assert cycle_cols <= {"id", "label", "size", "contribution_amount_paise", "members", "rules",
                          "status", "is_synthetic", "created_at"}, cycle_cols
    alloc_cols = {c.name for c in PoolAllocation.__table__.columns}
    assert alloc_cols <= {"id", "cycle_id", "user_id", "amount_paise", "reason", "status", "created_at"}, alloc_cols


def test_every_ledger_event_belongs_to_a_user(db) -> None:
    """A ledger event with no user would be money that belongs to 'the pool'. Schema forbids it."""
    assert LedgerEvent.__table__.columns["user_id"].nullable is False


# ---------------------------------------------------------------------------
# 2. Conservation
# ---------------------------------------------------------------------------


def test_all_money_is_attributable_to_individual_ledgers_after_seed(db) -> None:
    demo_data.seed_all(db)
    db.commit()
    assert _total_money_in_ledgers(db) == _total_money_in_events(db)
    assert _total_money_in_events(db) != 0   # the check is not vacuous


def test_pool_service_exposes_no_balance_function() -> None:
    from backend.services import pool_service
    public = [n for n in dir(pool_service) if not n.startswith("_")]
    offenders = [n for n in public if any(w in n.lower() for w in BALANCE_WORDS)]
    assert offenders == [], offenders


# ---------------------------------------------------------------------------
# 3. Behaviour: a full simulated cycle
# ---------------------------------------------------------------------------


def test_full_cycle_with_payout_keeps_the_invariant(db) -> None:
    # Ten synthetic members, each contributing Rs.500 (PRD s4.1 demo).
    members = []
    for i in range(10):
        u = User(name=f"Member {i} (demo)")
        db.add(u)
        db.flush()
        db.add(SpendPolicy(user_id=u.id, monthly_limit_paise=1000 * RUPEE,
                           approval_threshold_paise=500 * RUPEE, protected_buckets=["emergency_savings"]))
        members.append(u)
    db.commit()

    cycle = PoolCycle(size=10, contribution_amount_paise=500 * RUPEE, members=[m.id for m in members],
                      rules={"description": "test cycle"}, status=PoolCycleStatus.ACTIVE)
    db.add(cycle)
    db.flush()

    # Everyone contributes through the real intent path.
    for m in members:
        r = mas.create(db, user_id=m.id, action="CONTRIBUTION", amount_paise=500 * RUPEE, purpose=f"savings_goal:{m.id}")
        mas.begin_execution(db, r.intent, evidence={"debug": True})
        mas.settle_success(db, r.intent, provider_evidence={"debug": True}, source="test:contrib")
    db.commit()

    # Invariant holds: Rs.5,000 exists, all of it in ten individual ledgers.
    assert _total_money_in_events(db) == 5000 * RUPEE
    assert _total_money_in_ledgers(db) == 5000 * RUPEE
    for m in members:
        assert ledger_service.get_balance(db, m.id, Bucket.EMERGENCY_SAVINGS) == 500 * RUPEE

    # One member gets an explained allocation and a settled payout.
    winner = members[3]
    db.add(PoolAllocation(cycle_id=cycle.id, user_id=winner.id, amount_paise=200 * RUPEE,
                          reason="Contributed on time in every prior cycle; consistency reward (test).",
                          status=AllocationStatus.CONFIRMED))
    db.commit()
    r = mas.create(db, user_id=winner.id, action="TEST_PAYOUT", amount_paise=200 * RUPEE, purpose="pool_payout:test")
    mas.begin_execution(db, r.intent, evidence={"debug": True})
    mas.settle_success(db, r.intent, provider_evidence={"debug": True, "id": "fake_payout"}, source="test:payout")
    db.commit()

    # The payout is money in the RECIPIENT's ledger, funded as a reward - not
    # a transfer out of some shared pot, because no shared pot exists.
    assert ledger_service.get_balance(db, winner.id, Bucket.REWARDS) == 200 * RUPEE
    assert ledger_service.get_balance(db, winner.id, Bucket.EMERGENCY_SAVINGS) == 500 * RUPEE  # untouched
    for m in members:
        if m is not winner:
            assert ledger_service.get_balance(db, m.id, Bucket.EMERGENCY_SAVINGS) == 500 * RUPEE  # nobody was debited

    assert _total_money_in_ledgers(db) == _total_money_in_events(db)
    # Funding source is explicit on the event (PRD s4.2)
    payout_event = db.query(LedgerEvent).filter_by(type=LedgerEventType.POOL_PAYOUT).one()
    assert payout_event.user_id == winner.id
    assert payout_event.intent_id == r.intent.id


def test_cycle_summary_never_reports_a_pooled_amount_as_a_balance(db) -> None:
    from backend.services import pool_service
    demo_data.seed_all(db)
    db.commit()
    aarav = db.query(User).filter_by(name=demo_data.PRIMARY_DEMO_USER).one()
    summary = pool_service.cycle_summary(db, aarav.id)
    assert summary is not None
    assert "note" in summary and "No money is pooled" in summary["note"]
    # The virtual cycle amount is a rule parameter, labelled as such - not a balance key.
    assert not any("balance" in k for k in summary)
