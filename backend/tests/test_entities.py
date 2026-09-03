"""Phase 1 tests: do the schema's guarantees actually hold?

A constraint you have never watched reject bad data is a constraint you are
merely hoping exists. Each test here forces one guarantee to prove itself.
"""

from __future__ import annotations

import pytest
from sqlalchemy.exc import IntegrityError

from backend.models.entities import (
    ActionIntent,
    Bucket,
    Goal,
    IntentStatus,
    IntentType,
    LedgerEvent,
    LedgerEventType,
    Offer,
    RewardSource,
    SpendPolicy,
    User,
    UserStatus,
    new_id,
)


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def make_user(db, name: str = "Demo Student") -> User:
    user = User(name=name)
    db.add(user)
    db.commit()
    return user


def make_intent(db, user: User, *, client_ref: str, amount_paise: int = 50000) -> ActionIntent:
    intent = ActionIntent(
        user_id=user.id,
        type=IntentType.CONTRIBUTION,
        amount_paise=amount_paise,
        purpose="savings_goal:test",
        client_ref=client_ref,
    )
    db.add(intent)
    db.commit()
    return intent


# --------------------------------------------------------------------------
# Identifiers
# --------------------------------------------------------------------------


def test_ids_are_prefixed_and_unique(db) -> None:
    """Prefixed IDs make a stray ID in a log line self-describing."""
    user_a = make_user(db, "A")
    user_b = make_user(db, "B")
    assert user_a.id.startswith("usr_")
    assert user_a.id != user_b.id


def test_new_id_prefixes_are_distinct_per_kind() -> None:
    assert new_id("int").startswith("int_")
    assert new_id("led").startswith("led_")


# --------------------------------------------------------------------------
# Enum storage
# --------------------------------------------------------------------------


def test_enums_persist_as_readable_strings(db) -> None:
    """Stored as text, so the SQLite file stays inspectable by a human."""
    user = make_user(db)
    raw = db.execute(
        LedgerEvent.__table__.select()  # no rows yet; check users instead
    ).all()
    assert raw == []

    stored_status = db.execute(
        User.__table__.select().where(User.id == user.id)
    ).one()._mapping["status"]
    assert stored_status == "active"
    assert user.status is UserStatus.ACTIVE


# --------------------------------------------------------------------------
# Money conventions
# --------------------------------------------------------------------------


def test_ledger_amounts_are_signed_integers(db) -> None:
    """Positive credits a bucket, negative debits it. No floats anywhere."""
    user = make_user(db)
    db.add_all(
        [
            LedgerEvent(
                user_id=user.id,
                type=LedgerEventType.CONTRIBUTION,
                amount_paise=50000,
                bucket=Bucket.EMERGENCY_SAVINGS,
                source="seed:opening_balance",
            ),
            LedgerEvent(
                user_id=user.id,
                type=LedgerEventType.PURCHASE,
                amount_paise=-12000,
                bucket=Bucket.DISCRETIONARY,
                source="razorpay_payment:pay_demo",
            ),
        ]
    )
    db.commit()

    events = db.query(LedgerEvent).filter_by(user_id=user.id).all()
    assert len(events) == 2
    assert all(isinstance(e.amount_paise, int) for e in events)
    assert sum(e.amount_paise for e in events) == 38000


def test_zero_amount_ledger_event_is_rejected(db) -> None:
    """A zero-value money event is always a bug, so the DB refuses it."""
    user = make_user(db)
    db.add(
        LedgerEvent(
            user_id=user.id,
            type=LedgerEventType.CONTRIBUTION,
            amount_paise=0,
            bucket=Bucket.EMERGENCY_SAVINGS,
            source="test:zero",
        )
    )
    with pytest.raises(IntegrityError):
        db.commit()


def test_non_positive_intent_amount_is_rejected(db) -> None:
    user = make_user(db)
    db.add(
        ActionIntent(
            user_id=user.id,
            type=IntentType.PURCHASE,
            amount_paise=0,
            purpose="purchase:test",
            client_ref="ref-zero",
        )
    )
    with pytest.raises(IntegrityError):
        db.commit()


# --------------------------------------------------------------------------
# Idempotency (Guardrail 4)
# --------------------------------------------------------------------------


def test_duplicate_client_ref_is_refused_by_the_database(db) -> None:
    """The idempotency guarantee must not depend on application code alone.

    Even if a service-layer check is bypassed or racy, the unique index means a
    second intent for the same logical action cannot exist.
    """
    user = make_user(db)
    make_intent(db, user, client_ref="user1|contribution|50000|2026-09")

    db.add(
        ActionIntent(
            user_id=user.id,
            type=IntentType.CONTRIBUTION,
            amount_paise=50000,
            purpose="savings_goal:test",
            client_ref="user1|contribution|50000|2026-09",  # same ref
        )
    )
    with pytest.raises(IntegrityError):
        db.commit()


# --------------------------------------------------------------------------
# Referential integrity
# --------------------------------------------------------------------------


def test_foreign_keys_are_enforced(db) -> None:
    """SQLite ignores FKs unless PRAGMA foreign_keys=ON; prove ours is on."""
    db.add(
        Goal(
            user_id="usr_does_not_exist",
            target_amount_paise=500000,
        )
    )
    with pytest.raises(IntegrityError):
        db.commit()


def test_one_spend_policy_per_user(db) -> None:
    user = make_user(db)
    db.add(
        SpendPolicy(
            user_id=user.id,
            monthly_limit_paise=100000,
            approval_threshold_paise=50000,
            protected_buckets=[Bucket.EMERGENCY_SAVINGS.value],
        )
    )
    db.commit()

    db.add(
        SpendPolicy(
            user_id=user.id,
            monthly_limit_paise=999999,
            approval_threshold_paise=1,
            protected_buckets=[],
        )
    )
    with pytest.raises(IntegrityError):
        db.commit()


# --------------------------------------------------------------------------
# Product guardrails encoded in the schema
# --------------------------------------------------------------------------


def test_intents_start_in_proposed_state(db) -> None:
    """Nothing may be born already authorized (PRD s5.5)."""
    user = make_user(db)
    intent = make_intent(db, user, client_ref="ref-initial-state")
    assert intent.status is IntentStatus.PROPOSED
    assert intent.policy_result is None
    assert intent.provider_ref is None


def test_offer_requires_a_discount(db) -> None:
    """An offer with no discount at all is meaningless data."""
    db.add(
        Offer(
            merchant="DemoMart",
            title="Nothing off anything",
            category="fashion",
            funding_source=RewardSource.PARTNER_FUNDED,
        )
    )
    with pytest.raises(IntegrityError):
        db.commit()


def test_synthetic_flags_default_to_true(db) -> None:
    """PRD s8.2: all demo data must be labelled synthetic, by default."""
    user = make_user(db)
    offer = Offer(
        merchant="DemoMart",
        title="10% off fashion",
        category="fashion",
        discount_pct=10.0,
        funding_source=RewardSource.PARTNER_FUNDED,
    )
    db.add(offer)
    db.commit()
    assert user.is_synthetic is True
    assert offer.is_synthetic is True


# --------------------------------------------------------------------------
# DPDP-readiness field design (Phase 1 Step 6)
# --------------------------------------------------------------------------


def test_user_records_a_purpose(db) -> None:
    """A consent notice and an access request both have to answer 'why held?'."""
    user = make_user(db)
    assert user.purpose == "demo_account"


def test_retention_is_unset_rather_than_guessed(db) -> None:
    """The retention PERIOD is a legal decision, not an engineering default.

    Master build plan Part D: DPDP erasure rights conflict with financial
    record-keeping duties, and resolving that needs counsel. So the column
    exists (cheap now, painful to retrofit) but carries no invented value.
    """
    user = make_user(db)
    assert user.retention_until is None


def test_data_classification_is_documented(db) -> None:
    """The classification is the map you hand a privacy reviewer; keep it present."""
    from backend.models import entities

    doc = entities.__doc__ or ""
    assert "DATA CLASSIFICATION" in doc
    for bucket in ("SYNTHETIC-ONLY", "WOULD-BE-PERSONAL-DATA", "FINANCIAL-RECORD"):
        assert bucket in doc, f"{bucket} missing from the data classification"
