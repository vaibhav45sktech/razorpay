"""Health endpoints (master build plan Phase 8 item 4).

Two endpoints, on purpose, because they answer different questions:

    GET /health        LIVENESS. Is the process up? Never touches a dependency,
                       so it cannot be made to fail by something outside this
                       process. A restart-loop supervisor reads this one.

    GET /health/ready  READINESS. Can this instance actually do its job right
                       now? Checks the database, Ollama and the Razorpay
                       configuration, and reports what each one means for the
                       user rather than just up/down.

The distinction matters here more than in most services, because this app is
DESIGNED to keep working with a dependency missing (degraded mode, master plan
Phase 8 item 8). "Ollama unreachable" is therefore NOT unready — the app still
answers with verified ledger numbers, which is most of its value. Readiness is
`degraded` in that case, and only a broken database is `down`, because without
the ledger there is nothing truthful left to say.
"""

from __future__ import annotations

import time
from typing import Any

import httpx
from fastapi import APIRouter, Response
from sqlalchemy import text

from backend import config
from backend.agent import llm_client
from backend.models import db as database
from backend.services import razorpay_adapter

router = APIRouter(tags=["health"])

#: A health check must not hang a load balancer. Deliberately shorter than any
#: request timeout in the app.
PROBE_TIMEOUT_S = 2.0


def _check_database() -> dict[str, Any]:
    t0 = time.perf_counter()
    try:
        with database.session_scope() as session:
            session.execute(text("SELECT 1"))
            journal = session.execute(text("PRAGMA journal_mode")).scalar_one_or_none() \
                if config.DATABASE_URL.startswith("sqlite") else None
        return {"status": "ok", "latency_ms": round((time.perf_counter() - t0) * 1000, 1),
                "journal_mode": journal}
    except Exception as exc:  # noqa: BLE001 - a health check reports, never raises
        return {"status": "down", "error": f"{type(exc).__name__}: {exc}"[:200]}


def _check_ollama() -> dict[str, Any]:
    """Reachability and whether the configured model is actually pulled.

    The second half matters: a running Ollama with the wrong model produces a
    confusing runtime failure on the first chat, not a clear one here.
    """
    breaker = llm_client.circuit_state()
    if breaker["open"]:
        return {"status": "degraded", "reason": "circuit breaker open", "circuit": breaker,
                "model": config.OLLAMA_MODEL}
    t0 = time.perf_counter()
    try:
        r = httpx.get(f"{config.OLLAMA_URL}/api/tags", timeout=PROBE_TIMEOUT_S)
        r.raise_for_status()
        names = [m.get("name", "") for m in (r.json().get("models") or [])]
        want = config.OLLAMA_MODEL
        # Ollama reports "qwen2.5:7b-instruct"; a config value without a tag
        # should still match, so compare on the name before the colon too.
        present = any(n == want or n.split(":")[0] == want.split(":")[0] for n in names)
        return {
            "status": "ok" if present else "degraded",
            "reason": None if present else f"model {want!r} is not pulled on this Ollama",
            "model": want, "models_available": len(names),
            "latency_ms": round((time.perf_counter() - t0) * 1000, 1),
            "circuit": breaker,
        }
    except Exception as exc:  # noqa: BLE001
        return {"status": "degraded", "reason": f"unreachable: {type(exc).__name__}",
                "model": config.OLLAMA_MODEL, "circuit": breaker,
                "note": "chat answers from the ledger with an 'assistant unavailable' note"}


def _check_razorpay() -> dict[str, Any]:
    if not razorpay_adapter.enabled():
        return {"status": "degraded", "mode": "not_configured",
                "note": "reads and policy work; nothing can be paid"}
    return {
        "status": "ok" if config.RAZORPAY_WEBHOOK_SECRET else "degraded",
        "mode": "test",
        "webhook_secret_set": config.RAZORPAY_WEBHOOK_SECRET is not None,
        "reason": None if config.RAZORPAY_WEBHOOK_SECRET
        else "no webhook secret: every webhook is rejected, settlement waits for reconciliation",
    }


@router.get("/health")
def health() -> dict[str, object]:
    """Liveness only. Touches nothing, so it cannot fail for someone else's
    reasons. Also surfaces the non-secret config for quick sanity checks."""
    return {"status": "ok", "config": config.summary()}


@router.get("/health/ready")
def ready(response: Response) -> dict[str, Any]:
    """Readiness: what works right now, and what the user would notice.

    503 only when the database is unusable. A missing model or an unconfigured
    Razorpay is `degraded`, not unready: the app is built to keep telling the
    truth about the ledger without either of them, and reporting that as a
    hard failure would hide a working system.
    """
    checks = {
        "database": _check_database(),
        "ollama": _check_ollama(),
        "razorpay": _check_razorpay(),
    }
    if checks["database"]["status"] == "down":
        status = "down"
    elif any(c["status"] == "degraded" for c in checks.values()):
        status = "degraded"
    else:
        status = "ok"

    response.status_code = 503 if status == "down" else 200
    return {
        "status": status,
        "checks": checks,
        "degraded_behaviour": (
            "See docs/degradation_matrix.md. In short: no Ollama -> real numbers with an "
            "'assistant unavailable' note; no Razorpay -> proposals but no payment; "
            "no database -> nothing, and the app says so rather than guessing."
        ),
        "debug_routes_enabled": config.DEBUG,
    }
