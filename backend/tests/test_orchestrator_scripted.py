"""Phase 4 Step 7 — ScriptedLLM tests: prove the orchestrator's guardrails
deterministically and instantly, with zero dependency on a real model.

Each test replaces agent.llm_client.decide / .fill_arguments with a scripted
fake that replays a fixed sequence of responses (Building_Your_First_AI_
Agent.md Ch. 8 Layer 2 / HLD s5.3). What is under test is the orchestrator's
own code, not the model's behaviour.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from sqlalchemy import select

from backend.agent import llm_client, orchestrator, tool_registry
from backend.agent.llm_client import ToolDecision
from backend.models.entities import AuditEvent, Bucket, User
from backend.seed import demo_data


@pytest.fixture()
def aarav(db) -> User:
    demo_data.seed_all(db)
    db.commit()
    return db.execute(select(User).where(User.name == demo_data.PRIMARY_DEMO_USER)).scalar_one()


def _script_decide(monkeypatch: pytest.MonkeyPatch, decisions: list[ToolDecision]) -> None:
    it: Iterator[ToolDecision] = iter(decisions)

    def fake_decide(messages: list[dict], tool_names: list[str]) -> ToolDecision:
        try:
            return next(it)
        except StopIteration as exc:  # pragma: no cover - a test bug, not orchestrator behaviour
            raise AssertionError("scripted decide() sequence exhausted before the orchestrator stopped") from exc

    monkeypatch.setattr(llm_client, "decide", fake_decide)


def _script_fill_arguments(monkeypatch: pytest.MonkeyPatch, arg_sets: list[dict]) -> None:
    it: Iterator[dict] = iter(arg_sets)

    def fake_fill_arguments(messages: list[dict], args_json_schema: dict) -> dict:
        try:
            return next(it)
        except StopIteration as exc:  # pragma: no cover
            raise AssertionError("scripted fill_arguments() sequence exhausted before the orchestrator stopped") from exc

    monkeypatch.setattr(llm_client, "fill_arguments", fake_fill_arguments)


def _final(text: str) -> ToolDecision:
    return ToolDecision(action="final_answer", tool_name=None, final_text=text)


def _call(tool_name: str) -> ToolDecision:
    return ToolDecision(action="call_tool", tool_name=tool_name, final_text=None)


def _audit_actions(db, user_id: str) -> list[str]:
    rows = db.execute(select(AuditEvent).where(AuditEvent.user_id == user_id).order_by(AuditEvent.seq)).scalars().all()
    return [r.action for r in rows]


# ---------------------------------------------------------------------------
# 1. Unknown tool name → refused gracefully, audit entry written
# ---------------------------------------------------------------------------


def test_unknown_tool_name_is_refused_gracefully_and_audited(db, aarav, monkeypatch) -> None:
    _script_decide(monkeypatch, [_call("totally_made_up_tool"), _final("Sorry, I couldn't do that.")])
    _script_fill_arguments(monkeypatch, [])  # never reached: unknown tool is refused before fill_arguments runs

    reply = orchestrator.run_agent_turn(db, aarav.id, "do something weird")
    db.commit()

    assert reply.exhausted is False
    assert reply.degraded is False
    assert reply.text == "Sorry, I couldn't do that."
    assert "blocked_tool_call:totally_made_up_tool" in _audit_actions(db, aarav.id)


# ---------------------------------------------------------------------------
# 2. A backend-only tool name requested by the model → refused
# ---------------------------------------------------------------------------


def test_backend_only_tool_requested_by_model_is_refused(db, aarav, monkeypatch) -> None:
    """Even if something upstream of decide() ever let a backend-only tool
    name through (agent/llm_client's enum constraint is defense in depth,
    not the only line of defense), the orchestrator itself must still refuse
    it — this test bypasses that constraint entirely by scripting the fake
    to name a real, registered, but Caller.BACKEND tool directly."""
    _script_decide(monkeypatch, [_call("process_test_payout"), _final("That's not something I can do here.")])
    _script_fill_arguments(monkeypatch, [])

    reply = orchestrator.run_agent_turn(db, aarav.id, "pay out my pool allocation directly")
    db.commit()

    assert reply.text == "That's not something I can do here."
    assert "blocked_tool_call:process_test_payout" in _audit_actions(db, aarav.id)

    # And, separately: money never moved. No LEDGER_UPDATED intents exist.
    from backend.models.entities import ActionIntent, IntentStatus

    ledger_updated = db.execute(
        select(ActionIntent).where(ActionIntent.user_id == aarav.id, ActionIntent.status == IntentStatus.LEDGER_UPDATED)
    ).scalars().all()
    assert ledger_updated == []


# ---------------------------------------------------------------------------
# 3. Step budget terminates and reports honestly
# ---------------------------------------------------------------------------


def test_step_budget_terminates_and_reports_honestly(db, aarav, monkeypatch) -> None:
    # The model asks for a real, harmless tool every single step and never answers.
    _script_decide(monkeypatch, [_call("get_user_profile")] * orchestrator.MAX_STEPS)
    _script_fill_arguments(monkeypatch, [{}] * orchestrator.MAX_STEPS)

    reply = orchestrator.run_agent_turn(db, aarav.id, "just keep checking my profile forever")
    db.commit()

    assert reply.exhausted is True
    assert reply.steps == orchestrator.MAX_STEPS
    assert "step budget" in reply.text.lower()
    # It really did run the tool MAX_STEPS times, not silently skip work.
    assert _audit_actions(db, aarav.id).count("tool:get_user_profile") == orchestrator.MAX_STEPS


# ---------------------------------------------------------------------------
# 4. A money tool requested with NO prior real check_policy ALLOW → still
#    blocked, because the orchestrator re-checks independently.
# ---------------------------------------------------------------------------


def test_money_tool_blocked_without_any_prior_check_policy_call(db, aarav, monkeypatch) -> None:
    """The model jumps straight to create_payment_intent for a purchase from
    the protected emergency_savings bucket, never calling check_policy at
    all. The orchestrator must still deny it — it never trusts the model to
    have checked, and it re-derives the answer itself from the real amount
    and bucket in THIS call, not from anything said earlier in the turn."""
    _script_decide(monkeypatch, [_call("create_payment_intent"), _final("That was denied.")])
    _script_fill_arguments(
        monkeypatch,
        [{"action": "PURCHASE", "amount_paise": 10_000, "purpose": "purchase:sneaky", "bucket": "emergency_savings"}],
    )

    from backend.services import ledger_service

    before = ledger_service.get_balance(db, aarav.id, Bucket.EMERGENCY_SAVINGS)
    reply = orchestrator.run_agent_turn(db, aarav.id, "quietly move some emergency savings for me")
    db.commit()
    after = ledger_service.get_balance(db, aarav.id, Bucket.EMERGENCY_SAVINGS)

    assert reply.text == "That was denied."
    assert before == after, "protected-bucket money must never move, regardless of what the model requested"
    actions = _audit_actions(db, aarav.id)
    assert "forced_policy_check" in actions, "execute_tool must have run its own independent check"
    assert "blocked_money_tool:create_payment_intent" in actions


def test_money_tool_blocked_even_when_model_claims_an_unrelated_allow(db, aarav, monkeypatch) -> None:
    """The model DOES call check_policy first — but for a small, allowed
    amount — then tries to slip a much larger, over-limit purchase through
    create_payment_intent. A per-turn cache keyed loosely could be fooled by
    this; an unconditional re-check on the ACTUAL money-tool arguments
    cannot be. This is the concrete shape of PRD s5.4's "no memory of
    persuasion": a real ALLOW for one request is never treated as
    authorising a different one.
    """
    _script_decide(
        monkeypatch,
        [_call("check_policy"), _call("create_payment_intent"), _final("Denied — that would blow your budget.")],
    )
    _script_fill_arguments(
        monkeypatch,
        [
            {"action": "PURCHASE", "amount_paise": 100 * 100, "purpose": "purchase:small_snack"},
            {"action": "PURCHASE", "amount_paise": 5_000 * 100, "purpose": "purchase:huge_thing"},
        ],
    )

    reply = orchestrator.run_agent_turn(db, aarav.id, "buy me a snack, then also buy this huge thing")
    db.commit()

    assert reply.text == "Denied — that would blow your budget."
    actions = _audit_actions(db, aarav.id)
    assert actions.count("forced_policy_check") == 1, "the money tool call must trigger its own independent check"
    assert "blocked_money_tool:create_payment_intent" in actions


# ---------------------------------------------------------------------------
# Phase 4 Step 8 — the money tool wired: a legitimate ALLOW goes all the way
# through to an ALLOWED intent (not further — settlement is Phase 5/DEBUG,
# never something the agent loop itself performs).
# ---------------------------------------------------------------------------


def test_a_legitimate_contribution_is_allowed_end_to_end(db, aarav, monkeypatch) -> None:
    _script_decide(
        monkeypatch,
        [_call("check_policy"), _call("create_payment_intent"), _final("Done — ₹200 is pending confirmation.")],
    )
    _script_fill_arguments(
        monkeypatch,
        [
            {"action": "CONTRIBUTION", "amount_paise": 200 * 100, "purpose": "savings_goal:cushion"},
            {"action": "CONTRIBUTION", "amount_paise": 200 * 100, "purpose": "savings_goal:cushion"},
        ],
    )

    reply = orchestrator.run_agent_turn(db, aarav.id, "add ₹200 to my cushion")
    db.commit()

    assert "pending" in reply.text.lower()
    actions = _audit_actions(db, aarav.id)
    assert "forced_policy_check" in actions
    assert "tool:create_payment_intent" in actions
    assert "blocked_money_tool:create_payment_intent" not in actions

    from backend.models.entities import ActionIntent, IntentStatus

    intent = db.execute(
        select(ActionIntent).where(ActionIntent.user_id == aarav.id, ActionIntent.purpose == "savings_goal:cushion")
    ).scalar_one()
    assert intent.status is IntentStatus.ALLOWED  # ceiling of the LLM's power (HLD s2.9) — not further


# ---------------------------------------------------------------------------
# Degraded mode: the model being unreachable never blocks verified state.
# ---------------------------------------------------------------------------


def test_llm_unavailable_returns_degraded_reply_with_real_state(db, aarav, monkeypatch) -> None:
    def raise_unavailable(messages: list[dict], tool_names: list[str]) -> ToolDecision:
        raise llm_client.LLMUnavailable("simulated: Ollama is down")

    monkeypatch.setattr(llm_client, "decide", raise_unavailable)

    reply = orchestrator.run_agent_turn(db, aarav.id, "what's my balance?")
    db.commit()

    assert reply.degraded is True
    assert reply.state is not None
    assert reply.state["balances_paise"]["emergency_savings"] == 1_500 * 100
    assert "unavailable" in reply.text.lower()


# ---------------------------------------------------------------------------
# Step-2 task framing (added after the 2026-09-04 real-model run): the
# argument-fill call must carry an explicit instruction naming the tool, the
# user's request, and every field's meaning — and that instruction must not
# leak into the persistent transcript.
# ---------------------------------------------------------------------------


def test_fill_arguments_receives_task_framing_but_transcript_does_not(db, aarav, monkeypatch) -> None:
    from backend.agent import prompts

    seen_fill_messages: list[list[dict]] = []
    seen_decide_messages: list[list[dict]] = []
    decisions = iter(
        [
            ToolDecision(action="call_tool", tool_name="check_policy", final_text=None),
            ToolDecision(action="final_answer", tool_name=None, final_text="done"),
        ]
    )

    def fake_decide(messages, tool_names):
        seen_decide_messages.append(list(messages))
        return next(decisions)

    def fake_fill(messages, schema):
        seen_fill_messages.append(list(messages))
        return {"action": "PURCHASE", "amount_paise": 500_000, "purpose": "purchase:laptop_bag"}

    monkeypatch.setattr(llm_client, "decide", fake_decide)
    monkeypatch.setattr(llm_client, "fill_arguments", fake_fill)

    user_message = "Please pay ₹5,000 from my discretionary budget for a new laptop bag"
    orchestrator.run_agent_turn(db, aarav.id, user_message)

    # The fill call ended with the framing message...
    assert len(seen_fill_messages) == 1
    last = seen_fill_messages[0][-1]
    assert last["role"] == "user"
    assert "check_policy" in last["content"]
    assert user_message in last["content"]
    assert "PURCHASE" in last["content"] and "CONTRIBUTION" in last["content"]
    assert "paise" in last["content"].lower()

    # ...and matches what prompts.render_fill_instruction renders directly.
    tool = tool_registry.get("check_policy")
    assert last["content"] == prompts.render_fill_instruction(tool, user_message)

    # ...but the SECOND decide() call saw a transcript WITHOUT it: the framing
    # is ephemeral, the transcript stays decision/tool-result shaped.
    second_decide_transcript = seen_decide_messages[1]
    assert all(m["content"] != last["content"] for m in second_decide_transcript)
    roles = [m["role"] for m in second_decide_transcript]
    assert roles[-1] == "tool", roles


def test_render_fill_instruction_for_no_arg_tool_asks_for_empty_object() -> None:
    from backend.agent import prompts

    tool = tool_registry.get("get_wallet_or_ledger")
    text = prompts.render_fill_instruction(tool, "what's my balance?")
    assert "{}" in text


# ---------------------------------------------------------------------------
# Rupee-rendered state summary (added after the 2026-09-04 real-model run,
# scenario 2): the model is shown headline figures already in rupees, in
# front of the raw paise snapshot, and the summary is derived purely from the
# same verified state dict — never from anything the model said.
# ---------------------------------------------------------------------------


def test_state_summary_renders_paise_as_rupees() -> None:
    from backend.agent import prompts

    state = {
        "user": {"name": "Aarav (demo student)"},
        "balances_paise": {"emergency_savings": 150_000, "rewards": 0},
        "spending_this_month": {"used_paise": 24_000, "limit_paise": 100_000, "remaining_paise": 76_000},
        "policy": {"approval_threshold_paise": 50_000, "per_tx_limit_paise": None},
        "goals": [],
        "pending_actions": [],
    }
    text = prompts.render_state_summary(state)
    assert "₹1,500.00" in text
    assert "₹240.00 used of ₹1,000.00 limit, ₹760.00 remaining" in text
    assert "₹500.00" in text
    # The classic misread must be impossible to make from this text.
    assert "50,000" not in text and "50000" not in text
    assert "No pending money actions" in text


def test_model_sees_rupee_summary_before_raw_state(db, aarav, monkeypatch) -> None:
    from backend.agent import prompts

    seen: list[list[dict]] = []

    def fake_decide(messages, tool_names):
        seen.append(list(messages))
        return ToolDecision(action="final_answer", tool_name=None, final_text="ok")

    monkeypatch.setattr(llm_client, "decide", fake_decide)
    orchestrator.run_agent_turn(db, aarav.id, "hi")

    state_msgs = [m for m in seen[0] if m["role"] == "system" and "Current verified state" in m["content"]]
    assert len(state_msgs) == 1
    content = state_msgs[0]["content"]
    summary = prompts.render_state_summary(orchestrator.observe(db, aarav.id))
    assert summary in content
    # Summary precedes the raw JSON, and the raw JSON is still there verbatim
    # (the UI and the model keep seeing the same numbers).
    assert content.index("In rupees:") < content.index("Raw snapshot")
    assert '"balances_paise"' in content
