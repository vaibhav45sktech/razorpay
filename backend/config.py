"""Single source of configuration for the CampusPool prototype.

Engineering rule (Playbook A.5): this is the ONLY module in the codebase that
reads os.environ. Everything else imports these names. That keeps the whole
configuration surface visible in one file and makes environment swaps trivial.

Hard product rule (PRD s11, Project Instructions s2): this prototype runs in
Razorpay TEST MODE ONLY. If a non-test key is ever supplied, the process
refuses to start rather than risking a real-money call.
"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

# Project root = the directory containing this file's parent (repo root).
BASE_DIR = Path(__file__).resolve().parent.parent

# Load .env from the repo root, if present. Real environment variables always
# win over .env values, which is what you want in CI or a deployed setting.
load_dotenv(BASE_DIR / ".env", override=False)


def _get_bool(name: str, default: bool = False) -> bool:
    """Parse a boolean env var tolerantly ('true', '1', 'yes' all mean True)."""
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


# --------------------------------------------------------------------------
# Razorpay — TEST MODE ONLY
# --------------------------------------------------------------------------
# These are intentionally optional: Phases 0-4 of the build have no payment
# integration at all, and the app must run without them. Phase 5 fills them in.
RAZORPAY_KEY_ID: str | None = os.environ.get("RAZORPAY_KEY_ID") or None
RAZORPAY_KEY_SECRET: str | None = os.environ.get("RAZORPAY_KEY_SECRET") or None
RAZORPAY_WEBHOOK_SECRET: str | None = os.environ.get("RAZORPAY_WEBHOOK_SECRET") or None

# THE GUARD. Written in Phase 0 on purpose: it costs nothing today, and it is
# not something anyone remembers to add later under deadline pressure.
if RAZORPAY_KEY_ID is not None and not RAZORPAY_KEY_ID.startswith("rzp_test_"):
    raise SystemExit(
        "FATAL: RAZORPAY_KEY_ID does not start with 'rzp_test_'.\n"
        "This prototype is a hackathon demo and runs in Razorpay TEST MODE only "
        "(PRD s11). Refusing to start.\n"
        "Fix: use a Test Mode key from the Razorpay dashboard, or leave the "
        "variable blank until Phase 5."
    )

RAZORPAY_ENABLED: bool = RAZORPAY_KEY_ID is not None and RAZORPAY_KEY_SECRET is not None


# --------------------------------------------------------------------------
# Local LLM (Ollama) — see HLD s2.6
# --------------------------------------------------------------------------
OLLAMA_URL: str = os.environ.get("OLLAMA_URL", "http://localhost:11434")
OLLAMA_MODEL: str = os.environ.get("OLLAMA_MODEL", "qwen2.5:7b-instruct")

# --------------------------------------------------------------------------
# Passive watcher (backend/watcher) — background suggestions from the ledger
# --------------------------------------------------------------------------
# A deliberately SMALL model: the watcher only phrases one sentence from facts
# the code already decided, so a 1.5B model is plenty and keeps RAM low while
# running all day. Falls back to templated text if Ollama is unreachable.
WATCHER_MODEL: str = os.environ.get("WATCHER_MODEL", "qwen2.5:1.5b-instruct")
WATCHER_POLL_SECONDS: float = float(os.environ.get("WATCHER_POLL_SECONDS", "15"))
# Cooldown before the same kind of suggestion may repeat for the same user/key.
WATCHER_COOLDOWN_HOURS: float = float(os.environ.get("WATCHER_COOLDOWN_HOURS", "24"))


# --------------------------------------------------------------------------
# Database
# --------------------------------------------------------------------------
DATABASE_URL: str = os.environ.get("DATABASE_URL", f"sqlite:///{BASE_DIR / 'campuspool_demo.db'}")


# --------------------------------------------------------------------------
# App
# --------------------------------------------------------------------------
# DEBUG gates the /debug/* routes (notably the Phase 3 fake payment settler).
# MUST be false once real Razorpay is wired in Phase 5.
DEBUG: bool = _get_bool("DEBUG", default=True)

# Currency: every amount in this codebase is an integer number of paise.
# 500 rupees == 50000 paise. Razorpay's API also uses paise, which removes a
# whole class of float/conversion bugs. See HLD s2.2.
CURRENCY: str = "INR"


def summary() -> dict[str, object]:
    """Non-secret config snapshot, safe to log or expose on a health endpoint."""
    return {
        "razorpay_enabled": RAZORPAY_ENABLED,
        "razorpay_mode": "test" if RAZORPAY_KEY_ID else "not_configured",
        "ollama_url": OLLAMA_URL,
        "ollama_model": OLLAMA_MODEL,
        "watcher_model": WATCHER_MODEL,
        "watcher_poll_seconds": WATCHER_POLL_SECONDS,
        "database": DATABASE_URL.split("/")[-1],
        "debug": DEBUG,
    }
