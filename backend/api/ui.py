"""Serves the frontend (frontend/) from the same process as the API.

Plain HTML/CSS/JS, no build step (master plan Phase 6 item 1: "plain HTML if
time is tight — nothing here needs a framework"). The page gets the same
strict Content-Security-Policy as the Phase 5 checkout page (Phase 6 item 3):
scripts only from this origin and Razorpay's checkout host; no inline
scripts at all.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import FileResponse, RedirectResponse, Response

FRONTEND_DIR = Path(__file__).resolve().parents[2] / "frontend"

router = APIRouter(tags=["ui"])

APP_CSP = (
    "default-src 'none'; "
    "script-src 'self' https://checkout.razorpay.com; "
    "style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data: https://*.razorpay.com; "
    "font-src 'self'; "
    "connect-src 'self' https://api.razorpay.com https://lumberjack.razorpay.com; "
    "frame-src https://api.razorpay.com https://checkout.razorpay.com; "
    "base-uri 'none'; form-action 'self'; frame-ancestors 'none'"
)
_HEADERS = {"Content-Security-Policy": APP_CSP, "X-Frame-Options": "DENY", "Referrer-Policy": "no-referrer",
            "X-Content-Type-Options": "nosniff", "Cache-Control": "no-cache"}

_TYPES = {".css": "text/css", ".js": "application/javascript", ".svg": "image/svg+xml", ".png": "image/png",
          ".woff2": "font/woff2", ".ico": "image/x-icon"}


@router.get("/", include_in_schema=False)
def index() -> Response:
    return FileResponse(FRONTEND_DIR / "index.html", media_type="text/html", headers=_HEADERS)


@router.get("/app", include_in_schema=False)
def app_alias() -> Response:
    """The README and demo script say /app; the page itself is served at /.
    Both must work, because a 404 in front of a judge is unrecoverable."""
    return RedirectResponse("/", status_code=307)


@router.get("/app/{asset}", include_in_schema=False)
def asset(asset: str) -> Response:
    path = (FRONTEND_DIR / asset).resolve()
    if path.parent != FRONTEND_DIR.resolve() or not path.is_file():
        return Response(status_code=404)
    return FileResponse(path, media_type=_TYPES.get(path.suffix, "application/octet-stream"), headers=_HEADERS)
