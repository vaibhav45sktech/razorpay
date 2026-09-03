"""Phase 3 — the curl-able flow, driven through the HTTP layer.

A passing integration test at the service layer can coexist with a broken HTTP
layer, so this drives the same journey through FastAPI's TestClient with the
app's real routers, against an isolated in-memory database.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from backend import config
from backend.main import app
from backend.models.db import get_session
from backend.seed import demo_data

RUPEE = 100


@pytest.fixture()
def client(db):
    """TestClient whose requests share the test's isolated session."""
    def _override():
        yield db
    app.dependency_overrides[get_session] = _override
    try:
        with TestClient(app) as c:
            yield c
    finally:
        app.dependency_overrides.clear()


@pytest.fixture()
def seeded(client, db) -> str:
    demo_data.seed_all(db)
    db.commit()
    from backend.models.entities import User
    return db.query(User).filter_by(name=demo_data.PRIMARY_DEMO_USER).one().id


# ---------------------------------------------------------------------------
# Debug gating
# ---------------------------------------------------------------------------


def test_debug_routes_are_404_when_debug_is_off(client, monkeypatch) -> None:
    monkeypatch.setattr(config, "DEBUG", False)
    assert client.post("/debug/seed").status_code == 404
    assert client.post("/debug/intents", json={"user_id": "u", "action": "PURCHASE", "amount_paise": 1, "purpose": "x"}).status_code == 404
    assert client.post("/debug/intents/x/fake-settle").status_code == 404


def test_debug_routes_work_when_debug_is_on(client, monkeypatch) -> None:
    monkeypatch.setattr(config, "DEBUG", True)
    assert client.post("/debug/seed").status_code == 200


# ---------------------------------------------------------------------------
# The flagship journey: "Save Rs.500 this month" (HLD s1.5), fake provider
# ---------------------------------------------------------------------------


def test_contribution_end_to_end(client, seeded, monkeypatch) -> None:
    monkeypatch.setattr(config, "DEBUG", True)
    user_id = seeded

    before = client.get(f"/api/state/{user_id}").json()
    assert before["balances_paise"]["emergency_savings"] == 1500 * RUPEE
    assert before["goals"][0]["pct_complete"] == 30.0   # 1,500 of 5,000

    created = client.post("/debug/intents", json={
        "user_id": user_id, "action": "CONTRIBUTION", "amount_paise": 500 * RUPEE, "purpose": "savings_goal:demo",
    }).json()
    assert created["status"] == "ALLOWED"
    assert created["duplicate"] is False

    # state shows it pending, not yet in the balance
    mid = client.get(f"/api/state/{user_id}").json()
    assert mid["balances_paise"]["emergency_savings"] == 1500 * RUPEE
    assert any(p["intent_id"] == created["intent_id"] for p in mid["pending_actions"])

    settled = client.post(f"/debug/intents/{created['intent_id']}/fake-settle").json()
    assert settled["status"] == "LEDGER_UPDATED"

    after = client.get(f"/api/state/{user_id}").json()
    assert after["balances_paise"]["emergency_savings"] == 2000 * RUPEE
    assert after["goals"][0]["pct_complete"] == 40.0
    assert after["pending_actions"] == []
    assert after["recent_events"][0]["amount_paise"] == 500 * RUPEE


def test_duplicate_contribution_is_blocked_over_http(client, seeded, monkeypatch) -> None:
    monkeypatch.setattr(config, "DEBUG", True)
    body = {"user_id": seeded, "action": "CONTRIBUTION", "amount_paise": 500 * RUPEE, "purpose": "savings_goal:demo"}
    first = client.post("/debug/intents", json=body).json()
    second = client.post("/debug/intents", json=body).json()
    assert second["duplicate"] is True
    assert second["intent_id"] == first["intent_id"]


# ---------------------------------------------------------------------------
# Approval over HTTP - a structured user action
# ---------------------------------------------------------------------------


def test_purchase_needing_approval_over_http(client, seeded, monkeypatch) -> None:
    monkeypatch.setattr(config, "DEBUG", True)
    user_id = seeded

    created = client.post("/debug/intents", json={
        "user_id": user_id, "action": "PURCHASE", "amount_paise": 600 * RUPEE, "purpose": "purchase:headphones",
    }).json()
    assert created["status"] == "AWAITING_APPROVAL"

    # Cannot execute while awaiting
    assert client.post(f"/debug/intents/{created['intent_id']}/fake-settle").status_code == 409

    # Wrong user cannot approve
    assert client.post(f"/api/intents/{created['intent_id']}/approve", json={"user_id": "usr_intruder"}).status_code == 403

    # Owner approves, then it settles
    ok = client.post(f"/api/intents/{created['intent_id']}/approve", json={"user_id": user_id}).json()
    assert ok["status"] == "APPROVED"
    settled = client.post(f"/debug/intents/{created['intent_id']}/fake-settle").json()
    assert settled["status"] == "LEDGER_UPDATED"

    state = client.get(f"/api/state/{user_id}").json()
    assert state["spending_this_month"]["used_paise"] == (240 + 600) * RUPEE
    assert state["balances_paise"]["emergency_savings"] == 1500 * RUPEE   # untouched


def test_denied_purchase_over_http_is_closed_with_reason(client, seeded, monkeypatch) -> None:
    monkeypatch.setattr(config, "DEBUG", True)
    created = client.post("/debug/intents", json={
        "user_id": seeded, "action": "PURCHASE", "amount_paise": 5000 * RUPEE, "purpose": "purchase:laptop",
    }).json()
    assert created["status"] == "CLOSED"
    assert created["policy"]["decision"] == "DENY"
    assert "monthly limit" in created["policy"]["reason"]


def test_failed_payment_leaves_ledger_untouched_over_http(client, seeded, monkeypatch) -> None:
    monkeypatch.setattr(config, "DEBUG", True)
    created = client.post("/debug/intents", json={
        "user_id": seeded, "action": "CONTRIBUTION", "amount_paise": 300 * RUPEE, "purpose": "savings_goal:fail",
    }).json()
    failed = client.post(f"/debug/intents/{created['intent_id']}/fake-fail").json()
    assert failed["status"] == "CLOSED"
    assert client.get(f"/api/state/{seeded}").json()["balances_paise"]["emergency_savings"] == 1500 * RUPEE


def test_reversal_over_http(client, seeded, monkeypatch) -> None:
    monkeypatch.setattr(config, "DEBUG", True)
    created = client.post("/debug/intents", json={
        "user_id": seeded, "action": "CONTRIBUTION", "amount_paise": 500 * RUPEE, "purpose": "savings_goal:rev",
    }).json()
    client.post(f"/debug/intents/{created['intent_id']}/fake-settle")
    assert client.get(f"/api/state/{seeded}").json()["balances_paise"]["emergency_savings"] == 2000 * RUPEE

    r = client.post(f"/debug/intents/{created['intent_id']}/reverse", params={"reason": "test dispute"}).json()
    assert r["reversed"] is True
    assert client.get(f"/api/state/{seeded}").json()["balances_paise"]["emergency_savings"] == 1500 * RUPEE
    assert client.get(f"/api/intents/{created['intent_id']}").json()["reversed"] is True


def test_unknown_user_state_is_404(client) -> None:
    assert client.get("/api/state/usr_nobody").status_code == 404


def test_state_never_exposes_a_discretionary_balance(client, seeded) -> None:
    """Decision D2.1, checked at the API boundary."""
    state = client.get(f"/api/state/{seeded}").json()
    assert "discretionary" not in state["balances_paise"]
    assert state["spending_this_month"]["used_paise"] == 240 * RUPEE
    assert state["spending_this_month"]["limit_paise"] == 1000 * RUPEE
