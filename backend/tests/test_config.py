"""Phase 0 tests: configuration safety and app boot.

These exist because config.py holds a genuine safety net (the Razorpay Test
Mode guard). A safety net you have never seen fire is not a safety net, so
test_live_key_is_refused deliberately proves it fires.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend import config
from backend.main import app

REPO_ROOT = Path(__file__).resolve().parent.parent.parent

client = TestClient(app)


def test_health_returns_ok() -> None:
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_health_leaks_no_secrets() -> None:
    """The health endpoint is public-ish; it must never echo credentials."""
    body = client.get("/health").text
    for forbidden in ("KEY_SECRET", "WEBHOOK_SECRET", "rzp_test_", "rzp_live_"):
        assert forbidden not in body, f"/health leaked {forbidden}"


def test_amounts_use_paise_convention() -> None:
    """Every amount in this codebase is integer paise. See HLD s2.2."""
    assert config.CURRENCY == "INR"


@pytest.mark.parametrize("bad_key", ["rzp_live_abc123", "sk_live_xyz", "totally_wrong"])
def test_live_key_is_refused(bad_key: str) -> None:
    """THE safety net: a non-test Razorpay key must stop the process dead.

    Run in a subprocess because config validates at import time, and an already
    imported module cannot be un-imported cleanly.
    """
    result = subprocess.run(
        [sys.executable, "-c", "from backend import config"],
        cwd=REPO_ROOT,
        env={"PATH": "/usr/bin:/bin", "RAZORPAY_KEY_ID": bad_key},
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0, f"process started with key {bad_key!r} — guard failed"
    assert "rzp_test_" in result.stderr, result.stderr


def test_test_mode_key_is_accepted() -> None:
    """The guard must not be so strict that a legitimate test key is rejected."""
    result = subprocess.run(
        [sys.executable, "-c", "from backend import config; print(config.RAZORPAY_ENABLED)"],
        cwd=REPO_ROOT,
        env={
            "PATH": "/usr/bin:/bin",
            "RAZORPAY_KEY_ID": "rzp_test_fake123",
            "RAZORPAY_KEY_SECRET": "fakesecret",
        },
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "True"


def test_sqlite_parent_directory_is_created_on_demand(tmp_path) -> None:
    from backend.models import db as database

    target = tmp_path / "nested" / "deeper" / "x.db"
    database._ensure_parent_dir(f"sqlite:///{target.as_posix()}")
    assert target.parent.is_dir()
