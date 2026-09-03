"""FastAPI application entrypoint for the CampusPool prototype.

Routers are added phase by phase per the master build plan. Keeping this file thin is deliberate — it wires things
together and owns nothing.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from collections.abc import AsyncIterator

from fastapi import FastAPI

from backend import config
from backend.api import debug as debug_routes
from backend.api import intents as intent_routes
from backend.api import state as state_routes
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
    yield
    logger.info("CampusPool shutting down.")


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
app.include_router(debug_routes.router)


@app.get("/health")
def health() -> dict[str, object]:
    """Liveness probe. Also surfaces non-secret config for quick sanity checks."""
    return {"status": "ok", "config": config.summary()}
