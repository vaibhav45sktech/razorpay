"""Synthetic demo data. Run with: python -m backend.seed.demo_data

EVERYTHING IN THIS FILE IS FAKE. No real student, merchant, offer, pool member
or payout is represented. PRD s8.2 requires all demo data to be labelled
synthetic in both code and UI copy, so every seeded record carries
is_synthetic=True and every user-visible string says "(demo)" or similar. A
screenshot of this app must never be mistakable for real merchant data.

IDEMPOTENT: running this twice does not duplicate anything. It checks for an
existing marker user and exits unless --reset is passed. "Why do I have forty
offers" is a real afternoon lost, so the test suite pins this behaviour.

Values trace to the PRD:
- Contribution band Rs.100-Rs.500          (PRD s1)
- Monthly discretionary limit Rs.1,000     (PRD s4.3)
- Approval threshold Rs.500                (PRD s4.3)
- Emergency savings protected              (PRD s4.3)
- Pool demo: 10 members x Rs.500 = Rs.5,000 (PRD s4.1)

Nothing here is invented product policy. Where the PRD is silent, the value is
marked TODO rather than guessed.
"""

from __future__ import annotations

import argparse
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from backend.models import db as database
from backend.models.entities import (
    ActionIntent,
    Approval,
    AuditEvent,
    Bucket,
    ExceptionRecord,
    Goal,
    LedgerEvent,
    LedgerEventType,
    Offer,
    PoolAllocation,
    PoolCycle,
    PoolCycleStatus,
    Reward,
    RewardSource,
    RewardStatus,
    SpendPolicy,
    Suggestion,
    User,
)
from backend.services import ledger_service

logger = logging.getLogger("campuspool.seed")

# Marker used to detect an already-seeded database.
PRIMARY_DEMO_USER = "Aarav (demo student)"

# ---- Values from the PRD, in paise ----
RUPEE = 100
CONTRIBUTION_MIN_PAISE = 100 * RUPEE       # Rs.100   (PRD s1)
CONTRIBUTION_MAX_PAISE = 500 * RUPEE       # Rs.500   (PRD s1)
MONTHLY_LIMIT_PAISE = 1_000 * RUPEE        # Rs.1,000 (PRD s4.3)
APPROVAL_THRESHOLD_PAISE = 500 * RUPEE     # Rs.500   (PRD s4.3)
POOL_SIZE = 10                             # PRD s4.1
POOL_CONTRIBUTION_PAISE = 500 * RUPEE      # PRD s4.1

# Tables cleared by --reset, in FK-safe order (children before parents).
_RESET_ORDER = (
    Suggestion,
    AuditEvent,
    ExceptionRecord,
    Approval,
    LedgerEvent,
    ActionIntent,
    PoolAllocation,
    PoolCycle,
    Reward,
    Offer,
    SpendPolicy,
    Goal,
    User,
)


def is_seeded(session: Session) -> bool:
    return (
        session.execute(select(User).where(User.name == PRIMARY_DEMO_USER)).scalar_one_or_none()
        is not None
    )


def reset(session: Session) -> None:
    """Delete all demo data. Safe because every row in this prototype is synthetic."""
    for model in _RESET_ORDER:
        session.execute(delete(model))
    session.flush()
    logger.info("Cleared %d tables", len(_RESET_ORDER))


# ---------------------------------------------------------------------------
# Seed sections
# ---------------------------------------------------------------------------


def seed_users(session: Session) -> list[User]:
    """Two demo students: one with savings history, one brand new.

    The second exists so empty-state behaviour gets exercised - a zero balance,
    no transactions, no streak. Empty states are where display bugs hide.
    """
    aarav = User(name=PRIMARY_DEMO_USER, is_synthetic=True)
    diya = User(name="Diya (demo student, new account)", is_synthetic=True)
    session.add_all([aarav, diya])
    session.flush()
    logger.info("Seeded 2 demo users")
    return [aarav, diya]


def seed_policies(session: Session, users: list[User]) -> None:
    """Per-user spending rules from PRD s4.3."""
    for user in users:
        session.add(
            SpendPolicy(
                user_id=user.id,
                monthly_limit_paise=MONTHLY_LIMIT_PAISE,
                approval_threshold_paise=APPROVAL_THRESHOLD_PAISE,
                # PRD does not define a per-transaction cap.
                # TODO: confirm per_tx_limit_paise with product owner.
                per_tx_limit_paise=None,
                protected_buckets=[Bucket.EMERGENCY_SAVINGS.value],
                paused=False,
            )
        )
    session.flush()
    logger.info("Seeded spend policies for %d users", len(users))


def seed_goals(session: Session, users: list[User]) -> list[Goal]:
    """One emergency-cushion goal each. Progress is DERIVED from the ledger."""
    goals = [
        Goal(
            user_id=users[0].id,
            label="Emergency cushion (demo)",
            target_amount_paise=5_000 * RUPEE,   # Rs.5,000
            cadence="monthly",
        ),
        Goal(
            user_id=users[1].id,
            label="First emergency cushion (demo)",
            target_amount_paise=2_000 * RUPEE,   # Rs.2,000
            cadence="monthly",
        ),
    ]
    session.add_all(goals)
    session.flush()
    logger.info("Seeded %d goals", len(goals))
    return goals


def seed_ledger_history(session: Session, users: list[User]) -> None:
    """Three months of contributions for the established user; none for the new one.

    Written through ledger_service.append rather than raw inserts, so the seed
    exercises the same validation and audit path as production code. If seeding
    breaks, the ledger contract broke.
    """
    aarav = users[0]
    now = datetime.now(timezone.utc)

    # Three prior monthly contributions of Rs.500, backdated.
    for months_ago in (3, 2, 1):
        event = ledger_service.append(
            session,
            user_id=aarav.id,
            type=LedgerEventType.CONTRIBUTION,
            amount_paise=CONTRIBUTION_MAX_PAISE,
            bucket=Bucket.EMERGENCY_SAVINGS,
            source="seed:demo_contribution",
        )
        event.created_at = now - timedelta(days=30 * months_ago)

    # A little discretionary spending this month, so limits are visibly in play
    # without being near the cap - Rs.240 of the Rs.1,000 monthly limit.
    ledger_service.append(
        session,
        user_id=aarav.id,
        type=LedgerEventType.PURCHASE,
        amount_paise=-240 * RUPEE,
        bucket=Bucket.DISCRETIONARY,
        source="seed:demo_purchase",
    )

    session.flush()
    logger.info("Seeded ledger history (Rs.1,500 saved, Rs.240 spent this month)")


def seed_offers(session: Session) -> list[Offer]:
    """Synthetic partner offers across the PRD s4.2 categories.

    Merchant names are obviously fake on purpose. Funding source is explicit on
    every row, because PRD s4.2 requires reward economics to be stated rather
    than implied.
    """
    expiry = datetime.now(timezone.utc) + timedelta(days=30)
    offers = [
        Offer(
            merchant="DemoMart (synthetic)",
            title="Rs.200 off headphones",
            category="electronics",
            list_price_paise=2_000 * RUPEE,      # PRD s4.2 worked example
            discount_paise=200 * RUPEE,
            expiry=expiry,
            funding_source=RewardSource.PARTNER_FUNDED,
            eligibility={"min_contributions": 1},
        ),
        Offer(
            merchant="FauxThreads (synthetic)",
            title="10% off fashion order",
            category="fashion",
            discount_pct=10.0,
            expiry=expiry,
            funding_source=RewardSource.PARTNER_FUNDED,
            eligibility={},
        ),
        Offer(
            merchant="Sample Cafe (synthetic)",
            title="Rs.50 off campus meals over Rs.300",
            category="food",
            discount_paise=50 * RUPEE,
            expiry=expiry,
            funding_source=RewardSource.PARTNER_FUNDED,
            eligibility={"min_order_paise": 300 * RUPEE},
        ),
        Offer(
            merchant="TestTunes (synthetic)",
            title="2 months free on student music plan",
            category="subscriptions",
            discount_paise=199 * RUPEE,
            expiry=expiry,
            funding_source=RewardSource.PLATFORM_FUNDED,
            eligibility={"streak_months": 3},
        ),
        Offer(
            merchant="MockBooks (synthetic)",
            title="15% off textbooks",
            category="education",
            discount_pct=15.0,
            expiry=expiry,
            funding_source=RewardSource.PARTNER_FUNDED,
            eligibility={},
        ),
        Offer(
            merchant="ExpiredCo (synthetic)",
            title="Rs.500 off - EXPIRED, for testing",
            category="electronics",
            discount_paise=500 * RUPEE,
            expiry=datetime.now(timezone.utc) - timedelta(days=2),
            funding_source=RewardSource.PARTNER_FUNDED,
            eligibility={},
        ),
    ]
    session.add_all(offers)
    session.flush()
    logger.info("Seeded %d synthetic offers (1 deliberately expired)", len(offers))
    return offers


def seed_rewards(session: Session, users: list[User]) -> None:
    """Milestone rewards, with funding source explicit (PRD s4.2)."""
    aarav = users[0]
    session.add_all(
        [
            Reward(
                user_id=aarav.id,
                label="3-month savings streak (demo)",
                source=RewardSource.PLATFORM_FUNDED,
                amount_paise=100 * RUPEE,
                eligibility={"streak_months": 3},
                status=RewardStatus.ELIGIBLE,
            ),
            Reward(
                user_id=aarav.id,
                label="Reach Rs.5,000 cushion (demo)",
                source=RewardSource.PLATFORM_FUNDED,
                amount_paise=250 * RUPEE,
                eligibility={"target_balance_paise": 5_000 * RUPEE},
                status=RewardStatus.LOCKED,
            ),
        ]
    )
    session.flush()
    logger.info("Seeded 2 rewards (1 eligible, 1 locked)")


def seed_pool(session: Session, users: list[User]) -> PoolCycle:
    """The PRD s4.1 demo cycle: 10 members x Rs.500 = Rs.5,000 virtual amount.

    IMPORTANT: this cycle holds NO money. It records rules and membership only.
    Each participant keeps an individual ledger, and test_pool_invariant
    (Phase 3) asserts that no code path can ever produce a pooled balance -
    see CampusPool_Production_Readiness.md s2 on why that matters legally.
    """
    member_ids = [users[0].id, users[1].id] + [f"usr_synthetic_member_{i}" for i in range(3, POOL_SIZE + 1)]

    cycle = PoolCycle(
        label="Demo cycle #1 (synthetic)",
        size=POOL_SIZE,
        contribution_amount_paise=POOL_CONTRIBUTION_PAISE,
        members=member_ids,
        rules={
            "description": (
                "Simulated community savings cycle inspired by chit-fund mechanics. "
                "Each of 10 members contributes Rs.500 per cycle, giving a virtual "
                "cycle amount of Rs.5,000. Early-access benefit is allocated by "
                "transparent rule, and every allocation states its reason. "
                "DEMO ONLY - no real money is pooled and each member's balance "
                "remains individually ledgered."
            ),
            "cycle_amount_paise": POOL_SIZE * POOL_CONTRIBUTION_PAISE,
            "allocation_rule": "contribution_consistency",
            # PRD s4.1 gives the example but not the discount arithmetic.
            # TODO: confirm reward/discount formula with product owner.
            "discount_formula": None,
        },
        status=PoolCycleStatus.ACTIVE,
        is_synthetic=True,
    )
    session.add(cycle)
    session.flush()

    session.add(
        PoolAllocation(
            cycle_id=cycle.id,
            user_id=users[0].id,
            amount_paise=200 * RUPEE,
            reason=(
                "Contributed on time in all 3 prior months, so qualifies for the "
                "consistency reward of Rs.200 from the cycle's reward pool (demo)."
            ),
        )
    )
    session.flush()
    logger.info("Seeded pool cycle (%d members) with 1 explained allocation", POOL_SIZE)
    return cycle


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------


def seed_all(session: Session, *, force: bool = False) -> bool:
    """Seed everything. Returns True if data was written, False if already seeded."""
    if is_seeded(session) and not force:
        logger.info("Database already seeded; nothing to do. Use --reset to reseed.")
        return False

    if force:
        reset(session)

    users = seed_users(session)
    seed_policies(session, users)
    seed_goals(session, users)
    seed_ledger_history(session, users)
    seed_offers(session)
    seed_rewards(session, users)
    seed_pool(session, users)
    return True


def main() -> None:
    parser = argparse.ArgumentParser(description="Seed synthetic CampusPool demo data.")
    parser.add_argument(
        "--reset",
        action="store_true",
        help="Delete all existing data first. Safe: every row in this prototype is synthetic.",
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)-8s %(message)s")

    database.create_all()
    with database.session_scope() as session:
        wrote = seed_all(session, force=args.reset)

    if wrote:
        with database.session_scope() as session:
            from backend.services import audit_service

            user_id = (
                session.execute(select(User).where(User.name == PRIMARY_DEMO_USER))
                .scalar_one()
                .id
            )
            print("\nSeeded. Demo state for", PRIMARY_DEMO_USER)

            # Balance buckets read as balances...
            for bucket, amount in ledger_service.get_balances(session, user_id).items():
                print(f"  {bucket:<20} Rs.{amount / 100:>10,.2f}")

            # ...and discretionary reads as spend-against-limit, never as a
            # negative balance. See the Bucket docstring in models/entities.py.
            spend = ledger_service.get_month_spend_summary(
                session, user_id, monthly_limit_paise=MONTHLY_LIMIT_PAISE
            )
            print(
                f"  {'spent this month':<20} Rs.{spend['used_paise'] / 100:>10,.2f}"
                f"  of Rs.{spend['limit_paise'] / 100:,.2f}"
                f"  ({spend['pct_used']}% used)"
            )

            chain = audit_service.verify_chain(session)
            print(f"  {'audit chain':<20} {'verified' if chain.ok else 'BROKEN':>13} "
                  f"({chain.checked} entries)")
            print("\nALL DATA IS SYNTHETIC / DEMO.")


if __name__ == "__main__":
    main()
