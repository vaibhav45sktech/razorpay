"""Phase 6 — the Autopilot (agent-led flow) over HTTP.

What these tests pin down:
  * the monthly plan is derived from the ledger + goal and moves due ->
    pending -> done as the user acts, never by being told;
  * "Agree" creates exactly one CONTRIBUTION intent and reuses it on a
    second tap (no double-charging from a double-click);
  * the draw-round recommendation is deterministic: last round with no
    needs, the round just before the first projected shortfall with needs,
    and every reason it gives is a plain sentence;
  * requesting a round replaces an earlier request, refuses past rounds,
    and refuses a second draw after one was paid;
  * the simulated payout runs through the REAL policy gate (an allocation
    must authorise it) and is refused outright with DEBUG off;
  * offers are matched to needs by category and carry a policy preview;
    proposing one creates a PURCHASE intent that the policy engine judges.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from backend import config
from backend.main import app
from backend.models.db import get_session
from backend.models.entities import AllocationStatus, PoolAllocation, User
from backend.seed import demo_data
from backend.services import autopilot_service as ap

RUPEE = 100


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


@pytest.fixture()
def seeded(client, db) -> str:
    demo_data.seed_all(db)
    db.commit()
    return db.query(User).filter_by(name=demo_data.PRIMARY_DEMO_USER).one().id


@pytest.fixture()
def new_user(client, db) -> str:
    demo_data.seed_all(db)
    db.commit()
    return db.query(User).filter(User.name.like("Diya%")).one().id


# ---------------------------------------------------------------------------
# Plan
# ---------------------------------------------------------------------------

def test_plan_is_due_with_a_reasoned_recommendation(client, seeded) -> None:
    plan = client.get(f"/api/plan/{seeded}").json()
    assert plan["status"] == "due"
    assert plan["recommended_paise"] == 500 * RUPEE            # capped at the band max
    assert plan["goal"]["saved_paise"] == 1_500 * RUPEE         # from the ledger, not a stored counter
    assert plan["goal"]["remaining_paise"] == 3_500 * RUPEE
    assert plan["goal"]["months_to_goal"] == 7
    assert plan["policy_preview"]["decision"] == "ALLOW"
    assert any("Capped" in r for r in plan["reasons"])
    assert "Synthetic" in plan["demo_notice"]


def test_plan_for_a_new_user_still_recommends_within_band(client, new_user) -> None:
    plan = client.get(f"/api/plan/{new_user}").json()
    assert plan["status"] == "due"
    assert plan["band"]["min_paise"] <= plan["recommended_paise"] <= plan["band"]["max_paise"]
    assert plan["goal"]["saved_paise"] == 0


def test_plan_unknown_user_is_404(client, seeded) -> None:
    assert client.get("/api/plan/usr_nobody").status_code == 404


def test_agree_creates_one_intent_and_a_second_tap_reuses_it(client, seeded) -> None:
    first = client.post(f"/api/plan/{seeded}/agree").json()
    assert first["reused"] is False and first["status"] == "ALLOWED"
    assert first["amount_paise"] == 500 * RUPEE

    second = client.post(f"/api/plan/{seeded}/agree").json()
    assert second["reused"] is True and second["intent_id"] == first["intent_id"]

    plan = client.get(f"/api/plan/{seeded}").json()
    assert plan["status"] == "pending"
    assert plan["pending_intent"]["intent_id"] == first["intent_id"]

    state = client.get(f"/api/state/{seeded}").json()
    assert [p["type"] for p in state["pending_actions"]] == ["CONTRIBUTION"]


def test_plan_becomes_done_after_the_contribution_settles(client, seeded, monkeypatch) -> None:
    monkeypatch.setattr(config, "DEBUG", True)
    intent_id = client.post(f"/api/plan/{seeded}/agree").json()["intent_id"]
    assert client.post(f"/debug/intents/{intent_id}/fake-settle").status_code == 200

    plan = client.get(f"/api/plan/{seeded}").json()
    assert plan["status"] == "done"
    assert plan["contributed_this_month_paise"] == 500 * RUPEE
    # Agreeing again when the month is covered is a 409, not a second charge.
    assert client.post(f"/api/plan/{seeded}/agree").status_code == 409


# ---------------------------------------------------------------------------
# Needs
# ---------------------------------------------------------------------------

def test_needs_crud_and_validation(client, seeded) -> None:
    r = client.post(f"/api/needs/{seeded}", json={"label": "Exam fees", "month": "2026-11", "amount_rupees": 3000, "category": "education"})
    assert r.status_code == 201, r.text
    need_id = r.json()["need_id"]
    assert r.json()["amount_paise"] == 3_000 * RUPEE

    assert client.post(f"/api/needs/{seeded}", json={"label": "x", "month": "2026-11", "amount_rupees": 10}).status_code == 422
    assert client.post(f"/api/needs/{seeded}", json={"label": "Trip", "month": "Nov 2026", "amount_rupees": 10}).status_code == 422
    assert client.post(f"/api/needs/{seeded}", json={"label": "Trip", "month": "2026-11", "amount_rupees": 0}).status_code == 422
    assert client.post(f"/api/needs/{seeded}", json={"label": "Trip", "month": "2026-11", "amount_rupees": 10, "category": "yachts"}).status_code == 422

    listed = client.get(f"/api/needs/{seeded}").json()
    assert [n["need_id"] for n in listed["needs"]] == [need_id]
    assert "education" in listed["categories"]

    assert client.delete(f"/api/needs/{seeded}/{need_id}").status_code == 200
    assert client.get(f"/api/needs/{seeded}").json()["needs"] == []
    assert client.delete(f"/api/needs/{seeded}/{need_id}").status_code == 404


def test_a_need_cannot_be_deleted_by_another_user(client, seeded, new_user) -> None:
    need_id = client.post(f"/api/needs/{seeded}", json={"label": "Exam fees", "month": "2026-11", "amount_rupees": 300}).json()["need_id"]
    assert client.delete(f"/api/needs/{new_user}/{need_id}").status_code == 404
    assert len(client.get(f"/api/needs/{seeded}").json()["needs"]) == 1


# ---------------------------------------------------------------------------
# Pool timeline + recommendation
# ---------------------------------------------------------------------------

def test_timeline_has_three_drawn_rounds_and_this_month_is_round_four(client, seeded) -> None:
    pool = client.get(f"/api/pool/{seeded}").json()
    assert pool["in_pool"] is True
    assert pool["round_amount_paise"] == 5_000 * RUPEE
    rounds = pool["rounds"]
    assert len(rounds) == 10
    assert [r["status"] for r in rounds[:3]] == ["drawn"] * 3
    assert all(r["drawer"] for r in rounds[:3])
    assert rounds[3]["current"] is True and rounds[3]["status"] == "open"
    assert pool["can_simulate_draw"] == bool(config.DEBUG)


def test_without_needs_the_agent_recommends_the_last_round(client, seeded) -> None:
    pool = client.get(f"/api/pool/{seeded}").json()
    rec = pool["recommendation"]
    assert rec["month"] == pool["rounds"][-1]["month"]
    assert any("haven't listed any upcoming needs" in r for r in rec["reasons"])
    assert [r for r in pool["rounds"] if r.get("recommended")][0]["month"] == rec["month"]


def test_with_a_shortfall_the_agent_recommends_the_round_before_it(client, seeded) -> None:
    # Saved ₹1,500 now, ₹500/month projected: by the 2nd open month she has ₹2,500;
    # a ₹3,000 need that month is a ₹500 gap -> recommend that month's round.
    pool = client.get(f"/api/pool/{seeded}").json()
    target = pool["rounds"][5]["month"]                 # two months after the current round
    client.post(f"/api/needs/{seeded}", json={"label": "Exam fees", "month": target, "amount_rupees": 3000, "category": "education"})

    pool = client.get(f"/api/pool/{seeded}").json()
    rec = pool["recommendation"]
    assert rec["month"] == target
    assert any("gap of ₹500" in r for r in rec["reasons"]), rec["reasons"]
    assert any("Exam fees" in r for r in rec["reasons"])


def test_needs_covered_by_savings_do_not_pull_the_draw_forward(client, seeded) -> None:
    pool = client.get(f"/api/pool/{seeded}").json()
    target = pool["rounds"][5]["month"]
    client.post(f"/api/needs/{seeded}", json={"label": "Books", "month": target, "amount_rupees": 800})
    pool = client.get(f"/api/pool/{seeded}").json()
    assert pool["recommendation"]["month"] == pool["rounds"][-1]["month"]
    assert any("cover every need" in r for r in pool["recommendation"]["reasons"])


def test_recommendation_is_deterministic(client, seeded) -> None:
    a = client.get(f"/api/pool/{seeded}").json()["recommendation"]
    b = client.get(f"/api/pool/{seeded}").json()["recommendation"]
    assert a == b


def test_user_outside_a_pool_gets_a_plain_answer(client, db, seeded) -> None:
    loner = User(name="Loner (test)", is_synthetic=True)
    db.add(loner); db.commit()
    pool = client.get(f"/api/pool/{loner.id}").json()
    assert pool["in_pool"] is False
    assert client.post(f"/api/pool/{loner.id}/request-round", json={"month": "2030-01"}).status_code == 409


# ---------------------------------------------------------------------------
# Requesting a round
# ---------------------------------------------------------------------------

def test_request_round_records_an_explained_allocation(client, db, seeded) -> None:
    pool = client.get(f"/api/pool/{seeded}").json()
    month = pool["recommendation"]["month"]
    r = client.post(f"/api/pool/{seeded}/request-round", json={"month": month})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "confirmed" and body["followed_recommendation"] is True
    assert f"round:{month}" in body["reason"] and "DEMO" in body["reason"]

    alloc = db.get(PoolAllocation, body["allocation_id"])
    assert alloc.status is AllocationStatus.CONFIRMED and alloc.amount_paise == 5_000 * RUPEE

    pool = client.get(f"/api/pool/{seeded}").json()
    mine = [x for x in pool["rounds"] if x.get("requested_by_you")]
    assert len(mine) == 1 and mine[0]["month"] == month and mine[0]["drawer"] == "You"


def test_second_request_replaces_the_first(client, db, seeded) -> None:
    rounds = client.get(f"/api/pool/{seeded}").json()["rounds"]
    first = client.post(f"/api/pool/{seeded}/request-round", json={"month": rounds[4]["month"]}).json()
    second = client.post(f"/api/pool/{seeded}/request-round", json={"month": rounds[6]["month"]}).json()
    assert second["followed_recommendation"] is False
    assert "against the agent's recommendation" in second["reason"]
    assert db.get(PoolAllocation, first["allocation_id"]).status is AllocationStatus.CANCELLED
    assert db.get(PoolAllocation, second["allocation_id"]).status is AllocationStatus.CONFIRMED
    pool = client.get(f"/api/pool/{seeded}").json()
    assert pool["my_draw"]["allocation_id"] == second["allocation_id"]


def test_past_and_malformed_rounds_are_refused(client, seeded) -> None:
    rounds = client.get(f"/api/pool/{seeded}").json()["rounds"]
    assert client.post(f"/api/pool/{seeded}/request-round", json={"month": rounds[0]["month"]}).status_code == 409
    assert client.post(f"/api/pool/{seeded}/request-round", json={"month": "2031-13"}).status_code == 422
    assert client.post(f"/api/pool/{seeded}/request-round", json={"month": "2031-01"}).status_code == 409  # not a round


# ---------------------------------------------------------------------------
# Simulated draw
# ---------------------------------------------------------------------------

def test_simulated_draw_is_refused_when_debug_is_off(client, seeded, monkeypatch) -> None:
    monkeypatch.setattr(config, "DEBUG", False)
    rounds = client.get(f"/api/pool/{seeded}").json()["rounds"]
    client.post(f"/api/pool/{seeded}/request-round", json={"month": rounds[4]["month"]})
    assert client.post(f"/api/pool/{seeded}/simulate-draw").status_code == 403
    assert client.get(f"/api/pool/{seeded}").json()["can_simulate_draw"] is False


def test_simulated_draw_needs_a_requested_round(client, seeded, monkeypatch) -> None:
    monkeypatch.setattr(config, "DEBUG", True)
    assert client.post(f"/api/pool/{seeded}/simulate-draw").status_code == 409
    assert client.get(f"/api/state/{seeded}").json()["balances_paise"].get("rewards", 0) == 0


def test_simulated_draw_goes_through_the_policy_gate_and_settles_once(client, db, seeded, monkeypatch) -> None:
    monkeypatch.setattr(config, "DEBUG", True)
    rounds = client.get(f"/api/pool/{seeded}").json()["rounds"]
    month = rounds[4]["month"]
    client.post(f"/api/pool/{seeded}/request-round", json={"month": month})

    r = client.post(f"/api/pool/{seeded}/simulate-draw")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["executed"] is True and body["status"] == "LEDGER_UPDATED"
    assert "SIMULATED" in body["note"]

    state = client.get(f"/api/state/{seeded}").json()
    assert state["balances_paise"]["rewards"] == 5_000 * RUPEE
    assert state["balances_paise"]["emergency_savings"] == 1_500 * RUPEE   # untouched

    pool = client.get(f"/api/pool/{seeded}").json()
    assert pool["my_draw"]["status"] == "paid"
    # A second draw: the allocation is PAID, so the policy engine denies it
    # (payout_already_paid) — or the duplicate guard stops it earlier. Either
    # way nothing is executed and the balance does not move.
    again = client.post(f"/api/pool/{seeded}/simulate-draw").json()
    assert again["executed"] is False
    assert client.get(f"/api/state/{seeded}").json()["balances_paise"]["rewards"] == 5_000 * RUPEE
    # And she cannot pick another round this cycle.
    assert client.post(f"/api/pool/{seeded}/request-round", json={"month": rounds[6]["month"]}).status_code == 409


def test_simulated_draw_is_denied_without_an_authorising_allocation(db, seeded, monkeypatch) -> None:
    """Belt and braces at the service layer: even if a caller fakes `my_draw`
    by cancelling the allocation underneath, the policy engine says no."""
    monkeypatch.setattr(config, "DEBUG", True)
    ap.request_round(db, seeded, month=ap.pool_view(db, seeded)["rounds"][4]["month"])
    for a in db.query(PoolAllocation).filter_by(user_id=seeded, amount_paise=5_000 * RUPEE).all():
        a.status = AllocationStatus.CANCELLED
    db.flush()
    # my_draw still resolves (newest allocation at the round amount, now cancelled)…
    assert ap.pool_view(db, seeded)["my_draw"]["status"] == "cancelled"
    out = ap.simulate_draw(db, seeded)
    assert out["executed"] is False and out["policy"]["decision"] == "DENY"
    assert out["policy"]["rule"] == "no_pool_authorization"


# ---------------------------------------------------------------------------
# Spend
# ---------------------------------------------------------------------------

def test_spend_view_matches_offers_to_needs_and_previews_policy(client, seeded) -> None:
    client.post(f"/api/needs/{seeded}", json={"label": "Textbooks", "month": "2027-01", "amount_rupees": 1200, "category": "education"})
    sp = client.get(f"/api/spend/{seeded}").json()
    assert sp["spent_this_month_paise"] == 240 * RUPEE
    assert sp["monthly_limit_paise"] == 1_000 * RUPEE
    assert sp["headroom_paise"] == 760 * RUPEE
    titles = [o["title"] for o in sp["offers"]]
    assert "Rs.500 off - EXPIRED, for testing" not in titles
    matched = [o for o in sp["offers"] if o["matched_needs"]]
    assert matched and matched[0]["category"] == "education"
    assert "Textbooks" in matched[0]["match_note"]
    assert sp["offers"][0]["matched_needs"]                      # matched offers sort first
    for o in sp["offers"]:
        if o["effective_price_paise"] is not None:
            assert o["policy_preview"]["decision"] in {"ALLOW", "REQUIRE_APPROVAL", "DENY"}


def test_propose_purchase_is_judged_by_the_policy_engine(client, seeded) -> None:
    sp = client.get(f"/api/spend/{seeded}").json()
    priced = [o for o in sp["offers"] if o["effective_price_paise"] is not None]
    over_limit = next(o for o in priced if o["effective_price_paise"] > sp["headroom_paise"])
    r = client.post(f"/api/spend/{seeded}/propose", json={"offer_id": over_limit["offer_id"]})
    assert r.status_code == 200
    assert r.json()["policy"]["decision"] == "DENY"
    assert r.json()["status"] == "CLOSED"
    # A denied proposal never becomes a pending action.
    assert client.get(f"/api/state/{seeded}").json()["pending_actions"] == []


def test_propose_purchase_unknown_offer_is_404(client, seeded) -> None:
    assert client.post(f"/api/spend/{seeded}/propose", json={"offer_id": "ofr_nope"}).status_code == 404


# ---------------------------------------------------------------------------
# The chat agent can explain the Autopilot (read-only tool)
# ---------------------------------------------------------------------------

def test_get_autopilot_plan_tool_reads_the_same_decision_the_screen_shows(db, seeded, monkeypatch) -> None:
    """The drawer agent must explain the plan the user is looking at — not a
    different one. The tool is read-only, takes no arguments (so the
    orchestrator skips the argument-fill call), and shows rupees to the model."""
    import json

    from backend.agent import llm_client, orchestrator, tool_registry
    from backend.agent.llm_client import ToolDecision

    ap.add_need(db, seeded, label="Exam fees", month=ap.pool_view(db, seeded)["rounds"][5]["month"],
                amount_paise=3_000 * RUPEE, category="education")
    expected = ap.pool_view(db, seeded)["recommendation"]

    tool = tool_registry.get("get_autopilot_plan")
    assert tool.caller is tool_registry.Caller.LLM
    assert tool.args_json_schema().get("properties", {}) == {}

    seen: dict = {}
    decisions = iter([ToolDecision(action="call_tool", tool_name="get_autopilot_plan", final_text=None),
                      ToolDecision(action="final_answer", tool_name=None, final_text="Explained.")])

    def fake_decide(messages, tool_names):
        for m in messages:
            if m.get("role") == "tool" and m.get("name") == "get_autopilot_plan":
                seen["payload"] = json.loads(m["content"])
        return next(decisions)

    def no_fill(messages, schema):  # pragma: no cover - must not be called for a no-arg tool
        raise AssertionError("fill_arguments must not run for a no-argument tool")

    monkeypatch.setattr(llm_client, "decide", fake_decide)
    monkeypatch.setattr(llm_client, "fill_arguments", no_fill)
    reply = orchestrator.run_agent_turn(db, seeded, "Why this month for my draw?")
    assert reply.text == "Explained."

    result = seen["payload"]["result"]
    assert result["pool_draw"]["recommended_round"]["month"] == expected["label"]
    assert result["pool_draw"]["recommended_round"]["reasons"] == expected["reasons"]
    assert result["this_month"]["status"] == "due"
    # Rupees at the model boundary, never paise (Phase 4 lesson).
    flat = json.dumps(result)
    assert "paise" not in flat and "proposed_contribution_rupees" in flat
    assert [n["label"] for n in result["upcoming_needs"]] == ["Exam fees"]
