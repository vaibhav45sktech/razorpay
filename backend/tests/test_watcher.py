"""Passive watcher (backend/watcher): deterministic rules, phrasing guard,
dedup/cooldown, cold start, audit, API, and its place in state. The model is
never called here — phrasing is monkeypatched or falls back to templates."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from backend.agent import llm_client, prompts
from backend.main import app
from backend.models.db import get_session
from backend.models.entities import AuditActor, AuditEvent, Bucket, LedgerEventType, Suggestion, User
from backend.seed import demo_data
from backend.services import audit_service, ledger_service, state_service
from backend.watcher import phrasing, rules, service
from backend.watcher.rules import Candidate

RUPEE = 100
NOW = datetime(2026, 9, 20, 12, 0, tzinfo=timezone.utc)


@pytest.fixture()
def aarav(db) -> User:
    demo_data.seed_all(db)
    db.commit()
    return db.execute(select(User).where(User.name == demo_data.PRIMARY_DEMO_USER)).scalar_one()


@pytest.fixture(autouse=True)
def _no_model(monkeypatch):
    """The watcher must be fully testable with no Ollama at all."""
    def unavailable(*a, **k):
        raise llm_client.LLMUnavailable("no model in tests")
    monkeypatch.setattr(llm_client, "chat_json", unavailable)


def _append(db, user_id: str, *, paise: int, type_: LedgerEventType, bucket: Bucket, source: str, when: datetime):
    ev = ledger_service.append(
        db, user_id=user_id, type=type_, amount_paise=paise, bucket=bucket, source=source,
    )
    ev.created_at = when
    db.flush()
    return ev


# --------------------------------------------------------------------------
# Rules are pure and boring
# --------------------------------------------------------------------------

def test_spend_pace_fires_only_when_clearly_ahead_of_the_month() -> None:
    state = {"spending_this_month": {"used_paise": 60_000, "limit_paise": 100_000, "remaining_paise": 40_000}}
    early = datetime(2026, 9, 5, tzinfo=timezone.utc)      # 60% by day 5 -> way ahead
    late = datetime(2026, 9, 28, tzinfo=timezone.utc)      # 60% by day 28 -> fine
    assert [c.kind for c in rules.spend_pace(state, early)] == ["spend_pace"]
    assert rules.spend_pace(state, late) == []
    c = rules.spend_pace(state, early)[0]
    assert c.facts["used_rupees"] == 600 and c.facts["limit_rupees"] == 1000
    assert "₹1,000" in c.template and "₹400" in c.template
    assert c.dedup_key == "2026-09"


def test_spend_pace_stays_quiet_under_40_percent() -> None:
    state = {"spending_this_month": {"used_paise": 30_000, "limit_paise": 100_000, "remaining_paise": 70_000}}
    assert rules.spend_pace(state, datetime(2026, 9, 1, tzinfo=timezone.utc)) == []


def test_large_purchase_threshold_is_a_quarter_of_the_limit(db, aarav) -> None:
    state = state_service.get_state(db, aarav.id)
    small = _append(db, aarav.id, paise=-10_000, type_=LedgerEventType.PURCHASE, bucket=Bucket.DISCRETIONARY,
                    source="purchase:food:x", when=NOW)
    big = _append(db, aarav.id, paise=-30_000, type_=LedgerEventType.PURCHASE, bucket=Bucket.DISCRETIONARY,
                  source="purchase:books:y", when=NOW)
    assert rules.large_purchase(state, small) == []
    got = rules.large_purchase(state, big)
    assert len(got) == 1 and got[0].source_event_id == big.id and got[0].facts["purchase_rupees"] == 300


def test_goal_milestone_detects_the_crossing_not_the_level(db, aarav) -> None:
    # Seeded: ₹1,500 of ₹5,000 (30%). A ₹1,000 contribution -> ₹2,500 (50%) crosses 50 only.
    ev = _append(db, aarav.id, paise=100_000, type_=LedgerEventType.CONTRIBUTION, bucket=Bucket.EMERGENCY_SAVINGS,
                 source="test:contrib", when=NOW)
    state = state_service.get_state(db, aarav.id)
    got = rules.goal_milestone(state, ev)
    assert [c.facts["milestone_pct"] for c in got] == [50]
    assert got[0].dedup_key.endswith(":50")


def test_savings_nudge_needs_two_quiet_weeks(db, aarav) -> None:
    now = datetime.now(timezone.utc)
    state = state_service.get_state(db, aarav.id)
    # The seed backdates its contributions by whole months -> quiet for 14+ days -> nudge, keyed by ISO week.
    got = rules.savings_nudge(db, state, aarav.id, now)
    assert len(got) == 1 and got[0].kind == "savings_nudge" and got[0].dedup_key == now.strftime("%G-W%V")
    # A contribution today silences it.
    _append(db, aarav.id, paise=10_000, type_=LedgerEventType.CONTRIBUTION, bucket=Bucket.EMERGENCY_SAVINGS,
            source="test:today", when=now)
    assert rules.savings_nudge(db, state_service.get_state(db, aarav.id), aarav.id, now) == []


def test_pending_approval_reminder_only_after_ten_minutes(db, aarav) -> None:
    from backend.services import money_action_service as mas
    r = mas.create(db, user_id=aarav.id, action="PURCHASE", amount_paise=600 * RUPEE,
                   purpose="purchase:big", actor=AuditActor.USER)
    assert r.as_dict()["status"] == "AWAITING_APPROVAL"
    assert rules.pending_approval(db, aarav.id, datetime.now(timezone.utc)) == []
    got = rules.pending_approval(db, aarav.id, datetime.now(timezone.utc) + timedelta(minutes=11))
    assert len(got) == 1 and got[0].facts["amount_rupees"] == 600 and "approval" in got[0].template


# --------------------------------------------------------------------------
# Phrasing: model output is checked, never trusted
# --------------------------------------------------------------------------

def _cand() -> Candidate:
    return Candidate(kind="k", dedup_key="d", facts={"a": 1},
                     template="You've used 60% of your ₹1,000 budget — ₹400 left.")


def test_phrasing_falls_back_to_template_when_model_unavailable() -> None:
    text, by = phrasing.phrase(_cand())
    assert text == _cand().template and by == "template"


def test_phrasing_rejects_model_text_that_changes_a_number(monkeypatch) -> None:
    monkeypatch.setattr(llm_client, "chat_json", lambda *a, **k: {"text": "You've used 60% of your ₹1,000 budget — ₹4,000 left!"})
    text, by = phrasing.phrase(_cand())
    assert by == "template" and text == _cand().template


def test_phrasing_accepts_model_text_that_keeps_every_number(monkeypatch) -> None:
    monkeypatch.setattr(llm_client, "chat_json", lambda *a, **k: {"text": "Quick heads-up: 60% of your ₹1,000 budget is used, so ₹400 remains."})
    text, by = phrasing.phrase(_cand())
    assert by.startswith("llm:") and "₹400" in text


# --------------------------------------------------------------------------
# Service: cold start, dedup, cooldown, audit, restart-safety
# --------------------------------------------------------------------------

def test_cold_start_does_not_narrate_seeded_history(db, aarav) -> None:
    made = service.run_once(db, now=datetime.now(timezone.utc))
    kinds = {s.kind for s in made}
    # Seed events are older than the poll window -> no event-triggered kinds.
    assert not ({"large_purchase", "goal_milestone", "offer_match"} & kinds)


def test_new_purchase_produces_one_suggestion_once(db, aarav) -> None:
    now = datetime.now(timezone.utc)
    _append(db, aarav.id, paise=-30_000, type_=LedgerEventType.PURCHASE, bucket=Bucket.DISCRETIONARY,
            source="purchase:books:x", when=now)
    first = service.run_once(db, now=now, react_since=now - timedelta(minutes=1))
    assert [s.kind for s in first if s.kind == "large_purchase"] == ["large_purchase"]
    again = service.run_once(db, now=now + timedelta(seconds=15), react_since=now - timedelta(minutes=1))
    assert [s for s in again if s.kind == "large_purchase"] == [], "same event must not be narrated twice"


def test_periodic_kind_respects_cooldown(db, aarav) -> None:
    later = datetime.now(timezone.utc) + timedelta(days=30)   # triggers savings_nudge
    first = service.run_once(db, now=later)
    assert any(s.kind == "savings_nudge" for s in first)
    soon = service.run_once(db, now=later + timedelta(hours=1))
    assert not any(s.kind == "savings_nudge" for s in soon)
    next_week = service.run_once(db, now=later + timedelta(days=8))
    assert any(s.kind == "savings_nudge" for s in next_week), "a new ISO week past the cooldown may nudge again"


def test_every_suggestion_is_audited_and_chain_holds(db, aarav) -> None:
    later = datetime.now(timezone.utc) + timedelta(days=30)
    made = service.run_once(db, now=later)
    assert made
    rows = db.execute(select(AuditEvent).where(AuditEvent.action.like("suggestion:%"))).scalars().all()
    assert {r.action for r in rows} == {f"suggestion:{s.kind}" for s in made}
    assert all(r.actor is AuditActor.SYSTEM for r in rows)
    assert audit_service.verify_chain(db).ok


def test_high_water_mark_is_derived_from_the_database(db, aarav) -> None:
    now = datetime.now(timezone.utc)
    ev = _append(db, aarav.id, paise=-30_000, type_=LedgerEventType.PURCHASE, bucket=Bucket.DISCRETIONARY,
                 source="purchase:books:x", when=now - timedelta(seconds=5))
    service.run_once(db, now=now)                     # cold start window covers it
    hw = service._high_water(db, aarav.id, now + timedelta(hours=1))
    assert hw == ev.created_at.replace(tzinfo=timezone.utc) or hw == ev.created_at


# --------------------------------------------------------------------------
# API + state + prompt
# --------------------------------------------------------------------------

@pytest.fixture()
def client(db):
    def _override():
        yield db
    app.dependency_overrides[get_session] = _override
    try:
        with TestClient(app) as c:
            yield c
    finally:
        app.dependency_overrides.clear()


def test_api_lists_and_dismisses_suggestions_read_only(client, db, aarav) -> None:
    later = datetime.now(timezone.utc) + timedelta(days=30)
    made = service.run_once(db, now=later)
    assert made
    resp = client.get(f"/api/suggestions/{aarav.id}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["suggestions"] and all("advisory_notice" in s for s in body["suggestions"])
    sid = body["suggestions"][0]["suggestion_id"]

    wrong_user = client.post(f"/api/suggestions/{sid}/dismiss", json={"user_id": "usr_nope"})
    assert wrong_user.status_code == 404
    ok = client.post(f"/api/suggestions/{sid}/dismiss", json={"user_id": aarav.id})
    assert ok.status_code == 200 and ok.json()["dismissed"] is True
    assert sid not in [s["suggestion_id"] for s in client.get(f"/api/suggestions/{aarav.id}").json()["suggestions"]]
    assert client.get("/api/suggestions/usr_nope").status_code == 404


def test_state_carries_top_suggestions_and_prompt_renders_them(db, aarav) -> None:
    later = datetime.now(timezone.utc) + timedelta(days=30)
    service.run_once(db, now=later)
    state = state_service.get_state(db, aarav.id)
    assert state["suggestions"] and state["suggestions"][0]["advisory"] is True
    summary = prompts.render_state_summary(state)
    assert "Recent app suggestion (advisory only" in summary


def test_no_tool_or_route_can_act_on_a_suggestion() -> None:
    """Structural: no LLM-visible tool takes a suggestion id, and the only
    suggestion routes are GET (list) and POST .../dismiss."""
    from backend.agent import tool_registry
    for t in tool_registry.llm_visible_tools():
        assert "suggestion" not in str(t.args_json_schema()).lower()
    paths = app.openapi()["paths"]
    routes = {(path, tuple(sorted(m.upper() for m in ops))) for path, ops in paths.items() if "suggestion" in path}
    assert routes == {
        ("/api/suggestions/{user_id}", ("GET",)),
        ("/api/suggestions/{suggestion_id}/dismiss", ("POST",)),
    }
