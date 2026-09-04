"""agent/orchestrator.py — the agent loop itself (HLD s2.8, centerpiece of Phase 4).

Read this file twice; everything else in agent/ is plumbing around it.

THE ONE GUARANTEE THAT MATTERS
-------------------------------
execute_tool() re-runs policy_engine.check_policy() itself before every
money-moving tool call, UNCONDITIONALLY — regardless of whether the model
already called the check_policy tool earlier in this same turn, and
regardless of what that earlier call returned. The system prompt asks the
model to check policy first; this function is what actually guarantees it.
A confused or adversarial model cannot get a payment through by skipping the
check, lying about having checked, or asking five times with mounting
insistence — the policy engine has no memory of persuasion (PRD s5.4) and
this code never trusts the model's account of its own behaviour.

Belt-and-suspenders, not redundancy: money_action_service.create() (Phases
1-3) ALSO runs check_policy internally and freezes its own decision onto the
ActionIntent row. That is real, independent protection at the state-machine
layer. The check here is a SEPARATE gate in front of the LLM-facing path,
so a DENY is refused before create() is ever reached at all, independent of
whatever create() would itself have decided. Two gates, not one relying on
the other.

WHY THE LOOP LOOKS DIFFERENT FROM THE HLD s2.8 SKETCH
-------------------------------------------------------
The HLD sketch drives this loop with Ollama's native `tools` field and a
`tool_calls` array. Per the Phase 4 Pre-Build Research Brief, that field is
not schema-guaranteed and cannot be safely combined with schema-constrained
decoding. agent/llm_client.py instead runs a two-step, format-constrained
hybrid (decide() then fill_arguments()), and this loop is shaped around
that: one tool call per model turn, no tool_call_id to correlate (there is
no tool_calls array here at all), and the model's "tool request" and
"final answer" are the same kind of structured message.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import time
from typing import Any

from pydantic import BaseModel, ValidationError
from sqlalchemy.orm import Session

from backend.agent import llm_client, prompts, tool_registry
from backend.agent.tool_registry import Caller, ToolDef
from backend.models.entities import AuditActor, PolicyDecision
from backend.services import audit_service, policy_engine, state_service

logger = logging.getLogger("campuspool.agent")

#: Hard step budget: the model cannot loop forever (master build plan Phase 4 Step 6).
MAX_STEPS = 8

#: Wall-clock budget ON TOP OF the step count. MAX_STEPS bounds *count*, not
#: *time* — 8 slow steps is still a broken demo.
TURN_BUDGET_SECONDS = 45.0

#: Tools that require the independent policy re-check before they may run.
MONEY_TOOLS: frozenset[str] = frozenset({"create_payment_intent"})

_UNAVAILABLE_TEXT = (
    "The assistant is temporarily unavailable, so I can't chat right now — "
    "but here are your current verified numbers."
)
_EXHAUSTED_TEXT = (
    "I couldn't finish this in my step budget — nothing was executed beyond what I "
    "already reported. Please try a simpler request."
)


@dataclasses.dataclass
class AgentReply:
    text: str
    steps: int
    exhausted: bool = False
    degraded: bool = False
    #: Always the freshly-observed, verified state (HLD s2.8 step "OBSERVE") —
    #: present even on a normal reply, so a caller never depends on chat text
    #: alone for numbers (Production Readiness s3.1).
    state: dict[str, Any] | None = None


def observe(session: Session, user_id: str) -> dict[str, Any]:
    """Deterministic pre-fetch of verified state, injected as context — never
    trusted from the model. The same function GET /api/state calls, so the
    agent can never "know" a number the UI doesn't (state_service docstring).
    May raise state_service.UnknownUser; callers (the API layer) turn that
    into a 404, the same way GET /api/state already does.
    """
    return state_service.get_state(session, user_id)


def run_agent_turn(
    session: Session,
    user_id: str,
    user_message: str,
    history: list[dict[str, Any]] | None = None,
) -> AgentReply:
    history = list(history or [])
    state = observe(user_id=user_id, session=session)

    tools = tool_registry.llm_visible_tools()
    tool_names = [t.name for t in tools]
    catalog = prompts.render_tool_catalog(tools)

    messages: list[dict[str, Any]] = [
        {"role": "system", "content": prompts.SYSTEM_PROMPT},
        {"role": "system", "content": f"Available tools:\n{catalog}"},
        {
            "role": "system",
            "content": f"Current verified state (from the ledger, not memory): {json.dumps(state, default=str)}",
        },
        *history,
        {"role": "user", "content": user_message},
    ]

    audit_service.write(
        session, actor=AuditActor.USER, action="chat_turn_started", user_id=user_id,
        inputs={"message": user_message},
    )

    started = time.monotonic()

    for step in range(MAX_STEPS):
        if time.monotonic() - started > TURN_BUDGET_SECONDS:
            return _exhausted_reply(step, state, reason="turn_time_budget_exceeded")

        try:
            decision = llm_client.decide(messages, tool_names)
        except llm_client.LLMUnavailable as exc:
            logger.warning("LLM unavailable mid-turn for user %s: %s", user_id, exc)
            return _degraded_reply(state, step)
        except llm_client.LLMMalformedOutput as exc:
            # A model that cannot even produce a valid routing decision after
            # llm_client's own retry is not usefully steerable this turn.
            logger.warning("LLM malformed routing decision for user %s: %s", user_id, exc)
            return _degraded_reply(state, step)

        if decision.action == "final_answer":
            reply_text = decision.final_text or "I don't have anything further to add."
            audit_service.write(session, actor=AuditActor.LLM, action="chat_turn_final_answer", user_id=user_id)
            return AgentReply(text=reply_text, steps=step + 1, state=state)

        name = decision.tool_name
        messages.append({"role": "assistant", "content": json.dumps(decision.model_dump())})

        tool = tool_registry.get(name) if name else None

        # Guardrail 0: unknown tool name, or a tool the model has no business
        # naming (backend-only / system-only) → structured refusal, not a
        # crash. This is defense in depth: agent/llm_client.decide() already
        # grammar-constrains tool_name to the LLM-visible enum, so a real
        # model talking through it cannot normally reach this branch at all —
        # but nothing here trusts that constraint to have actually held.
        if tool is None or tool.caller is not Caller.LLM:
            audit_service.write(
                session, actor=AuditActor.LLM, action=f"blocked_tool_call:{name}", user_id=user_id,
            )
            result: dict[str, Any] = {"error": f"Tool '{name}' does not exist or is not available to you."}
        else:
            try:
                # Step 2 gets explicit task framing (see prompts.render_fill_instruction);
                # it is NOT appended to `messages`, so the transcript the model
                # sees on later steps stays decision/tool-result shaped.
                fill_messages = [
                    *messages,
                    {"role": "user", "content": prompts.render_fill_instruction(tool, user_message)},
                ]
                raw_args = llm_client.fill_arguments(fill_messages, tool.args_json_schema())
            except llm_client.LLMUnavailable as exc:
                logger.warning("LLM unavailable filling arguments for %s (user %s): %s", name, user_id, exc)
                return _degraded_reply(state, step)
            except llm_client.LLMMalformedOutput as exc:
                result = {"error": "invalid_arguments", "detail": f"could not parse arguments: {exc}"}
            else:
                result = execute_tool(session, user_id, tool, raw_args)

        messages.append({"role": "tool", "name": name or "unknown", "content": json.dumps(result, default=str)})

    return _exhausted_reply(MAX_STEPS, state, reason="step_budget_exhausted")


def execute_tool(session: Session, user_id: str, tool: ToolDef, raw_args: dict[str, Any]) -> dict[str, Any]:
    """Run one LLM-requested tool for real. Every call here — including
    refused and blocked ones — lands in the audit log.

    Preconditions the caller (run_agent_turn) has already established: `tool`
    is a registered, LLM-visible ToolDef. This function does not re-derive
    that (Guardrail 0 already ran), but asserts it as a loud invariant rather
    than silently trusting it.
    """
    assert tool.caller is Caller.LLM, f"execute_tool called with a non-LLM tool: {tool.name}"

    # Guardrail 1: schema validation. Bad args → an error the model can see
    # and retry from, not a crash. user_id is never part of `raw_args` at
    # all — it is a separate parameter here and in every handler signature,
    # injected by the caller from the session, never accepted from the model.
    try:
        parsed = tool.args_schema.model_validate(raw_args)
    except ValidationError as exc:
        return {"error": "invalid_arguments", "detail": exc.errors()}

    if tool.name in MONEY_TOOLS:
        action = getattr(parsed, "action")
        amount_paise = getattr(parsed, "amount_paise")
        purpose = getattr(parsed, "purpose")
        bucket = getattr(parsed, "bucket", None)

        # Guardrail 2 — THE load-bearing guarantee (see module docstring):
        # re-run check_policy ourselves, unconditionally, regardless of
        # whatever the model already did or claims to have done this turn.
        allow = policy_engine.check_policy(
            session, user_id=user_id, action=action, amount_paise=amount_paise, purpose=purpose, bucket=bucket,
        )
        audit_service.write(
            session, actor=AuditActor.BACKEND, action="forced_policy_check", user_id=user_id,
            policy_result=allow.as_dict(),
        )
        if allow.decision is not PolicyDecision.ALLOW:
            audit_service.write(
                session, actor=AuditActor.LLM, action=f"blocked_money_tool:{tool.name}", user_id=user_id,
                inputs=raw_args, policy_result=allow.as_dict(),
            )
            return {"blocked": True, "decision": allow.decision.value, "reason": allow.reason}

    output = tool.handler(session, user_id, parsed)
    result = output.model_dump() if isinstance(output, BaseModel) else output

    audit_service.write(
        session, actor=AuditActor.LLM, action=f"tool:{tool.name}", user_id=user_id, inputs=raw_args,
        policy_result=result if tool.name == "check_policy" else None,
    )
    return result


def _degraded_reply(state: dict[str, Any], steps: int) -> AgentReply:
    """HLD s2.8 / Production Readiness s3.1: if the model is unreachable or
    too slow, the caller still gets real ledger state — balances, goals and
    offers stay visible with the LLM completely dead. This is the difference
    between "the AI is slow" and "the app is broken"."""
    return AgentReply(text=_UNAVAILABLE_TEXT, steps=steps, degraded=True, state=state)


def _exhausted_reply(steps: int, state: dict[str, Any], *, reason: str) -> AgentReply:
    logger.info("agent turn exhausted: reason=%s steps=%s", reason, steps)
    return AgentReply(text=_EXHAUSTED_TEXT, steps=steps, exhausted=True, state=state)
