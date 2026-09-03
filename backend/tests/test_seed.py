"""Phase 1 Step 5 — seed data tests.

The seed script is demo infrastructure, but it is also the fastest way to
restore a known-good state before a demo (see Master Build Plan Phase 8 item 6:
"reproducible from seed" is a stronger guarantee than backups at this scope).
So it gets tested like real code - particularly its idempotency, because a
seed that silently duplicates is a confusing afternoon.
"""

from __future__ import annotations

from backend.models.entities import (
    Bucket,
    Goal,
    Offer,
    PoolAllocation,
    PoolCycle,
    Reward,
    SpendPolicy,
    User,
)
from backend.seed import demo_data
from backend.services import audit_service, ledger_service


# ---------------------------------------------------------------------------
# Idempotency — the property most likely to waste your time if broken
# ---------------------------------------------------------------------------


def test_seeding_twice_does_not_duplicate(db) -> None:
    assert demo_data.seed_all(db) is True
    db.commit()
    first_counts = {
        "users": db.query(User).count(),
        "offers": db.query(Offer).count(),
        "goals": db.query(Goal).count(),
        "pools": db.query(PoolCycle).count(),
    }

    # Second run must be a no-op and report so.
    assert demo_data.seed_all(db) is False
    db.commit()

    assert {
        "users": db.query(User).count(),
        "offers": db.query(Offer).count(),
        "goals": db.query(Goal).count(),
        "pools": db.query(PoolCycle).count(),
    } == first_counts


def test_reset_reseeds_cleanly(db) -> None:
    demo_data.seed_all(db)
    db.commit()
    before = db.query(User).count()

    demo_data.seed_all(db, force=True)
    db.commit()

    assert db.query(User).count() == before  # not doubled
    assert demo_data.is_seeded(db) is True


# ---------------------------------------------------------------------------
# PRD-traceable values
# ---------------------------------------------------------------------------


def test_spend_policy_matches_prd_demo_values(db) -> None:
    demo_data.seed_all(db)
    db.commit()

    policy = db.query(SpendPolicy).first()
    assert policy is not None
    assert policy.monthly_limit_paise == 100_000        # Rs.1,000  PRD s4.3
    assert policy.approval_threshold_paise == 50_000    # Rs.500    PRD s4.3
    assert policy.protected_buckets == [Bucket.EMERGENCY_SAVINGS.value]
    assert policy.paused is False
    # PRD is silent on a per-transaction cap; it must stay unset, not guessed.
    assert policy.per_tx_limit_paise is None


def test_pool_matches_prd_demo_shape(db) -> None:
    demo_data.seed_all(db)
    db.commit()

    cycle = db.query(PoolCycle).one()
    assert cycle.size == 10                                  # PRD s4.1
    assert cycle.contribution_amount_paise == 50_000         # Rs.500
    assert cycle.rules["cycle_amount_paise"] == 500_000      # Rs.5,000
    assert len(cycle.members) == 10


def test_every_pool_allocation_states_a_reason(db) -> None:
    """PRD s4.1: every allocation must be explainable."""
    demo_data.seed_all(db)
    db.commit()

    allocations = db.query(PoolAllocation).all()
    assert allocations
    for allocation in allocations:
        assert allocation.reason and len(allocation.reason.strip()) > 20


def test_every_reward_and_offer_declares_a_funding_source(db) -> None:
    """PRD s4.2: reward economics are stated, never implied."""
    demo_data.seed_all(db)
    db.commit()

    for offer in db.query(Offer).all():
        assert offer.funding_source is not None
    for reward in db.query(Reward).all():
        assert reward.source is not None


# ---------------------------------------------------------------------------
# Synthetic labelling (PRD s8.2)
# ---------------------------------------------------------------------------


def test_all_seeded_records_are_flagged_synthetic(db) -> None:
    demo_data.seed_all(db)
    db.commit()

    assert all(u.is_synthetic for u in db.query(User).all())
    assert all(o.is_synthetic for o in db.query(Offer).all())
    assert all(r.is_synthetic for r in db.query(Reward).all())
    assert all(p.is_synthetic for p in db.query(PoolCycle).all())


def test_user_visible_strings_are_labelled_as_demo(db) -> None:
    """A screenshot must never be mistakable for real merchant data."""
    demo_data.seed_all(db)
    db.commit()

    markers = ("demo", "synthetic", "sample", "mock", "test", "faux")
    for user in db.query(User).all():
        assert any(m in user.name.lower() for m in markers), user.name
    for offer in db.query(Offer).all():
        assert any(m in offer.merchant.lower() for m in markers), offer.merchant


# ---------------------------------------------------------------------------
# Demo state is actually usable
# ---------------------------------------------------------------------------


def test_established_user_has_savings_and_headroom(db) -> None:
    """The demo needs a user with history AND room to act, or nothing is showable."""
    demo_data.seed_all(db)
    db.commit()

    aarav = db.query(User).filter_by(name=demo_data.PRIMARY_DEMO_USER).one()
    balances = ledger_service.get_balances(db, aarav.id)

    assert balances[Bucket.EMERGENCY_SAVINGS.value] == 150_000  # Rs.1,500 from 3 x Rs.500

    # Discretionary is read as spend-against-limit, never as a balance.
    summary = ledger_service.get_month_spend_summary(
        db, aarav.id, monthly_limit_paise=demo_data.MONTHLY_LIMIT_PAISE
    )
    assert summary["used_paise"] == 24_000        # Rs.240
    assert summary["remaining_paise"] == 76_000   # headroom for the demo
    assert summary["pct_used"] == 24.0


def test_new_user_starts_empty(db) -> None:
    """Empty states are where display bugs hide, so the demo includes one."""
    demo_data.seed_all(db)
    db.commit()

    diya = db.query(User).filter(User.name.like("Diya%")).one()
    assert ledger_service.get_balance(db, diya.id, Bucket.EMERGENCY_SAVINGS) == 0
    assert all(v == 0 for v in ledger_service.get_balances(db, diya.id).values())
    assert ledger_service.get_event_count(db, diya.id) == 0
    # ...but she still has rules and a goal, so the agent can talk to her.
    assert db.query(SpendPolicy).filter_by(user_id=diya.id).one() is not None
    assert db.query(Goal).filter_by(user_id=diya.id).one() is not None


def test_seed_includes_an_expired_offer_for_edge_case_testing(db) -> None:
    """PRD s10: an expired offer must be hidden, never fabricated into a discount."""
    from datetime import datetime, timezone

    demo_data.seed_all(db)
    db.commit()

    now = datetime.now(timezone.utc)
    expiries = [o.expiry for o in db.query(Offer).all() if o.expiry is not None]
    normalised = [e if e.tzinfo else e.replace(tzinfo=timezone.utc) for e in expiries]
    assert any(e < now for e in normalised), "seed should include an expired offer"
    assert any(e > now for e in normalised), "seed should include live offers"


# ---------------------------------------------------------------------------
# Seeding goes through the real code paths
# ---------------------------------------------------------------------------


def test_seed_audit_chain_verifies(db) -> None:
    """Seeding writes through ledger_service, so the audit chain must hold."""
    demo_data.seed_all(db)
    db.commit()

    result = audit_service.verify_chain(db)
    assert result.ok is True
    assert result.checked > 0
