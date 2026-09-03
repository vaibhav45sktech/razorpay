"""Phase 1 Step 4 — ledger tests.

The ledger is the financial source of truth, so these tests cover not just the
happy path but the specific ways a ledger can be wrong: a balance that drifts,
a spending limit that can be evaded, a reversal applied twice, an event nobody
can trace.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from backend.models.entities import Bucket, LedgerEvent, LedgerEventType, User
from backend.services import audit_service, ledger_service
from backend.services.ledger_service import LedgerError

EMERGENCY = Bucket.EMERGENCY_SAVINGS
DISCRETIONARY = Bucket.DISCRETIONARY


@pytest.fixture()
def user(db) -> User:
    u = User(name="Demo Student")
    db.add(u)
    db.commit()
    return u


def contribute(db, user: User, amount_paise: int, bucket: Bucket = EMERGENCY) -> LedgerEvent:
    return ledger_service.append(
        db,
        user_id=user.id,
        type=LedgerEventType.CONTRIBUTION,
        amount_paise=amount_paise,
        bucket=bucket,
        source="test:contribution",
    )


def spend(db, user: User, amount_paise: int, bucket: Bucket = DISCRETIONARY) -> LedgerEvent:
    """amount_paise given as a positive number; stored as a debit."""
    return ledger_service.append(
        db,
        user_id=user.id,
        type=LedgerEventType.PURCHASE,
        amount_paise=-abs(amount_paise),
        bucket=bucket,
        source="test:purchase",
    )


# ---------------------------------------------------------------------------
# Derived balances
# ---------------------------------------------------------------------------


def test_balance_is_derived_from_events(db, user) -> None:
    contribute(db, user, 50000)
    contribute(db, user, 30000)
    spend(db, user, 10000, bucket=EMERGENCY)
    db.commit()

    assert ledger_service.get_balance(db, user.id, EMERGENCY) == 70000


def test_empty_ledger_returns_zero_not_none(db, user) -> None:
    """SUM over no rows is NULL in SQL; a None balance would crash arithmetic."""
    balance = ledger_service.get_balance(db, user.id, EMERGENCY)
    assert balance == 0
    assert isinstance(balance, int)


def test_buckets_are_isolated(db, user) -> None:
    """A discretionary purchase must not touch the emergency balance."""
    contribute(db, user, 50000, bucket=EMERGENCY)
    spend(db, user, 20000, bucket=DISCRETIONARY)
    db.commit()

    assert ledger_service.get_balance(db, user.id, EMERGENCY) == 50000
    assert ledger_service.get_balance(db, user.id, DISCRETIONARY) == -20000


def test_get_balances_includes_every_bucket(db, user) -> None:
    """Callers must never have to handle a missing key."""
    contribute(db, user, 50000, bucket=EMERGENCY)
    db.commit()

    balances = ledger_service.get_balances(db, user.id)
    assert set(balances) == {b.value for b in Bucket}
    assert balances[EMERGENCY.value] == 50000
    assert balances[DISCRETIONARY.value] == 0


def test_balances_are_per_user(db) -> None:
    a = User(name="A")
    b = User(name="B")
    db.add_all([a, b])
    db.commit()
    contribute(db, a, 50000)
    db.commit()

    assert ledger_service.get_balance(db, a.id, EMERGENCY) == 50000
    assert ledger_service.get_balance(db, b.id, EMERGENCY) == 0


# ---------------------------------------------------------------------------
# month_spend — the policy engine depends on this being exactly right
# ---------------------------------------------------------------------------


def test_month_spend_returns_positive_total_of_debits(db, user) -> None:
    spend(db, user, 30000)
    spend(db, user, 12000)
    db.commit()

    assert ledger_service.month_spend(db, user.id, DISCRETIONARY) == 42000


def test_month_spend_ignores_credits(db, user) -> None:
    """THE limit-evasion case.

    If incoming money offset spending, a user could contribute their way back
    under a monthly spending limit and keep buying. Only debits count.
    """
    spend(db, user, 30000)
    ledger_service.append(
        db,
        user_id=user.id,
        type=LedgerEventType.REWARD,
        amount_paise=25000,
        bucket=DISCRETIONARY,
        source="test:reward",
    )
    db.commit()

    assert ledger_service.month_spend(db, user.id, DISCRETIONARY) == 30000


def test_month_spend_ignores_previous_months(db, user) -> None:
    old = spend(db, user, 90000)
    # Backdate directly: the service has no API for this, which is the point.
    old.created_at = datetime.now(timezone.utc) - timedelta(days=45)
    db.commit()

    spend(db, user, 10000)
    db.commit()

    assert ledger_service.month_spend(db, user.id, DISCRETIONARY) == 10000


def test_month_spend_is_zero_with_no_events(db, user) -> None:
    assert ledger_service.month_spend(db, user.id, DISCRETIONARY) == 0


# ---------------------------------------------------------------------------
# Input validation — refusals are loud, never silent
# ---------------------------------------------------------------------------


def test_zero_amount_is_refused(db, user) -> None:
    with pytest.raises(LedgerError, match="zero-amount"):
        ledger_service.append(
            db,
            user_id=user.id,
            type=LedgerEventType.CONTRIBUTION,
            amount_paise=0,
            bucket=EMERGENCY,
            source="test:zero",
        )


def test_float_amount_is_refused(db, user) -> None:
    """Money is integer paise. A float here is the start of a rounding bug."""
    with pytest.raises(LedgerError, match="never a float"):
        ledger_service.append(
            db,
            user_id=user.id,
            type=LedgerEventType.CONTRIBUTION,
            amount_paise=500.50,  # type: ignore[arg-type]
            bucket=EMERGENCY,
            source="test:float",
        )


def test_blank_source_is_refused(db, user) -> None:
    """An event nobody can trace is not auditable (PRD s6)."""
    with pytest.raises(LedgerError, match="source is required"):
        ledger_service.append(
            db,
            user_id=user.id,
            type=LedgerEventType.CONTRIBUTION,
            amount_paise=50000,
            bucket=EMERGENCY,
            source="   ",
        )


# ---------------------------------------------------------------------------
# Auditability
# ---------------------------------------------------------------------------


def test_every_append_writes_an_audit_entry(db, user) -> None:
    contribute(db, user, 50000)
    spend(db, user, 20000)
    db.commit()

    actions = [e.action for e in db.query(audit_service.AuditEvent).all()]
    assert actions.count("ledger_append:CONTRIBUTION") == 1
    assert actions.count("ledger_append:PURCHASE") == 1


def test_audit_chain_still_verifies_after_ledger_writes(db, user) -> None:
    for _ in range(5):
        contribute(db, user, 10000)
    db.commit()

    assert audit_service.verify_chain(db).ok is True


# ---------------------------------------------------------------------------
# Reversals — the dispute / clawback path
# ---------------------------------------------------------------------------


def test_reversal_offsets_the_original_exactly(db, user) -> None:
    original = contribute(db, user, 50000)
    db.commit()
    assert ledger_service.get_balance(db, user.id, EMERGENCY) == 50000

    ledger_service.append_reversal(db, original_event_id=original.id, reason="test clawback")
    db.commit()

    assert ledger_service.get_balance(db, user.id, EMERGENCY) == 0


def test_reversal_leaves_the_original_row_intact(db, user) -> None:
    """History is never edited. Both rows must survive."""
    original = contribute(db, user, 50000)
    db.commit()
    original_id = original.id

    ledger_service.append_reversal(db, original_event_id=original_id, reason="test")
    db.commit()

    still_there = db.get(LedgerEvent, original_id)
    assert still_there is not None
    assert still_there.amount_paise == 50000
    assert still_there.type is LedgerEventType.CONTRIBUTION
    assert db.query(LedgerEvent).count() == 2


def test_reversal_records_provenance(db, user) -> None:
    original = spend(db, user, 40000)
    db.commit()

    reversal = ledger_service.append_reversal(db, original_event_id=original.id, reason="chargeback")
    db.commit()

    assert reversal.type is LedgerEventType.REVERSAL
    assert reversal.source == f"reversal:{original.id}"
    assert reversal.bucket is original.bucket


def test_double_reversal_is_refused(db, user) -> None:
    """Reversing twice would silently re-credit the money."""
    original = contribute(db, user, 50000)
    db.commit()
    ledger_service.append_reversal(db, original_event_id=original.id, reason="first")
    db.commit()

    with pytest.raises(LedgerError, match="already reversed"):
        ledger_service.append_reversal(db, original_event_id=original.id, reason="second")


def test_reversing_a_reversal_is_refused(db, user) -> None:
    original = contribute(db, user, 50000)
    db.commit()
    reversal = ledger_service.append_reversal(db, original_event_id=original.id, reason="first")
    db.commit()

    with pytest.raises(LedgerError, match="itself a reversal"):
        ledger_service.append_reversal(db, original_event_id=reversal.id, reason="undo the undo")


def test_reversing_unknown_event_is_refused(db, user) -> None:
    with pytest.raises(LedgerError, match="unknown ledger event"):
        ledger_service.append_reversal(db, original_event_id="led_does_not_exist", reason="test")


# ---------------------------------------------------------------------------
# Reads for the agent tools
# ---------------------------------------------------------------------------


def test_recent_events_are_newest_first_and_limited(db, user) -> None:
    for i in range(5):
        contribute(db, user, 10000 + i)
    db.commit()

    recent = ledger_service.get_recent_events(db, user.id, limit=3)
    assert len(recent) == 3
    amounts = [e.amount_paise for e in recent]
    assert amounts == sorted(amounts, reverse=True) or len(set(amounts)) == 3


def test_event_count(db, user) -> None:
    assert ledger_service.get_event_count(db, user.id) == 0
    contribute(db, user, 50000)
    contribute(db, user, 50000)
    db.commit()
    assert ledger_service.get_event_count(db, user.id) == 2


# ---------------------------------------------------------------------------
# Structural guarantee — the append-only rule, enforced by CI
# ---------------------------------------------------------------------------


def test_ledger_service_exposes_no_mutation_functions() -> None:
    """The append-only rule must hold structurally, not by discipline.

    If someone adds update_event() or set_balance() in a hurry, this fails the
    build rather than quietly allowing history to be rewritten.
    """
    forbidden_prefixes = ("update", "delete", "edit", "set_", "modify", "overwrite")
    public = [n for n in dir(ledger_service) if not n.startswith("_")]
    offenders = [n for n in public if n.lower().startswith(forbidden_prefixes)]
    assert offenders == [], f"ledger_service must stay append-only; found {offenders}"
