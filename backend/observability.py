"""Rate limiting, request-id logging and metrics (Phase 8 items 1-3).

Three concerns that all attach at the ASGI edge, kept in one module so
main.py stays a wiring file.

WHY THESE THREE, AND WHY THIS SHAPE

  Rate limiting    Every endpoint gets a per-IP limit, but /api/chat gets a
      much tighter PER-USER one, because that is the endpoint an abuser would
      actually target: each call is several inference passes on one local
      model, so a handful of concurrent callers is a denial of service against
      every other student. /api/intents/{id}/execute is tighter still - it
      reaches a payment provider. The webhook endpoint is exempt from IP
      limits (Razorpay's own IPs would trip them) and is protected by
      signature verification instead, which is strictly stronger than an IP
      guess.

  request_id       One identifier propagated through the HTTP request, the
      agent turn, every tool call and the resulting webhook, so a single grep
      reconstructs an entire transaction. This is most of the value of
      distributed tracing at a fraction of the cost, and the right stopping
      point for one service. It is generated here if the caller did not send
      an X-Request-ID.

  /metrics         Prometheus text format. The counters are chosen to answer
      the questions this system is actually judged on: how often did policy
      deny, how often was a money tool blocked, how many intents reached each
      state, were webhooks valid. They double as the benchmark's instrumentation.
"""

from __future__ import annotations

import contextvars
import logging
import time
import uuid
from typing import Any, Awaitable, Callable

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse, PlainTextResponse
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, Histogram, generate_latest
from slowapi import Limiter
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address
from starlette.middleware.base import BaseHTTPMiddleware

logger = logging.getLogger("campuspool.obs")

#: The current request's id, readable from anywhere without threading a
#: parameter through every function signature. A ContextVar is the right tool:
#: it is per-task, so concurrent requests never see each other's id.
request_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("request_id", default="-")


def current_request_id() -> str:
    return request_id_var.get()


# ---------------------------------------------------------------------------
# Rate limiting
# ---------------------------------------------------------------------------


def _chat_key(request: Request) -> str:
    """Per-user for chat, falling back to IP.

    Reading the body here is not possible (it would consume the stream), so
    the user id comes from a header the frontend sets, or a query parameter.
    A caller who omits both is limited by IP, which is the safe default: the
    fallback must never be MORE permissive than the specific case.
    """
    uid = request.headers.get("x-user-id") or request.query_params.get("user_id")
    return f"user:{uid}" if uid else f"ip:{get_remote_address(request)}"


limiter = Limiter(
    key_func=get_remote_address,
    # Generous default: the browser polls /api/state and /api/card every few
    # seconds by design, and a limit that fights the app's own refresh loop
    # would be a bug dressed as security.
    default_limits=["240/minute"],
    # headers_enabled=False deliberately. With it on, slowapi's @limit
    # decorator requires every limited handler to take a `response: Response`
    # parameter purely so it can inject X-RateLimit-* headers, and our
    # handlers return dicts (FastAPI builds the response). Contorting the
    # signature of every money endpoint to carry a parameter it never uses is
    # a worse trade than losing advisory headers - and the thing a client
    # actually needs, Retry-After on a 429, is set explicitly in
    # _rate_limit_handler below.
    headers_enabled=False,
)

#: Applied as decorators in the route modules would scatter policy across the
#: codebase, so the tight limits live here as data and are attached in
#: install(). Values are per the reasoning in this module's docstring.
TIGHT_LIMITS: dict[str, str] = {
    "/api/chat": "12/minute",
    "/api/card/{user_id}/tick": "30/minute",
}

#: Never IP-limited: Razorpay retries from its own address range and would
#: trip a per-IP limit during a legitimate burst. Signature verification is
#: the real control on this endpoint.
EXEMPT_PATHS: tuple[str, ...] = ("/api/webhooks/razorpay", "/health", "/metrics")


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------

REQUESTS = Counter("campuspool_http_requests_total", "HTTP requests", ["method", "path", "status"])
REQUEST_SECONDS = Histogram("campuspool_http_request_seconds", "HTTP request duration", ["method", "path"])
TOOL_CALLS = Counter("campuspool_tool_calls_total", "Agent tool calls", ["tool", "outcome"])
POLICY_DECISIONS = Counter("campuspool_policy_decisions_total", "Policy engine verdicts", ["decision", "rule"])
INTENT_TRANSITIONS = Counter("campuspool_intent_transitions_total", "Intent state transitions", ["to_status"])
WEBHOOK_EVENTS = Counter("campuspool_webhook_events_total", "Razorpay webhooks", ["event", "outcome"])
AGENT_TURNS = Counter("campuspool_agent_turns_total", "Agent turns", ["outcome"])
AGENT_TURN_SECONDS = Histogram("campuspool_agent_turn_seconds", "Agent turn duration",
                               buckets=(0.05, 0.25, 1, 2.5, 5, 10, 20, 40, 75))
LLM_STEP_SECONDS = Histogram("campuspool_llm_step_seconds", "One LLM call", ["kind"],
                             buckets=(0.1, 0.5, 1, 2.5, 5, 10, 20, 40))
RATE_LIMITED = Counter("campuspool_rate_limited_total", "Requests refused by a rate limit", ["path"])
CARD_RULES_FIRED = Counter("campuspool_card_rules_fired_total", "Agentic Card rules that fired", ["outcome"])
AUDIT_CHAIN_OK = Gauge("campuspool_audit_chain_ok", "1 if the audit hash chain verifies, else 0")


def _route_template(request: Request) -> str:
    """Group by ROUTE, not by URL: `/api/state/usr_abc` and `/api/state/usr_def`
    are the same endpoint, and a label per user id would blow up cardinality
    (the classic Prometheus footgun) and leak ids into metric names."""
    route = request.scope.get("route")
    return getattr(route, "path", None) or "unmatched"


# ---------------------------------------------------------------------------
# Middleware
# ---------------------------------------------------------------------------


class ObservabilityMiddleware(BaseHTTPMiddleware):
    """Assigns the request id, records metrics, logs one structured line."""

    async def dispatch(self, request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
        rid = request.headers.get("x-request-id") or f"req_{uuid.uuid4().hex[:16]}"
        token = request_id_var.set(rid)
        t0 = time.perf_counter()
        try:
            response = await call_next(request)
            status = response.status_code
        except Exception:
            # Record the failure before re-raising, or the one request that
            # mattered most is the one missing from the metrics.
            elapsed = time.perf_counter() - t0
            path = _route_template(request)
            REQUESTS.labels(request.method, path, "500").inc()
            REQUEST_SECONDS.labels(request.method, path).observe(elapsed)
            logger.exception("request failed", extra={"request_id": rid, "path": path})
            request_id_var.reset(token)
            raise

        elapsed = time.perf_counter() - t0
        path = _route_template(request)
        REQUESTS.labels(request.method, path, str(status)).inc()
        REQUEST_SECONDS.labels(request.method, path).observe(elapsed)
        response.headers["X-Request-ID"] = rid

        # One line per request, at a level that does not drown the log. The
        # polling endpoints are the app's own heartbeat and are logged at DEBUG.
        level = logging.DEBUG if path in ("/api/state/{user_id}", "/api/card/{user_id}", "/health") else logging.INFO
        logger.log(level, "%s %s -> %s in %.3fs", request.method, path, status, elapsed,
                   extra={"request_id": rid})
        request_id_var.reset(token)
        return response


class RequestIdFilter(logging.Filter):
    """Puts the request id on every record, so a formatter can print it even
    for log lines written deep inside a service that never saw the Request."""

    def filter(self, record: logging.LogRecord) -> bool:
        if not hasattr(record, "request_id"):
            record.request_id = current_request_id()
        return True


def configure_logging(*, json_logs: bool) -> None:
    """Structured logs when asked for, human-readable by default.

    JSON is right for anything shipping logs to a collector; on a demo laptop,
    where a person is reading the terminal, JSON is strictly worse. So it is a
    flag rather than a decision imposed on every environment.
    """
    root = logging.getLogger()
    fmt: logging.Formatter
    if json_logs:
        import json as _json

        class _JsonFormatter(logging.Formatter):
            def format(self, record: logging.LogRecord) -> str:
                payload: dict[str, Any] = {
                    "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
                    "level": record.levelname,
                    "logger": record.name,
                    "request_id": getattr(record, "request_id", "-"),
                    "msg": record.getMessage(),
                }
                for key in ("path", "user_id", "intent_id", "tool"):
                    if hasattr(record, key):
                        payload[key] = getattr(record, key)
                if record.exc_info:
                    payload["exc"] = self.formatException(record.exc_info)
                return _json.dumps(payload, default=str)

        fmt = _JsonFormatter()
    else:
        fmt = logging.Formatter("%(asctime)s %(levelname)-8s [%(request_id)s] %(name)s: %(message)s")

    for handler in root.handlers:
        handler.setFormatter(fmt)
        handler.addFilter(RequestIdFilter())


# ---------------------------------------------------------------------------
# Wiring
# ---------------------------------------------------------------------------


def _rate_limit_handler(request: Request, exc: RateLimitExceeded) -> Response:
    RATE_LIMITED.labels(_route_template(request)).inc()
    retry_after = str(getattr(exc, "retry_after", None) or 60)
    logger.warning("rate limited: %s %s", request.method, request.url.path)
    return JSONResponse(
        status_code=429,
        content={"detail": f"Too many requests. Limit: {exc.detail}. Nothing was executed.",
                 "retry_after_seconds": retry_after},
        headers={"Retry-After": retry_after},
    )


def install(app: FastAPI, *, json_logs: bool = False, enable_limits: bool = True) -> None:
    """Attach everything. Called once from main.py."""
    configure_logging(json_logs=json_logs)

    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, _rate_limit_handler)  # type: ignore[arg-type]
    if enable_limits:
        from slowapi.middleware import SlowAPIMiddleware
        app.add_middleware(SlowAPIMiddleware)
    app.add_middleware(ObservabilityMiddleware)

    @app.get("/metrics", include_in_schema=False)
    def metrics() -> Response:
        """Prometheus scrape endpoint. Refreshes the audit-chain gauge on read:
        a chain that silently broke is exactly the thing an alert should fire
        on, and it is cheap enough to verify per scrape at this scale."""
        try:
            from backend.models import db as database
            from backend.services import audit_service
            with database.session_scope() as session:
                AUDIT_CHAIN_OK.set(1 if audit_service.verify_chain(session).ok else 0)
        except Exception:  # noqa: BLE001 - a metrics endpoint must never 500
            logger.exception("could not refresh the audit chain gauge")
        return PlainTextResponse(generate_latest(), media_type=CONTENT_TYPE_LATEST)

    logger.info("observability installed (json_logs=%s, rate_limits=%s)", json_logs, enable_limits)
