"""agent/llm_client.py, tested with a fake httpx transport — zero real
network, zero real Ollama. Covers the format-constrained decode/fill-args
parsing, the malformed-output retry, and the circuit breaker, which is the
riskiest new code in Phase 4.
"""

from __future__ import annotations

import json

import httpx
import pytest

from backend.agent import llm_client
from backend.agent.llm_client import LLMMalformedOutput, LLMUnavailable


@pytest.fixture(autouse=True)
def _isolated_llm_client():
    """Every test gets a closed circuit breaker and no leftover transport."""
    llm_client.reset_circuit_breaker()
    llm_client._transport = None
    yield
    llm_client.reset_circuit_breaker()
    llm_client._transport = None


def _ndjson_response(*chunks: dict) -> httpx.Response:
    body = ("\n".join(json.dumps(c) for c in chunks) + "\n").encode("utf-8")
    return httpx.Response(200, content=body)


# ---------------------------------------------------------------------------
# decide()
# ---------------------------------------------------------------------------


def test_decide_parses_a_valid_streamed_response() -> None:
    decision = {"action": "call_tool", "tool_name": "get_wallet_or_ledger", "final_text": None}
    payload = json.dumps(decision)

    def handler(request: httpx.Request) -> httpx.Response:
        # Split across two chunks, the way a real streamed response arrives.
        return _ndjson_response(
            {"message": {"content": payload[: len(payload) // 2]}, "done": False},
            {"message": {"content": payload[len(payload) // 2 :]}, "done": True},
        )

    llm_client._transport = httpx.MockTransport(handler)
    result = llm_client.decide([{"role": "user", "content": "what's my balance"}], ["get_wallet_or_ledger"])
    assert result.action == "call_tool"
    assert result.tool_name == "get_wallet_or_ledger"


def test_decide_retries_once_on_malformed_json_then_succeeds() -> None:
    calls = {"n": 0}
    good = {"action": "final_answer", "tool_name": None, "final_text": "hi there"}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return _ndjson_response({"message": {"content": "not json at all"}, "done": True})
        return _ndjson_response({"message": {"content": json.dumps(good)}, "done": True})

    llm_client._transport = httpx.MockTransport(handler)
    result = llm_client.decide([{"role": "user", "content": "hello"}], [])
    assert calls["n"] == 2, "expected exactly one corrective retry"
    assert result.action == "final_answer"
    assert result.final_text == "hi there"


def test_decide_raises_malformed_output_after_two_bad_attempts() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return _ndjson_response({"message": {"content": "still not json"}, "done": True})

    llm_client._transport = httpx.MockTransport(handler)
    with pytest.raises(LLMMalformedOutput):
        llm_client.decide([{"role": "user", "content": "hello"}], [])


def test_decide_rejects_a_syntactically_valid_but_schema_invalid_object() -> None:
    """Grammar-constrained decoding guarantees shape, not semantics: a real
    model could still emit e.g. an unrecognised action value. Pydantic
    validation must catch that too, not just JSON-parse failures."""

    def handler(request: httpx.Request) -> httpx.Response:
        bad = {"action": "do_something_else", "tool_name": None, "final_text": None}
        return _ndjson_response({"message": {"content": json.dumps(bad)}, "done": True})

    llm_client._transport = httpx.MockTransport(handler)
    with pytest.raises(LLMMalformedOutput):
        llm_client.decide([{"role": "user", "content": "hello"}], [])


# ---------------------------------------------------------------------------
# fill_arguments()
# ---------------------------------------------------------------------------


def test_fill_arguments_returns_the_raw_parsed_dict() -> None:
    args = {"period": "this_month"}

    def handler(request: httpx.Request) -> httpx.Response:
        return _ndjson_response({"message": {"content": json.dumps(args)}, "done": True})

    llm_client._transport = httpx.MockTransport(handler)
    result = llm_client.fill_arguments([{"role": "user", "content": "x"}], {"type": "object"})
    assert result == args


def test_fill_arguments_rejects_a_non_object_response() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return _ndjson_response({"message": {"content": json.dumps(["not", "an", "object"])}, "done": True})

    llm_client._transport = httpx.MockTransport(handler)
    with pytest.raises(LLMMalformedOutput):
        llm_client.fill_arguments([{"role": "user", "content": "x"}], {"type": "object"})


# ---------------------------------------------------------------------------
# Unavailability + circuit breaker
# ---------------------------------------------------------------------------


def test_connect_error_raises_llm_unavailable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route to host")

    llm_client._transport = httpx.MockTransport(handler)
    with pytest.raises(LLMUnavailable):
        llm_client.decide([{"role": "user", "content": "hi"}], [])


def test_http_error_status_raises_llm_unavailable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, content=b"internal error")

    llm_client._transport = httpx.MockTransport(handler)
    with pytest.raises(LLMUnavailable):
        llm_client.decide([{"role": "user", "content": "hi"}], [])


def test_circuit_breaker_opens_after_threshold_and_then_fails_fast_without_a_network_call() -> None:
    call_count = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        call_count["n"] += 1
        raise httpx.ConnectError("down")

    llm_client._transport = httpx.MockTransport(handler)
    for _ in range(llm_client.CIRCUIT_FAILURE_THRESHOLD):
        with pytest.raises(LLMUnavailable):
            llm_client.decide([{"role": "user", "content": "hi"}], [])

    calls_before_open = call_count["n"]
    assert calls_before_open == llm_client.CIRCUIT_FAILURE_THRESHOLD

    with pytest.raises(LLMUnavailable, match="circuit open"):
        llm_client.decide([{"role": "user", "content": "hi"}], [])

    assert call_count["n"] == calls_before_open, "an open circuit must not touch the transport at all"


def test_a_success_resets_the_breaker_so_failures_must_accumulate_again() -> None:
    def bad_handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down")

    llm_client._transport = httpx.MockTransport(bad_handler)
    for _ in range(llm_client.CIRCUIT_FAILURE_THRESHOLD - 1):
        with pytest.raises(LLMUnavailable):
            llm_client.decide([{"role": "user", "content": "hi"}], [])

    good = {"action": "final_answer", "tool_name": None, "final_text": "ok"}

    def good_handler(request: httpx.Request) -> httpx.Response:
        return _ndjson_response({"message": {"content": json.dumps(good)}, "done": True})

    llm_client._transport = httpx.MockTransport(good_handler)
    result = llm_client.decide([{"role": "user", "content": "hi"}], [])
    assert result.final_text == "ok"

    # The breaker was reset by that success. One more failure alone must not
    # be enough to open it again (threshold is CIRCUIT_FAILURE_THRESHOLD).
    calls = {"n": 0}

    def counting_bad_handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        raise httpx.ConnectError("down")

    llm_client._transport = httpx.MockTransport(counting_bad_handler)
    with pytest.raises(LLMUnavailable):
        llm_client.decide([{"role": "user", "content": "hi"}], [])
    assert calls["n"] == 1, "breaker should have been closed, so this failure should reach the transport"
