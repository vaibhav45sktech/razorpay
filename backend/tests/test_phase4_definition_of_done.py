"""Phase 4 Definition of Done — the audit log narrates an entire
conversation's decisions truthfully from the database alone, and the hash
chain over it is unbroken after a real agent turn (not just after Phase 1-3
writes, which test_audit_chain.py already covers).
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from sqlalchemy import select

from backend.agent import llm_client, orchestrator
from backend.agent.llm_client import ToolDecision
from backend.models.entities import AuditEvent, User
from backend.seed import demo_data
from backend.services import audit_service


@pytest.fixture()
def aarav(db) -> User:
    demo_data.seed_all(db)
    db.commit()
    return db.execute(select(User).where(User.name == demo_data.PRIMARY_DEMO_USER)).scalar_one()


def _script(monkeypatch: pytest.MonkeyPatch, decisions: list[ToolDecision], arg_sets: list[dict]) -> None:
    d_it: Iterator[ToolDecision] = iter(decisions)
    a_it: Iterator[dict] = iter(arg_sets)
    monkeypatch.setattr(llm_client, "decide", lambda messages, tool_names: next(d_it))
    monkeypatch.setattr(llm_client, "fill_arguments", lambda messages, schema: next(a_it))


def test_audit_chain_survives_a_full_agent_turn(db, aarav, monkeypatch) -> None:
    _script(
        monkeypatch,
        [
            ToolDecision(action="call_tool", tool_name="get_wallet_or_ledger", final_text=None),
            ToolDecision(action="call_tool", tool_name="check_policy", final_text=None),
            ToolDecision(action="call_tool", tool_name="create_payment_intent", final_text=None),
            ToolDecision(action="final_answer", tool_name=None, final_text="Added ₹300 — pending confirmation."),
        ],
        arg_sets=[
            {},
            {"action": "CONTRIBUTION", "amount_rupees": 300, "purpose": "savings_goal:e2e"},
            {"action": "CONTRIBUTION", "amount_rupees": 300, "purpose": "savings_goal:e2e"},
        ],
    )

    reply = orchestrator.run_agent_turn(db, aarav.id, "add ₹300 to my savings")
    db.commit()

    chain = audit_service.verify_chain(db)
    assert chain.ok, f"audit chain broke: {chain.reason}"
    assert chain.checked > 0

    rows = db.execute(select(AuditEvent).order_by(AuditEvent.seq)).scalars().all()
    story = [(r.actor.value, r.action, r.policy_result) for r in rows]

    # The database alone tells the true story, in order, without needing the
    # chat transcript at all:
    actions = [a for _, a, _ in story]
    assert "chat_turn_started" in actions
    assert "tool:get_wallet_or_ledger" in actions
    assert "tool:check_policy" in actions
    assert "forced_policy_check" in actions, "the independent re-check ran even though check_policy was also called"
    assert "tool:create_payment_intent" in actions
    assert "chat_turn_final_answer" in actions
    assert "blocked_money_tool:create_payment_intent" not in actions

    # And the forced re-check's own recorded decision matches what actually
    # happened to the intent (ALLOW -> ALLOWED, not a mismatched story).
    forced_checks = [r for r in rows if r.action == "forced_policy_check"]
    assert forced_checks[-1].policy_result["decision"] == "ALLOW"

    from backend.models.entities import ActionIntent, IntentStatus

    intent = db.execute(
        select(ActionIntent).where(ActionIntent.user_id == aarav.id, ActionIntent.purpose == "savings_goal:e2e")
    ).scalar_one()
    assert intent.status is IntentStatus.ALLOWED


def test_degraded_mode_is_also_fully_audited(db, aarav, monkeypatch) -> None:
    """Even when the LLM is dead, the turn's start is on the record — an
    outage is a fact the audit trail should show, not a gap in it."""

    def raise_unavailable(messages, tool_names):
        raise llm_client.LLMUnavailable("simulated outage")

    monkeypatch.setattr(llm_client, "decide", raise_unavailable)

    reply = orchestrator.run_agent_turn(db, aarav.id, "what's my balance?")
    db.commit()

    assert reply.degraded is True
    chain = audit_service.verify_chain(db)
    assert chain.ok

    rows = db.execute(select(AuditEvent).where(AuditEvent.user_id == aarav.id)).scalars().all()
    assert any(r.action == "chat_turn_started" for r in rows)
