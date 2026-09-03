"""Shared FastAPI dependencies."""

from __future__ import annotations

from fastapi import HTTPException

from backend import config


def require_debug() -> None:
    """Gate for /debug/* routes.

    Returns 404 - not 403 - when DEBUG is off, so the routes are
    indistinguishable from nonexistent in a non-debug deployment. Phase 5's
    checklist confirms this fires once real Razorpay is wired.
    """
    if not config.DEBUG:
        raise HTTPException(status_code=404, detail="Not Found")
