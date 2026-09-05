"""Phase 8 item 8 — the degradation matrix, TESTED rather than only written.

`docs/degradation_matrix.md` claims what the app still does when each
dependency is gone. A matrix nobody executes is a wish: the value of writing
one is finding the row where the code disagrees with the document. Every row
in that table has a test here, and the document is wrong if one of these fails.

The Definition of Done for this phase also asks for the tamper demo — forge an
audit row and watch the system name it — so that is pinned here too.
"""

from __future__ import annotations

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text

from backend import config
from backend.agent import llm_client, orchestrator
from backend.main import app
from backend.models.db import get_session
from backend.models.entities import ActionIntent, AuditEvent, LedgerEvent, User
from backend.seed import demo_data
from backend.services import audit_service

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


def _kill_llm(monkeypatch) -> None:
    def down(*_a, **_k):
        raise llm_client.LLMUnavailable("simulated outage")
    monkeypatch.setattr(llm_client, "decide", down)
    monkeypatch.setattr(llm_client, "fill_arguments", down)


# ---------------------------------------------------------------------------
# Row 1 — Ollama down
# ---------------------------------------------------------------------------


def test_no_ollama_still_returns_real_numbers_and_writes_nothing(client, seeded, db, monkeypatch) -> None:
    """The matrix's central claim: the model is a convenience, the ledger is
    the truth. Losing the model must cost conversation and nothing else."""
    _kill_llm(monkeypatch)
    intents_before = db.query(ActionIntent).count()
    ledger_before = db.query(LedgerEvent).count()

    r = client.post("/api/chat", json={"user_id": seeded, "message": "What's my balance?", "history": []})
    assert r.status_code == 200, "an outage is a degraded reply, never a 500"
    body = r.json()
    assert body["degraded"] is True
    assert "unavailable" in body["reply"].lower()
    # The verified numbers are still there, and they are the real ones.
    assert body["state"]["balances_paise"]["emergency_savings"] == 1_500 * RUPEE
    # Nothing half-done was written.
    assert db.query(ActionIntent).count() == intents_before
    assert db.query(LedgerEvent).count() == ledger_before
    assert audit_service.verify_chain(db).ok


def test_no_ollama_leaves_every_read_path_working(client, seeded, monkeypatch) -> None:
    _kill_llm(monkeypatch)
    for path in (f"/api/state/{seeded}", f"/api/plan/{seeded}", f"/api/pool/{seeded}",
                 f"/api/spend/{seeded}", f"/api/card/{seeded}", "/api/audit", "/api/exceptions"):
        assert client.get(path).status_code == 200, path


def test_no_ollama_leaves_the_autopilot_and_the_card_deciding(client, seeded, db, monkeypatch) -> None:
    """The Autopilot's plan and the card's rule engine are deterministic code,
    so they must be completely unaffected by the model being gone."""
    _kill_llm(monkeypatch)
    plan = client.get(f"/api/plan/{seeded}").json()
    assert plan["recommended_paise"] > 0 and plan["reasons"], "the plan is arithmetic, not inference"

    from backend.models.entities import PurchaseRule, RuleStatus, WatchedProduct
    from backend.services import agent_card_service as card
    meal = db.query(WatchedProduct).filter(WatchedProduct.name.like("%meal-card%")).one()
    card.set_price(db, product_id=meal.id, platform="shopkart", price_paise=440 * RUPEE)
    r = card.create_rule(db, seeded, product_id=meal.id, target_price_paise=450 * RUPEE)
    assert db.get(PurchaseRule, r["rule_id"]).status is RuleStatus.AWAITING_APPROVAL


def test_the_outage_itself_is_recorded(client, seeded, db, monkeypatch) -> None:
    _kill_llm(monkeypatch)
    client.post("/api/chat", json={"user_id": seeded, "message": "hello", "history": []})
    rows = db.execute(select(AuditEvent.action, AuditEvent.policy_result)).all()
    degraded = [(a, pr) for (a, pr) in rows if a.startswith("degraded_reply:")]
    assert degraded, [a for (a, _) in rows]
    action, detail = degraded[-1]
    assert action == "degraded_reply:llm_unavailable"
    assert detail["cause"] == "llm_unavailable" and "nothing was executed" in detail["note"]


def test_readiness_calls_a_missing_model_degraded_not_down(client, monkeypatch) -> None:
    """The row worth arguing about, pinned: if the model's absence took the
    service out of rotation, the architecture would be claiming the opposite
    of what it claims."""
    def boom(*_a, **_k):
        raise httpx.ConnectError("refused")
    monkeypatch.setattr(httpx, "get", boom)
    r = client.get("/health/ready")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "degraded"
    assert body["checks"]["ollama"]["status"] == "degraded"
    assert body["checks"]["database"]["status"] == "ok"


def test_readiness_names_a_wrong_model_rather_than_failing_later(client, monkeypatch) -> None:
    """A running Ollama with the wrong model otherwise surfaces as a confusing
    runtime error on the first chat instead of a clear one here."""
    class _Resp:
        status_code = 200
        def raise_for_status(self): return None
        def json(self): return {"models": [{"name": "llama3:8b"}]}
    monkeypatch.setattr(httpx, "get", lambda *_a, **_k: _Resp())
    monkeypatch.setattr(config, "OLLAMA_MODEL", "qwen2.5:7b-instruct")
    ollama = client.get("/health/ready").json()["checks"]["ollama"]
    assert ollama["status"] == "degraded" and "not pulled" in ollama["reason"]


def test_readiness_reports_an_open_circuit_without_probing_the_model(client, monkeypatch) -> None:
    """Probing the model to answer a health check would defeat the breaker."""
    probed = []
    monkeypatch.setattr(httpx, "get", lambda *a, **k: probed.append(1))
    monkeypatch.setattr(llm_client, "circuit_state",
                        lambda: {"open": True, "consecutive_failures": 3, "cooldown_remaining_s": 12.0})
    ollama = client.get("/health/ready").json()["checks"]["ollama"]
    assert ollama["status"] == "degraded" and ollama["reason"] == "circuit breaker open"
    assert not probed, "an open breaker must short-circuit the probe too"


# ---------------------------------------------------------------------------
# Row 2/3 — Razorpay
# ---------------------------------------------------------------------------


def test_no_razorpay_still_proposes_but_cannot_settle(client, seeded, db, monkeypatch) -> None:
    from backend.services import razorpay_adapter
    monkeypatch.setattr(razorpay_adapter, "enabled", lambda: False)

    # A proposal is still recorded...
    from backend.models.entities import AuditActor, Bucket
    from backend.services import money_action_service as mas
    r = mas.create(db, user_id=seeded, action="PURCHASE", amount_paise=300 * RUPEE,
                   purpose="purchase:test", bucket=Bucket.DISCRETIONARY, actor=AuditActor.USER)
    db.commit()
    assert r.intent.status.value == "ALLOWED"

    # ...but nothing can execute, and the refusal says why.
    ex = client.post(f"/api/intents/{r.intent.id}/execute", json={"user_id": seeded})
    assert ex.status_code == 503 and "not configured" in ex.json()["detail"].lower()
    assert db.query(LedgerEvent).filter_by(intent_id=r.intent.id).count() == 0


def test_readiness_flags_a_missing_webhook_secret(client, monkeypatch) -> None:
    from backend.services import razorpay_adapter
    monkeypatch.setattr(razorpay_adapter, "enabled", lambda: True)
    monkeypatch.setattr(config, "RAZORPAY_WEBHOOK_SECRET", None)
    rp = client.get("/health/ready").json()["checks"]["razorpay"]
    assert rp["status"] == "degraded" and "webhook secret" in rp["reason"]


# ---------------------------------------------------------------------------
# Row 4 — the database is the one dependency whose loss is fatal
# ---------------------------------------------------------------------------


def test_liveness_survives_a_broken_database_but_readiness_does_not(client, monkeypatch) -> None:
    """/health must never touch a dependency, so it cannot be made to fail by
    someone else's outage; /health/ready must fail loudly."""
    assert client.get("/health").status_code == 200

    import backend.api.health as health_mod
    monkeypatch.setattr(health_mod, "_check_database",
                        lambda: {"status": "down", "error": "OperationalError: unable to open database file"})
    r = client.get("/health/ready")
    assert r.status_code == 503 and r.json()["status"] == "down"
    assert client.get("/health").status_code == 200, "liveness is independent by design"


# ---------------------------------------------------------------------------
# Row 5 — the model is up but useless
# ---------------------------------------------------------------------------


def test_a_parroted_answer_is_discarded_rather_than_shown(client, seeded, db, monkeypatch) -> None:
    from backend.agent.llm_client import ToolDecision
    question = "What's my balance?"
    monkeypatch.setattr(llm_client, "decide",
                        lambda *_a, **_k: ToolDecision(action="final_answer", tool_name=None, final_text=question))
    r = client.post("/api/chat", json={"user_id": seeded, "message": question, "history": []})
    assert r.status_code == 200
    assert r.json()["reply"].strip().lower() != question.lower(), "the question must not be echoed back as an answer"
    actions = [a for (a,) in db.execute(select(AuditEvent.action))]
    assert any(a.startswith("parrot") for a in actions), actions


def test_malformed_model_output_is_a_clean_reply_not_a_crash(client, seeded, monkeypatch) -> None:
    def bad(*_a, **_k):
        raise llm_client.LLMMalformedOutput("not json")
    monkeypatch.setattr(llm_client, "decide", bad)
    r = client.post("/api/chat", json={"user_id": seeded, "message": "hi", "history": []})
    assert r.status_code == 200 and r.json()["state"] is not None


# ---------------------------------------------------------------------------
# Row 6 — poisoned tool result: read-only survives, money locks, per-turn only
# ---------------------------------------------------------------------------


def test_a_poisoned_tool_result_locks_money_tools_for_that_turn_only(db, seeded) -> None:
    from backend.agent import tool_registry
    tool = tool_registry.get("create_payment_intent")
    args = {"action": "CONTRIBUTION", "amount_rupees": 300, "purpose": "savings_goal:x"}

    locked = orchestrator.execute_tool(db, seeded, tool, dict(args),
                                       stated_amounts=frozenset({300 * RUPEE}),
                                       user_said="add ₹300", money_locked_reason="planted offer title")
    assert locked.get("blocked") and db.query(ActionIntent).count() == 0

    # The user's own next turn is unaffected: the lock is per-turn, not a
    # permanent shutdown of the product.
    ok = orchestrator.execute_tool(db, seeded, tool, dict(args),
                                   stated_amounts=frozenset({300 * RUPEE}), user_said="add ₹300")
    assert not ok.get("blocked") and db.query(ActionIntent).count() == 1


# ---------------------------------------------------------------------------
# Phase 8 Definition of Done — the tamper demo
# ---------------------------------------------------------------------------


def test_a_forged_audit_row_is_detected_and_named(client, seeded, db) -> None:
    """Not a claim about immutability - nothing in a single database can offer
    that - but a claim that tampering cannot be SILENT, and that the system
    names the exact entry where the chain first breaks."""
    assert audit_service.verify_chain(db).ok
    first_seq = db.execute(select(AuditEvent.seq).order_by(AuditEvent.seq).limit(1)).scalar_one()

    db.execute(text("UPDATE audit_events SET action = :a WHERE seq = :s"),
               {"a": "intent:POLICY_CHECK->ALLOWED", "s": first_seq})
    db.commit()

    chain = audit_service.verify_chain(db)
    assert chain.ok is False
    assert chain.broken_at_seq == first_seq, f"the break must be located, got {chain!r}"
    assert "modified" in chain.reason
    assert client.get("/api/audit").json()["chain"]["ok"] is False, "and the UI must see it too"


def test_deleting_an_audit_row_also_breaks_the_chain(client, seeded, db) -> None:
    seqs = [s for (s,) in db.execute(select(AuditEvent.seq).order_by(AuditEvent.seq))]
    assert len(seqs) > 3
    db.execute(text("DELETE FROM audit_events WHERE seq = :s"), {"s": seqs[len(seqs) // 2]})
    db.commit()
    assert audit_service.verify_chain(db).ok is False
