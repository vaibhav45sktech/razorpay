"""Phase 4 Step 2 — the registry's structural safety claim, tested immediately.

The single most important safety property in this system: the model cannot
request what it cannot see. llm_visible_tools() filters to Caller.LLM, and
this test proves the three backend-only money-moving tools (plus the
system-only audit tool) never appear in that filtered list.
"""

from __future__ import annotations

from backend.agent.tool_registry import TOOLS, Caller, llm_visible_tools

BACKEND_ONLY_NAMES = {"create_razorpay_payment", "get_payment_status", "process_test_payout"}
SYSTEM_ONLY_NAMES = {"write_audit_event"}


def test_backend_only_tools_are_absent_from_llm_visible_tools() -> None:
    visible_names = {t.name for t in llm_visible_tools()}
    assert visible_names.isdisjoint(BACKEND_ONLY_NAMES), (
        "A backend-only, money-moving tool leaked into the LLM-visible set: "
        f"{visible_names & BACKEND_ONLY_NAMES}"
    )


def test_system_only_tools_are_absent_from_llm_visible_tools() -> None:
    visible_names = {t.name for t in llm_visible_tools()}
    assert visible_names.isdisjoint(SYSTEM_ONLY_NAMES)


def test_backend_only_tools_are_still_registered() -> None:
    """Registered (so the registry protects something real), just not LLM-visible."""
    for name in BACKEND_ONLY_NAMES:
        assert name in TOOLS
        assert TOOLS[name].caller is Caller.BACKEND


def test_llm_visible_tools_is_nonempty_and_only_llm_caller() -> None:
    visible = llm_visible_tools()
    assert len(visible) >= 8
    assert all(t.caller is Caller.LLM for t in visible)


def test_every_registry_key_matches_its_own_tooldef_name() -> None:
    for key, tool in TOOLS.items():
        assert key == tool.name, f"registry key {key!r} does not match ToolDef.name {tool.name!r}"


def test_every_llm_tool_args_schema_produces_a_json_schema() -> None:
    """This is the exact schema agent/llm_client.py hands to Ollama's `format`
    parameter (constrained decoding) when filling a tool's arguments — it must
    be a well-formed JSON schema for every LLM-visible tool."""
    for tool in llm_visible_tools():
        schema = tool.args_json_schema()
        assert isinstance(schema, dict)
        assert schema.get("type") == "object"


def test_write_audit_event_stub_is_not_dispatchable() -> None:
    """Documentation-parity entry only; never called through the tool loop."""
    import pytest

    tool = TOOLS["write_audit_event"]
    assert tool.caller is Caller.SYSTEM
    with pytest.raises(NotImplementedError):
        tool.handler(None, "usr_x", None)
