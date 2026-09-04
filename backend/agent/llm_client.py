"""agent/llm_client.py — the only network-ish agent code, and it's local.

DESIGN NOTE (Phase 4 Pre-Build Research Brief, Sept 2026 — treated as
authoritative for this module, superseding the native-`tools` sketch in HLD
s2.6): Ollama's native `tools` field is NOT schema-guaranteed. It inlines a
tool's schema into the prompt as text for the model to imitate; there is no
grammar constraining the arguments it returns. Worse, a filed Ollama bug
shows that combining native `tools` with Ollama's `format` parameter
(genuine grammar-constrained decoding, landed in Ollama v0.5) in the same
request can silently suppress tool calls entirely. So this client never
sends `tools`. Instead it runs a two-step hybrid, both steps plain
POST /api/chat calls using `format`:

    1. decide()         -- format-constrained to {action, tool_name,
                            final_text}, so the model can only ever name a
                            tool that actually exists, or answer directly.
    2. fill_arguments() -- format-constrained to the chosen tool's own
                            Pydantic args schema (tool_registry.ToolDef.
                            args_json_schema()), so arguments are grammar-
                            shaped to the same contract the handler expects.

This also sidesteps Ollama's native tool_calls ID-correlation problems
entirely (Ollama does not reliably return or correlate a tool_call_id across
multi-call turns) — there is no tool_calls array here to correlate, because
each step is one self-contained structured response. It also means the
orchestrator only ever asks for ONE tool per model turn, matching the
brief's finding that small local models handle a second tool call in the
same turn unreliably.

Grammar-constrained decoding guarantees the response's SHAPE, not its
business-rule validity (e.g. "amount_paise > 0" isn't something a JSON-schema
grammar enforces the way llama.cpp compiles it) — callers must still run the
tool's own Pydantic validation on the returned arguments before trusting them
(agent/orchestrator.py's execute_tool does this as Guardrail 1).

RESILIENCE (master build plan Phase 4 Step 4, sharpened by the research
brief): rather than one flat per-call timeout, this client streams the
response and enforces a time-to-first-token (TTFT) bound and a separate,
tighter inter-chunk stall bound, plus an absolute hard cap regardless of
chunk activity. A small circuit breaker sits on top so a run of failures
stops hammering a down/overloaded Ollama and fails fast into degraded mode
for a cooldown period instead of retrying every single call.

Known limitation, documented rather than hidden: httpx's `read` timeout is a
single per-socket-read bound, not two independently configurable TTFT/stall
values. This client sets it to TTFT_TIMEOUT_SECONDS (the more generous of the
two, since a cold model load is a normal, not a failure, condition) and
additionally enforces the tighter STALL_TIMEOUT_SECONDS itself in Python
between chunks that DO arrive. A stall longer than TTFT_TIMEOUT_SECONDS with
no chunk at all surfaces as a transport-level timeout instead of the more
specific "stalled" message — functionally equivalent for triggering degraded
mode, just less precisely labelled.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any, Literal

import httpx
from pydantic import BaseModel, ValidationError

from backend import config

logger = logging.getLogger("campuspool.llm")

# ---------------------------------------------------------------------------
# Resilience knobs
# ---------------------------------------------------------------------------

TTFT_TIMEOUT_SECONDS = 25.0   # time to first token — generous: covers a cold model load
STALL_TIMEOUT_SECONDS = 12.0  # max gap between subsequent chunks once generation has started
HARD_CAP_SECONDS = 90.0       # absolute ceiling regardless of chunk activity
CONNECT_TIMEOUT_SECONDS = 5.0

CIRCUIT_FAILURE_THRESHOLD = 3
CIRCUIT_COOLDOWN_SECONDS = 60.0


class LLMUnavailable(Exception):
    """The model could not produce a usable response in time, or the circuit
    is open. Callers should fall back to degraded mode on this."""


class LLMMalformedOutput(Exception):
    """The model responded, but not with valid JSON matching the requested
    schema, even after one corrective retry. Distinct from LLMUnavailable:
    the model is reachable, its output just isn't usable."""


# ---------------------------------------------------------------------------
# The routing decision (step 1 of the hybrid)
# ---------------------------------------------------------------------------


class ToolDecision(BaseModel):
    action: Literal["call_tool", "final_answer"]
    tool_name: str | None = None
    final_text: str | None = None


def _decision_format_schema(tool_names: list[str]) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["call_tool", "final_answer"]},
            "tool_name": {"type": ["string", "null"], "enum": [*tool_names, None]},
            "final_text": {"type": ["string", "null"]},
        },
        "required": ["action", "tool_name", "final_text"],
    }


# ---------------------------------------------------------------------------
# Circuit breaker
# ---------------------------------------------------------------------------


class _CircuitBreaker:
    """After N consecutive failures, stop calling the model for a cooldown
    period and fail fast instead — cheaper and faster to reach degraded mode
    than re-discovering the same timeout on every step of every turn."""

    def __init__(self, failure_threshold: int, cooldown_seconds: float) -> None:
        self._failure_threshold = failure_threshold
        self._cooldown_seconds = cooldown_seconds
        self._consecutive_failures = 0
        self._open_until: float | None = None

    def before_call(self) -> None:
        if self._open_until is None:
            return
        now = time.monotonic()
        if now < self._open_until:
            remaining = self._open_until - now
            raise LLMUnavailable(
                f"circuit open after {self._consecutive_failures} consecutive failures; "
                f"cooling down for {remaining:.0f}s more"
            )
        # Cooldown elapsed: let exactly one probe request through.
        self._open_until = None

    def on_success(self) -> None:
        self._consecutive_failures = 0
        self._open_until = None

    def on_failure(self) -> None:
        self._consecutive_failures += 1
        if self._consecutive_failures >= self._failure_threshold and self._open_until is None:
            self._open_until = time.monotonic() + self._cooldown_seconds
            logger.warning(
                "LLM circuit breaker OPEN after %d consecutive failures; cooling down %ss",
                self._consecutive_failures,
                self._cooldown_seconds,
            )

    def reset(self) -> None:
        """Test/ops hook: force the breaker back to closed."""
        self._consecutive_failures = 0
        self._open_until = None


_breaker = _CircuitBreaker(CIRCUIT_FAILURE_THRESHOLD, CIRCUIT_COOLDOWN_SECONDS)


def reset_circuit_breaker() -> None:
    """Exposed for tests; also useful operationally after a known Ollama
    restart to skip waiting out a stale cooldown."""
    _breaker.reset()


# Test seam: a ScriptedLLM-style test can point this at an httpx.MockTransport
# to exercise streaming/timeout/circuit-breaker behaviour with zero real
# network calls. None (the default) means "use the real network".
_transport: httpx.BaseTransport | None = None


# ---------------------------------------------------------------------------
# The streaming call itself
# ---------------------------------------------------------------------------


def _post_stream(messages: list[dict], fmt: dict[str, Any] | str, *, temperature: float = 0.1) -> str:
    """POST /api/chat with stream=True and a `format` constraint; return the
    concatenated message content once the stream completes.

    Raises LLMUnavailable on any failure to reach or finish talking to
    Ollama in time. Updates the module-level circuit breaker on every
    attempt.
    """
    _breaker.before_call()

    url = f"{config.OLLAMA_URL}/api/chat"
    payload = {
        "model": config.OLLAMA_MODEL,
        "messages": messages,
        "format": fmt,
        "stream": True,
        "keep_alive": config.OLLAMA_KEEP_ALIVE,
        "options": {"temperature": temperature},
    }

    started = time.monotonic()
    first_chunk_at: float | None = None
    last_chunk_at = started
    content_parts: list[str] = []

    timeout = httpx.Timeout(connect=CONNECT_TIMEOUT_SECONDS, read=TTFT_TIMEOUT_SECONDS, write=10.0, pool=5.0)

    try:
        with (
            httpx.Client(timeout=timeout, transport=_transport) as client,
            client.stream("POST", url, json=payload) as response,
        ):
            response.raise_for_status()
            for line in response.iter_lines():
                now = time.monotonic()

                if now - started > HARD_CAP_SECONDS:
                    raise LLMUnavailable(f"exceeded hard cap of {HARD_CAP_SECONDS}s")

                if first_chunk_at is None:
                    first_chunk_at = now
                elif now - last_chunk_at > STALL_TIMEOUT_SECONDS:
                    raise LLMUnavailable(f"stalled for over {STALL_TIMEOUT_SECONDS}s mid-response")
                last_chunk_at = now

                if not line:
                    continue
                chunk = json.loads(line)
                piece = chunk.get("message", {}).get("content", "")
                if piece:
                    content_parts.append(piece)
                if chunk.get("done"):
                    break
    except httpx.ConnectError as exc:
        _breaker.on_failure()
        raise LLMUnavailable(f"could not reach Ollama at {config.OLLAMA_URL}: {exc}") from exc
    except httpx.ReadTimeout as exc:
        _breaker.on_failure()
        label = "no response" if first_chunk_at is None else "stalled"
        raise LLMUnavailable(f"{label} (read timeout talking to Ollama): {exc}") from exc
    except httpx.HTTPStatusError as exc:
        _breaker.on_failure()
        raise LLMUnavailable(f"Ollama returned HTTP {exc.response.status_code}") from exc
    except LLMUnavailable:
        _breaker.on_failure()
        raise
    except Exception as exc:  # noqa: BLE001 - any other transport hiccup is still "unavailable", not a crash
        _breaker.on_failure()
        raise LLMUnavailable(f"unexpected error talking to Ollama: {exc}") from exc

    _breaker.on_success()
    return "".join(content_parts)


def _chat_json(messages: list[dict], fmt: dict[str, Any] | str, *, temperature: float = 0.1) -> Any:
    """One format-constrained call, parsed as JSON. Retries exactly once with
    a corrective instruction if the content doesn't parse — grammar-
    constrained decoding should make this rare, but small local models have
    been observed truncating output or wrapping it in stray text."""
    raw = _post_stream(messages, fmt, temperature=temperature)
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        logger.warning("LLM response was not valid JSON on first attempt; retrying once")
        corrective = [
            *messages,
            {
                "role": "user",
                "content": (
                    "Your last response was not valid JSON matching the required schema. "
                    "Respond with ONLY the JSON object, nothing else."
                ),
            },
        ]
        raw2 = _post_stream(corrective, fmt, temperature=temperature)
        try:
            return json.loads(raw2)
        except (json.JSONDecodeError, TypeError) as exc:
            raise LLMMalformedOutput(f"model did not return valid JSON after a retry: {raw2!r}") from exc


# ---------------------------------------------------------------------------
# Public API used by agent/orchestrator.py
# ---------------------------------------------------------------------------


def decide(messages: list[dict], tool_names: list[str]) -> ToolDecision:
    """Step 1: ask the model to either answer directly or name one tool to call."""
    fmt = _decision_format_schema(tool_names)
    obj = _chat_json(messages, fmt)
    try:
        return ToolDecision.model_validate(obj)
    except ValidationError as exc:
        raise LLMMalformedOutput(f"decision did not match the required schema: {obj!r}") from exc


def fill_arguments(messages: list[dict], args_json_schema: dict[str, Any]) -> dict[str, Any]:
    """Step 2: ask the model to fill in one tool's arguments, grammar-
    constrained to that tool's own JSON schema. Returns the raw parsed dict;
    the caller (execute_tool) still runs full Pydantic validation on it —
    constrained decoding guarantees shape, not business-rule validity."""
    obj = _chat_json(messages, args_json_schema)
    if not isinstance(obj, dict):
        raise LLMMalformedOutput(f"expected a JSON object of tool arguments, got: {obj!r}")
    return obj


def prewarm() -> None:
    """A throwaway one-token call at startup, so the first real user request
    doesn't pay model-load cost (master build plan Phase 4 Step 4)."""
    try:
        _post_stream([{"role": "user", "content": "hi"}], fmt="json", temperature=0.0)
        logger.info("LLM prewarm succeeded for model %s", config.OLLAMA_MODEL)
    except LLMUnavailable as exc:
        logger.warning("LLM prewarm failed (will retry lazily on first real request): %s", exc)


def log_model_digest() -> None:
    """Record what Ollama reports for the configured model at startup, so a
    silently swapped or truncated model shows up in logs instead of as
    mysterious behaviour later (Production Readiness s3.11)."""
    try:
        r = httpx.post(f"{config.OLLAMA_URL}/api/show", json={"name": config.OLLAMA_MODEL}, timeout=10.0)
        r.raise_for_status()
        info = r.json()
        digest = info.get("digest") or info.get("details", {}).get("digest") or "unknown"
        logger.info("LLM model %s digest=%s", config.OLLAMA_MODEL, digest)
    except Exception as exc:  # noqa: BLE001 - best-effort log line, never fatal at startup
        logger.warning("Could not fetch model digest for %s: %s", config.OLLAMA_MODEL, exc)
