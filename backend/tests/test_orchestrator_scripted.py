"""Phase 4 Step 7 — ScriptedLLM tests: prove the orchestrator's guardrails
deterministically and instantly, with zero dependency on a real model.

Each test replaces agent.llm_client.decide / .fill_arguments with a scripted
fake that replays a fixed sequence of responses (Building_Your_First_AI_
Agent.md Ch. 8 Layer 2 / HLD s5.3). What is under test is the orchestrator's
own code, not the model's behaviour.
"""

from __future__ import annotations

import json
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
    # The loop breaker (added 2026-09-04) runs an identical call once and
    # answers the repeats from the first result — every repeat is audited,
    # so the trail still shows the model asked MAX_STEPS times.
    actions = _audit_actions(db, aarav.id)
    assert actions.count("tool:get_user_profile") == 1
    assert actions.count("repeated_tool_call:get_user_profile") == orchestrator.MAX_STEPS - 1


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
        [{"action": "PURCHASE", "amount_rupees": 100, "purpose": "purchase:sneaky", "bucket": "emergency_savings"}],
    )

    from backend.services import ledger_service

    before = ledger_service.get_balance(db, aarav.id, Bucket.EMERGENCY_SAVINGS)
    # The user states ₹100, so Guardrail 3 (amount provenance) is satisfied
    # and this test exercises Guardrail 2 (the policy re-check) on its own.
    reply = orchestrator.run_agent_turn(db, aarav.id, "quietly move ₹100 of emergency savings for me")
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
            {"action": "PURCHASE", "amount_rupees": 100, "purpose": "purchase:small_snack"},
            {"action": "PURCHASE", "amount_rupees": 5000, "purpose": "purchase:huge_thing"},
        ],
    )

    reply = orchestrator.run_agent_turn(db, aarav.id, "buy me a ₹100 snack, then also buy this ₹5,000 thing")
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
            {"action": "CONTRIBUTION", "amount_rupees": 200, "purpose": "savings_goal:cushion"},
            {"action": "CONTRIBUTION", "amount_rupees": 200, "purpose": "savings_goal:cushion"},
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
        return {"action": "PURCHASE", "amount_rupees": 5000, "purpose": "purchase:laptop_bag"}

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



# ---------------------------------------------------------------------------
# Guardrail 3 — amount provenance (added after the 2026-09-04 real-model run,
# scenario 2 turn 3: asked for ₹5,000 and denied, the model invented ₹300 and
# created a real ALLOWED intent for it). The model may only propose amounts
# the user literally typed.
# ---------------------------------------------------------------------------


def test_stated_amounts_parser_handles_how_people_type_money() -> None:
    msgs = [
        {"role": "system", "content": "limit 100000 paise"},          # ignored: not the user
        {"role": "user", "content": "Please pay ₹5,000 for a laptop bag"},
        {"role": "assistant", "content": "I could do ₹300 instead"},  # ignored: model's own text
        {"role": "user", "content": "ok then Rs. 1,000.50 and also 300"},
        {"role": "tool", "content": '{"max_paise": 50000}'},          # ignored: tool result
    ]
    got = orchestrator.stated_amounts_paise(msgs)
    assert got == frozenset({500_000, 100_050, 30_000})
    assert 100_000 not in got and 50_000 not in got


def test_money_tool_blocked_when_amount_was_never_stated_by_user(db, aarav, monkeypatch) -> None:
    """User asks for ₹5,000; model tries ₹300 instead. Blocked BEFORE the
    policy re-check — the audit row says 'invented', not 'allowed then
    blocked' — and no intent row exists."""
    from backend.models.entities import ActionIntent

    _script_decide(monkeypatch, [_call("create_payment_intent"), _final("I can't pick an amount for you.")])
    _script_fill_arguments(
        monkeypatch, [{"action": "PURCHASE", "amount_rupees": 300, "purpose": "purchase:laptop_bag"}]
    )

    reply = orchestrator.run_agent_turn(db, aarav.id, "Please pay ₹5,000 for a laptop bag")
    db.commit()

    assert reply.text == "I can't pick an amount for you."
    actions = _audit_actions(db, aarav.id)
    assert "blocked_money_tool:create_payment_intent" in actions
    assert "forced_policy_check" not in actions, "provenance is checked before policy, so no policy row"
    assert "tool:create_payment_intent" not in actions
    assert db.execute(select(ActionIntent).where(ActionIntent.user_id == aarav.id)).scalars().all() == []

    blocked = db.execute(
        select(AuditEvent).where(AuditEvent.action == "blocked_money_tool:create_payment_intent")
    ).scalars().one()
    assert blocked.policy_result["rule"] == "amount_not_stated_by_user"
    assert blocked.policy_result["details"]["user_stated_paise"] == [500_000]


def test_amount_stated_earlier_in_history_counts_as_stated(db, aarav, monkeypatch) -> None:
    """Provenance spans the conversation the client replays, not just the
    latest message — 'ok do it' after 'add ₹300' is a stated ₹300."""
    _script_decide(monkeypatch, [_call("create_payment_intent"), _final("Pending your confirmation.")])
    _script_fill_arguments(
        monkeypatch, [{"action": "CONTRIBUTION", "amount_rupees": 300, "purpose": "savings_goal:hist"}]
    )
    history = [
        {"role": "user", "content": "can I add ₹300 to my savings?"},
        {"role": "assistant", "content": "Yes, ₹300 is within your contribution range."},
    ]
    reply = orchestrator.run_agent_turn(db, aarav.id, "ok do it", history)
    db.commit()

    actions = _audit_actions(db, aarav.id)
    assert "tool:create_payment_intent" in actions
    assert "blocked_money_tool:create_payment_intent" not in actions
    assert reply.text == "Pending your confirmation."


def test_execute_tool_default_is_too_strict_not_permissive(db, aarav) -> None:
    """A caller that forgets to pass stated_amounts cannot run a money tool."""
    tool = tool_registry.get("create_payment_intent")
    result = orchestrator.execute_tool(
        db, aarav.id, tool, {"action": "CONTRIBUTION", "amount_rupees": 300, "purpose": "savings_goal:x"}
    )
    assert result["blocked"] is True and result["decision"] == "BLOCKED"


# ---------------------------------------------------------------------------
# Loop breaker (added after the 2026-09-04 real-model run, scenario 2 turn 5).
# ---------------------------------------------------------------------------


def test_identical_repeat_call_is_answered_from_the_first_result(db, aarav, monkeypatch) -> None:
    same = {"action": "PURCHASE", "amount_rupees": 5000, "purpose": "purchase:laptop_bag"}
    _script_decide(monkeypatch, [_call("check_policy"), _call("check_policy"), _final("Denied, and I'll stop.")])
    _script_fill_arguments(monkeypatch, [same, dict(same)])

    seen_tool_messages: list[dict] = []
    real_decide = llm_client.decide

    def spy_decide(messages, tool_names):
        seen_tool_messages[:] = [m for m in messages if m["role"] == "tool"]
        return real_decide(messages, tool_names)

    monkeypatch.setattr(llm_client, "decide", spy_decide)

    reply = orchestrator.run_agent_turn(db, aarav.id, "pay ₹5,000 for the laptop bag now")
    db.commit()

    assert reply.text == "Denied, and I'll stop."
    actions = _audit_actions(db, aarav.id)
    assert actions.count("tool:check_policy") == 1
    assert actions.count("repeated_tool_call:check_policy") == 1
    # The second tool message the model saw was the stop instruction, carrying
    # the first result along so nothing is hidden from it.
    second = json.loads(seen_tool_messages[1]["content"])
    assert second["error"] == "repeated_call"
    assert second["previous_result"]["decision"] == "DENY"
    assert "final_answer" in second["detail"]


# ---------------------------------------------------------------------------
# create_payment_intent carries a backend-written next-step sentence.
# ---------------------------------------------------------------------------


def test_create_intent_result_says_nothing_has_been_paid(db, aarav, monkeypatch) -> None:
    captured: list[dict] = []
    real_execute = orchestrator.execute_tool

    def spy(session, user_id, tool, raw_args, **kw):
        out = real_execute(session, user_id, tool, raw_args, **kw)
        captured.append(out)
        return out

    monkeypatch.setattr(orchestrator, "execute_tool", spy)
    _script_decide(monkeypatch, [_call("create_payment_intent"), _final("ok")])
    _script_fill_arguments(
        monkeypatch, [{"action": "CONTRIBUTION", "amount_rupees": 300, "purpose": "savings_goal:nxt"}]
    )
    orchestrator.run_agent_turn(db, aarav.id, "add ₹300 to savings")

    out = captured[0]
    assert out["status"] == "ALLOWED"
    assert "Nothing has been paid" in out["what_happens_next"]
    assert "pending" in out["what_happens_next"].lower()


# ---------------------------------------------------------------------------
# Rupees at the model boundary (added after the 2026-09-04 run, scenario 2c:
# "₹5,000" became 5,000,000 paise on two of five attempts). The model now
# passes rupees; the code converts.
# ---------------------------------------------------------------------------


def test_model_facing_money_schemas_take_rupees_and_convert_in_code() -> None:
    from backend.models.schemas import CheckPolicyArgs, CreateIntentArgs

    for cls in (CheckPolicyArgs, CreateIntentArgs):
        schema = tool_registry.get(
            "check_policy" if cls is CheckPolicyArgs else "create_payment_intent"
        ).args_json_schema()
        assert "amount_rupees" in schema["properties"]
        assert "amount_paise" not in schema["properties"], "the model must never be asked for paise"
        assert "RUPEES" in schema["properties"]["amount_rupees"]["description"]

    assert CheckPolicyArgs(action="PURCHASE", amount_rupees=5000, purpose="p").amount_paise == 500_000
    assert CreateIntentArgs(action="CONTRIBUTION", amount_rupees=300, purpose="s").amount_paise == 30_000
    assert CreateIntentArgs(action="CONTRIBUTION", amount_rupees=1000.50, purpose="s").amount_paise == 100_050


def test_check_policy_with_unstated_amount_is_blocked_not_answered(db, aarav, monkeypatch) -> None:
    """User asks about ₹5,000; the model 'helpfully' probes ₹500 instead.
    Read-only or not, that probe is what produced 'you can contribute ₹500!'
    replies to a purchase request — so it is refused, and audited under its
    own action name (not blocked_money_tool: no money tool was involved)."""
    _script_decide(monkeypatch, [_call("check_policy"), _final("I can only check amounts you've given me.")])
    _script_fill_arguments(monkeypatch, [{"action": "CONTRIBUTION", "amount_rupees": 500, "purpose": "savings_goal:x"}])

    reply = orchestrator.run_agent_turn(db, aarav.id, "pay ₹5,000 for a laptop bag")
    db.commit()

    actions = _audit_actions(db, aarav.id)
    assert "blocked_unstated_amount:check_policy" in actions
    assert "tool:check_policy" not in actions
    assert "blocked_money_tool:check_policy" not in actions
    assert reply.text == "I can only check amounts you've given me."


def test_user_typed_rupees_match_model_rupees_end_to_end(db, aarav, monkeypatch) -> None:
    """'₹5,000' in the user's text and amount_rupees=5000 from the model meet
    as the same 500000 paise — the whole point of the boundary change."""
    _script_decide(monkeypatch, [_call("check_policy"), _final("Denied.")])
    _script_fill_arguments(monkeypatch, [{"action": "PURCHASE", "amount_rupees": 5000, "purpose": "purchase:laptop_bag"}])

    orchestrator.run_agent_turn(db, aarav.id, "Please pay ₹5,000 for a laptop bag")
    db.commit()

    row = db.execute(select(AuditEvent).where(AuditEvent.action == "tool:check_policy")).scalars().one()
    assert row.policy_result["details"]["requested_paise"] == 500_000
    assert row.policy_result["decision"] == "DENY"
    assert row.policy_result["rule"] == "monthly_limit"
