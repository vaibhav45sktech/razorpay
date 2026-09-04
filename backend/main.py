"""FastAPI application entrypoint for the CampusPool prototype.

Routers are added phase by phase per the master build plan. Keeping this file thin is deliberate — it wires things
together and owns nothing.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from collections.abc import AsyncIterator

from fastapi import FastAPI

from backend import config
from backend.agent import llm_client
from backend.api import chat as chat_routes
from backend.api import checkout as checkout_routes
from backend.api import debug as debug_routes
from backend.api import intents as intent_routes
from backend.api import state as state_routes
from backend.api import webhooks as webhook_routes
from backend.models import db as database

# Playbook A.6: real logging from day one, not "when something breaks".
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
)
logger = logging.getLogger("campuspool")

@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    """Startup/shutdown hook.

    Logs the non-secret config on boot so the running state is never a mystery
    (Playbook A.6). Uses the lifespan pattern rather than the deprecated
    @app.on_event("startup") decorator.
    """
    logger.info("CampusPool starting with config: %s", config.summary())
    database.create_all()
    if not config.RAZORPAY_ENABLED:
        logger.info("Razorpay not configured yet - expected before Phase 5.")
    if config.DEBUG:
        logger.warning("DEBUG=true: /debug/* routes (fake payment settler) are ENABLED.")

    # Phase 4 Step 4: a silently swapped/truncated model shows up in logs
    # rather than as mysterious behaviour later, and the first real user
    # request doesn't pay model-load cost. Both are best-effort and never
    # fatal — an unreachable Ollama at startup is normal in dev/CI and is
    # handled at request time by degraded mode, not by refusing to boot.
    llm_client.log_model_digest()
    llm_client.prewarm()

    # Phase 5 Step 8: the stuck-intent sweeper. Only meaningful with Razorpay
    # configured; runs forever in the background, survives its own errors.
    sweeper: asyncio.Task | None = None
    if config.RAZORPAY_ENABLED:
        sweeper = asyncio.create_task(_sweeper_loop())
        logger.info("Reconciliation sweeper started (every %ss).", config.RECONCILE_INTERVAL_SECONDS)
        logger.info("Register this webhook URL in the Razorpay dashboard (Test Mode): %s/api/webhooks/razorpay",
                    config.PUBLIC_BASE_URL)
        if not config.RAZORPAY_WEBHOOK_SECRET:
            logger.warning("RAZORPAY_WEBHOOK_SECRET is not set: every webhook will be rejected (400) until it is.")

    yield
    if sweeper is not None:
        sweeper.cancel()
    logger.info("CampusPool shutting down.")


async def _sweeper_loop() -> None:
    from backend.services import reconciliation_service

    while True:
        await asyncio.sleep(config.RECONCILE_INTERVAL_SECONDS)
        try:
            with database.session_scope() as session:
                report = await asyncio.to_thread(reconciliation_service.sweep_stuck_intents, session)
            if report.checked:
                logger.info("sweeper: %s", report.as_dict())
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - a background loop must survive and say why
            logger.exception("sweeper pass failed; will retry")


app = FastAPI(
    title="CampusPool API",
    description=(
        "Hackathon prototype: student savings + simulated community pool + "
        "partner rewards + a rule-bound financial agent. "
        "ALL DATA IS SYNTHETIC. ALL PAYMENTS ARE RAZORPAY TEST MODE."
    ),
    version="0.0.0",
    lifespan=lifespan,
)


app.include_router(state_routes.router)
app.include_router(intent_routes.router)
app.include_router(chat_routes.router)
app.include_router(checkout_routes.router)
app.include_router(webhook_routes.router)
app.include_router(debug_routes.router)


@app.get("/health")
def health() -> dict[str, object]:
    """Liveness probe. Also surfaces non-secret config for quick sanity checks."""
    return {"status": "ok", "config": config.summary()}
