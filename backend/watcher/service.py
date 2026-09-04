"""The watcher loop: find new ledger events, run the rules, phrase, store.

State it keeps: none in memory that matters. The high-water mark ("which
ledger events have I already reacted to?") is derived from the database on
every pass — the newest LedgerEvent.created_at that already has a suggestion
pointing at it, or, on a cold start, "now" (so a fresh watcher does not
narrate months of seeded history). That makes the process safe to kill and
restart at any moment, which is the whole point of a background job.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from backend import config
from backend.models import db as database
from backend.models.entities import AuditActor, LedgerEvent, Suggestion, User, UserStatus
from backend.services import audit_service
from backend.watcher import phrasing
from backend.watcher.rules import Candidate, candidates_for_event, candidates_periodic

logger = logging.getLogger("campuspool.watcher")


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _already_stored(session: Session, user_id: str, c: Candidate, now: datetime) -> bool:
    """Same (user, kind, dedup_key) ever → skip. Same (user, kind) within the
    cooldown → skip, so a rule that dedups on a period cannot fire every poll."""
    exact = session.execute(
        select(Suggestion.id).where(
            Suggestion.user_id == user_id, Suggestion.kind == c.kind, Suggestion.dedup_key == c.dedup_key
        )
    ).first()
    if exact:
        return True
    since = now - timedelta(hours=config.WATCHER_COOLDOWN_HOURS)
    recent = session.execute(
        select(Suggestion.created_at)
        .where(Suggestion.user_id == user_id, Suggestion.kind == c.kind)
        .order_by(Suggestion.created_at.desc())
        .limit(1)
    ).scalar_one_or_none()
    # Event-triggered kinds (dedup on an event id) are allowed to repeat
    # within the cooldown — two big purchases in a day are two facts.
    if recent is not None and c.source_event_id is None and _aware(recent) >= since:
        return True
    return False


def _store(session: Session, user_id: str, c: Candidate, now: datetime) -> Suggestion:
    text, phrased_by = phrasing.phrase(c)
    row = Suggestion(
        user_id=user_id, kind=c.kind, dedup_key=c.dedup_key, facts=c.facts, text=text,
        phrased_by=phrased_by, source_event_id=c.source_event_id, created_at=now,
    )
    session.add(row)
    session.flush()
    audit_service.write(
        session, actor=AuditActor.SYSTEM, action=f"suggestion:{c.kind}", user_id=user_id,
        inputs={"suggestion_id": row.id, "dedup_key": c.dedup_key, "phrased_by": phrased_by, "facts": c.facts},
    )
    logger.info("suggestion user=%s kind=%s by=%s: %s", user_id, c.kind, phrased_by, text)
    return row


def _high_water(session: Session, user_id: str, now: datetime) -> datetime:
    last = session.execute(
        select(func.max(LedgerEvent.created_at))
        .join(Suggestion, Suggestion.source_event_id == LedgerEvent.id)
        .where(Suggestion.user_id == user_id)
    ).scalar_one_or_none()
    if last is not None:
        return _aware(last)
    # Cold start: only react to events from now on. Seeded history is not news.
    return now - timedelta(seconds=config.WATCHER_POLL_SECONDS * 2)


def run_once(session: Session, *, now: datetime | None = None, react_since: datetime | None = None) -> list[Suggestion]:
    """One pass over every active user. Returns the suggestions it created.
    `react_since` overrides the high-water mark (tests, and --backfill)."""
    now = now or datetime.now(timezone.utc)
    created: list[Suggestion] = []
    users = session.execute(select(User).where(User.status == UserStatus.ACTIVE)).scalars().all()
    for user in users:
        since = react_since or _high_water(session, user.id, now)
        events = session.execute(
            select(LedgerEvent)
            .where(LedgerEvent.user_id == user.id)
            .order_by(LedgerEvent.created_at.asc())
        ).scalars().all()
        new_events = [e for e in events if _aware(e.created_at) > since]

        candidates: list[Candidate] = []
        for e in new_events:
            candidates.extend(candidates_for_event(session, user.id, e))
        candidates.extend(candidates_periodic(session, user.id, now))

        for c in candidates:
            if _already_stored(session, user.id, c, now):
                continue
            created.append(_store(session, user.id, c, now))
    session.commit()
    return created


def run_forever(poll_seconds: float | None = None) -> None:
    poll = poll_seconds or config.WATCHER_POLL_SECONDS
    logger.info(
        "watcher starting: db=%s model=%s poll=%ss cooldown=%sh",
        config.DATABASE_URL.split("/")[-1], config.WATCHER_MODEL, poll, config.WATCHER_COOLDOWN_HOURS,
    )
    database.create_all()
    while True:
        started = time.monotonic()
        try:
            with database.session_scope() as session:
                made = run_once(session)
            if made:
                logger.info("pass complete: %d new suggestion(s)", len(made))
        except Exception:  # noqa: BLE001 - a background loop must survive anything and say why
            logger.exception("watcher pass failed; will retry next poll")
        time.sleep(max(0.5, poll - (time.monotonic() - started)))
