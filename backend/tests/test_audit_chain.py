"""Tests for audit trail tamper-evidence.

Answers the production-review question "how do you prevent admins from
modifying logs?" honestly: you cannot PREVENT it inside one database, so you
make it DETECTABLE. These tests tamper with the table directly via SQL - the
way someone with database access would - and assert the chain catches it.
"""

from __future__ import annotations

from sqlalchemy import text

from backend.models.entities import AuditActor, AuditEvent, User
from backend.services import audit_service


def _seed_user(db) -> User:
    user = User(name="Demo Student")
    db.add(user)
    db.commit()
    return user


def _write_some_events(db, user: User, count: int = 5) -> list[AuditEvent]:
    events = []
    for i in range(count):
        events.append(
            audit_service.write(
                db,
                actor=AuditActor.LLM,
                action=f"tool:get_wallet_or_ledger#{i}",
                user_id=user.id,
                inputs={"user_id": user.id, "step": i},
            )
        )
    db.commit()
    return events


# --------------------------------------------------------------------------
# Happy path
# --------------------------------------------------------------------------


def test_chain_verifies_when_untouched(db) -> None:
    user = _seed_user(db)
    _write_some_events(db, user, count=5)

    result = audit_service.verify_chain(db)
    assert result.ok is True
    assert result.checked == 5
    assert result.broken_at_seq is None


def test_first_entry_anchors_to_genesis(db) -> None:
    """An attacker must not be able to forge a new 'start' of the chain."""
    user = _seed_user(db)
    events = _write_some_events(db, user, count=1)
    assert events[0].prev_hash == audit_service.GENESIS_HASH


def test_each_entry_links_to_its_predecessor(db) -> None:
    user = _seed_user(db)
    events = _write_some_events(db, user, count=4)
    for earlier, later in zip(events, events[1:]):
        assert later.prev_hash == earlier.entry_hash


def test_empty_chain_is_valid(db) -> None:
    assert audit_service.verify_chain(db).ok is True


# --------------------------------------------------------------------------
# Tampering — the tests that actually matter
# --------------------------------------------------------------------------


def test_modified_row_is_detected(db) -> None:
    """Someone edits a policy decision after the fact to hide a denial."""
    user = _seed_user(db)
    events = _write_some_events(db, user, count=5)
    target_seq = events[2].seq

    db.execute(
        text("UPDATE audit_events SET action = :forged WHERE seq = :seq"),
        {"forged": "tool:definitely_was_allowed", "seq": target_seq},
    )
    db.commit()
    db.expire_all()

    result = audit_service.verify_chain(db)
    assert result.ok is False
    assert result.broken_at_seq == target_seq
    assert "modified" in (result.reason or "")


def test_deleted_row_is_detected(db) -> None:
    """Someone removes the record of an action entirely."""
    user = _seed_user(db)
    events = _write_some_events(db, user, count=5)
    removed_seq = events[2].seq

    db.execute(text("DELETE FROM audit_events WHERE seq = :seq"), {"seq": removed_seq})
    db.commit()
    db.expire_all()

    result = audit_service.verify_chain(db)
    assert result.ok is False
    # The break surfaces at the row AFTER the hole, whose prev_hash now dangles.
    assert result.broken_at_seq == events[3].seq
    assert "deleted" in (result.reason or "")


def test_tampered_policy_result_is_detected(db) -> None:
    """The highest-value forgery: rewriting a DENY into an ALLOW."""
    user = _seed_user(db)
    audit_service.write(
        db,
        actor=AuditActor.BACKEND,
        action="forced_policy_check",
        user_id=user.id,
        policy_result={"decision": "DENY", "reason": "exceeds monthly limit"},
    )
    db.commit()

    assert audit_service.verify_chain(db).ok is True

    db.execute(
        text("UPDATE audit_events SET policy_result = :forged"),
        {"forged": '{"decision": "ALLOW", "reason": "fine"}'},
    )
    db.commit()
    db.expire_all()

    result = audit_service.verify_chain(db)
    assert result.ok is False
    assert "modified" in (result.reason or "")


def test_appending_after_tampering_still_fails(db) -> None:
    """Tampering cannot be 'healed' by writing more entries afterwards."""
    user = _seed_user(db)
    events = _write_some_events(db, user, count=3)

    db.execute(
        text("UPDATE audit_events SET action = 'forged' WHERE seq = :seq"),
        {"seq": events[1].seq},
    )
    db.commit()
    db.expire_all()

    audit_service.write(db, actor=AuditActor.SYSTEM, action="tool:later_event", user_id=user.id)
    db.commit()

    assert audit_service.verify_chain(db).ok is False


# --------------------------------------------------------------------------
# Hashing behaviour
# --------------------------------------------------------------------------


def test_input_hashing_is_order_independent(db) -> None:
    """Same facts, different dict ordering, must hash identically."""
    assert audit_service.hash_inputs({"a": 1, "b": 2}) == audit_service.hash_inputs({"b": 2, "a": 1})


def test_input_hashing_distinguishes_different_inputs(db) -> None:
    assert audit_service.hash_inputs({"amount_paise": 50000}) != audit_service.hash_inputs({"amount_paise": 500000})


def test_raw_inputs_are_not_stored(db) -> None:
    """Only a digest is persisted, never the raw argument text."""
    user = _seed_user(db)
    audit_service.write(
        db,
        actor=AuditActor.LLM,
        action="tool:create_payment_intent",
        user_id=user.id,
        inputs={"secret_note": "sensitive-value-should-not-persist"},
    )
    db.commit()

    stored = db.execute(text("SELECT inputs_hash FROM audit_events")).scalar_one()
    assert stored is not None
    assert len(stored) == 64
    assert "sensitive-value" not in stored


def test_blocked_tool_calls_are_recorded_too(db) -> None:
    """A refused action must leave a trace, not vanish (PRD s6)."""
    user = _seed_user(db)
    audit_service.write(
        db,
        actor=AuditActor.LLM,
        action="blocked_tool_call:process_test_payout",
        user_id=user.id,
        inputs={"amount_paise": 500000},
    )
    db.commit()

    actions = db.execute(text("SELECT action FROM audit_events")).scalars().all()
    assert any(a.startswith("blocked_tool_call:") for a in actions)
    assert audit_service.verify_chain(db).ok is True
