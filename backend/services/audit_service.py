"""Append-only, tamper-evident audit trail.

PRD s6 makes "every money action traceable" an acceptance criterion, and s6.1
requires honest exception reporting. This service is how both are satisfied:
every agent tool call (including refused ones), every policy decision and every
state transition is recorded here, and the record is chained so that later
alteration is detectable.

WHY A HASH CHAIN, not just "we never write UPDATE":
    A convention protects against accident. It does not protect against anyone
    with database access - which, in a review of a financial system, is exactly
    the threat being asked about. Each entry commits to its own contents AND to
    the previous entry's hash, so editing or deleting any historical row breaks
    every hash after it. verify_chain() reports the first broken index.

    This does not make the log immutable; nothing inside a single database can.
    It makes forgery LOUD instead of silent, which is the property that matters.
    Write-once external storage (S3 Object Lock / WORM) is the production step
    beyond this - see CampusPool_Production_Readiness.md s3.4.

This module deliberately exposes NO update or delete function.
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.models.entities import AuditActor, AuditEvent

logger = logging.getLogger("campuspool.audit")

# The chain's anchor. The first entry's prev_hash is this value, so an attacker
# cannot make a forged entry look like the legitimate start of the chain.
GENESIS_HASH = "0" * 64


def canonical_timestamp(value: datetime) -> str:
    """Normalise a datetime to one exact string form for hashing.

    This exists because SQLite does not preserve tzinfo: a timezone-aware UTC
    datetime written to the database reads back NAIVE, so isoformat() would
    produce a different string on verification than it did on write, and every
    entry would look tampered with. Normalising to a fixed UTC format on both
    the write and the verify path makes the hash stable across a round trip.

    A naive value is treated as UTC, because everything this codebase writes is
    UTC by construction (see entities._utcnow).
    """
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")


def _canonical(value: Any) -> str:
    """Deterministic JSON for hashing.

    sort_keys matters: {"a":1,"b":2} and {"b":2,"a":1} are the same fact and
    must hash identically, or verification would fail on dict ordering alone.
    """
    if value is None:
        return ""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def compute_entry_hash(
    *,
    prev_hash: str,
    actor: str,
    action: str,
    user_id: str | None,
    intent_id: str | None,
    inputs_hash: str | None,
    policy_result: dict | None,
    provider_result: dict | None,
    created_at: datetime,
) -> str:
    """Hash one entry over its full content plus the previous hash.

    Field order is fixed and separated by a character that cannot appear in the
    values, so two different entries cannot produce the same input string.
    """
    payload = "\x1f".join(
        [
            prev_hash,
            actor,
            action,
            user_id or "",
            intent_id or "",
            inputs_hash or "",
            _canonical(policy_result),
            _canonical(provider_result),
            canonical_timestamp(created_at),
        ]
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def hash_inputs(inputs: Any) -> str:
    """Hash tool arguments for the trail.

    Stored as a hash rather than raw text so the trail proves WHAT was passed
    without copying user content into a second place - which keeps the audit
    table out of scope for data-deletion requests (see Production Readiness s4.9).
    """
    return hashlib.sha256(_canonical(inputs).encode("utf-8")).hexdigest()


def _last_entry(session: Session) -> AuditEvent | None:
    return session.execute(select(AuditEvent).order_by(AuditEvent.seq.desc()).limit(1)).scalar_one_or_none()


def write(
    session: Session,
    *,
    actor: AuditActor,
    action: str,
    user_id: str | None = None,
    intent_id: str | None = None,
    inputs: Any = None,
    policy_result: dict | None = None,
    provider_result: dict | None = None,
) -> AuditEvent:
    """Append one entry to the chain.

    Never raises on a caller's behalf: audit failures must surface (Playbook
    A.4), because an action that happened without a trail is worse than one
    that did not happen.
    """
    previous = _last_entry(session)
    prev_hash = previous.entry_hash if previous is not None else GENESIS_HASH
    seq = (previous.seq + 1) if previous is not None else 1

    created_at = datetime.now(timezone.utc)
    inputs_digest = hash_inputs(inputs) if inputs is not None else None

    entry_hash = compute_entry_hash(
        prev_hash=prev_hash,
        actor=actor.value,
        action=action,
        user_id=user_id,
        intent_id=intent_id,
        inputs_hash=inputs_digest,
        policy_result=policy_result,
        provider_result=provider_result,
        created_at=created_at,
    )

    event = AuditEvent(
        actor=actor,
        action=action,
        user_id=user_id,
        intent_id=intent_id,
        inputs_hash=inputs_digest,
        policy_result=policy_result,
        provider_result=provider_result,
        seq=seq,
        prev_hash=prev_hash,
        entry_hash=entry_hash,
        created_at=created_at,
    )
    session.add(event)
    session.flush()  # persist within the caller's transaction, without committing it

    logger.info("audit actor=%s action=%s seq=%s", actor.value, action, event.seq)
    return event


class ChainVerification:
    """Result of verifying the audit chain."""

    def __init__(self, *, ok: bool, checked: int, broken_at_seq: int | None, reason: str | None) -> None:
        self.ok = ok
        self.checked = checked
        self.broken_at_seq = broken_at_seq
        self.reason = reason

    def __repr__(self) -> str:
        if self.ok:
            return f"<ChainVerification ok checked={self.checked}>"
        return f"<ChainVerification BROKEN at seq={self.broken_at_seq}: {self.reason}>"

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "entries_checked": self.checked,
            "broken_at_seq": self.broken_at_seq,
            "reason": self.reason,
        }


def verify_chain(session: Session) -> ChainVerification:
    """Walk the whole chain and report the first break, if any.

    Detects all three tampering shapes:
      - a MODIFIED row (its recomputed hash no longer matches its stored hash)
      - a DELETED row  (the next row's prev_hash no longer matches its predecessor)
      - an INSERTED row (same linkage failure)
    """
    events = session.execute(select(AuditEvent).order_by(AuditEvent.seq.asc())).scalars().all()

    expected_prev = GENESIS_HASH
    for index, event in enumerate(events):
        if event.prev_hash != expected_prev:
            return ChainVerification(
                ok=False,
                checked=index,
                broken_at_seq=event.seq,
                reason=(
                    "prev_hash does not match the preceding entry - a row was "
                    "deleted, inserted, or reordered"
                ),
            )

        recomputed = compute_entry_hash(
            prev_hash=event.prev_hash,
            actor=event.actor.value if hasattr(event.actor, "value") else str(event.actor),
            action=event.action,
            user_id=event.user_id,
            intent_id=event.intent_id,
            inputs_hash=event.inputs_hash,
            policy_result=event.policy_result,
            provider_result=event.provider_result,
            created_at=event.created_at,
        )
        if recomputed != event.entry_hash:
            return ChainVerification(
                ok=False,
                checked=index,
                broken_at_seq=event.seq,
                reason="entry contents do not match its stored hash - this row was modified",
            )

        expected_prev = event.entry_hash

    return ChainVerification(ok=True, checked=len(events), broken_at_seq=None, reason=None)
