"""Forge one audit row, so the hash chain can be seen catching it (demo aid).

The point of the audit chain is that a forgery is *detectable*, not that it is
impossible. Demonstrating that requires actually committing a forgery, which
means going around the application and writing to the database directly - the
strongest attacker available. This script is that attacker.

    python -m scripts.tamper_demo              # forge audit_events.seq = 1
    python -m scripts.tamper_demo --seq 3      # forge a different row
    python -m scripts.tamper_demo --check      # report chain status, change nothing

Then refresh the Audit trail panel: the chain pill turns red and names the
exact row. To put the database back, reseed it - the chain cannot be repaired
in place, which is the whole point:

    python -m backend.seed.demo_data --reset

Why this exists as a file rather than a one-liner: the equivalent inline
`python -c` needs nested quotes, and PowerShell mangles them. A demo command
that fails on camera is worse than no demo.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

from backend import config


def _db_path() -> Path:
    """Resolve the live database from config, so this works from any directory
    and honours a DATABASE_URL override instead of assuming ./campuspool_demo.db."""
    url = config.DATABASE_URL
    if not url.startswith("sqlite:///"):
        sys.exit(f"This demo aid only handles SQLite; DATABASE_URL is {url!r}")
    return Path(url[len("sqlite:///") :]).expanduser()


def _chain_status() -> tuple[bool, str]:
    """Ask the application's own verifier, so this reports exactly what the UI
    reports - a separate implementation here could drift and lie."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from backend.services import audit_service

    engine = create_engine(f"sqlite:///{_db_path()}", future=True)
    try:
        with sessionmaker(bind=engine, future=True)() as session:
            result = audit_service.verify_chain(session)
        if result.ok:
            return True, f"intact, {result.checked} entries"
        return False, f"BROKEN at seq {result.broken_at_seq}: {result.reason}"
    finally:
        engine.dispose()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--seq", type=int, default=1, help="which audit_events row to forge (default: 1)")
    ap.add_argument("--check", action="store_true", help="report chain status without changing anything")
    args = ap.parse_args()

    path = _db_path()
    if not path.exists():
        sys.exit(f"No database at {path}\nRun: python -m backend.seed.demo_data --reset")

    if args.check:
        ok, note = _chain_status()
        print(f"audit chain: {note}")
        return 0 if ok else 1

    ok_before, note_before = _chain_status()
    print(f"before : audit chain {note_before}")

    conn = sqlite3.connect(path)
    try:
        row = conn.execute(
            "SELECT seq, action FROM audit_events WHERE seq = ?", (args.seq,)
        ).fetchone()
        if row is None:
            sys.exit(f"No audit_events row with seq = {args.seq}. Seed the demo data first.")
        conn.execute(
            "UPDATE audit_events SET action = ? WHERE seq = ?",
            ("intent:POLICY_CHECK->ALLOWED", args.seq),
        )
        conn.commit()
    finally:
        conn.close()

    print(f"forged : seq {row[0]}  {row[1]!r}  ->  'intent:POLICY_CHECK->ALLOWED'")
    ok_after, note_after = _chain_status()
    print(f"after  : audit chain {note_after}")

    if ok_after:
        print("\nUNEXPECTED: the chain still verifies. That is a bug in the chain, not the demo.")
        return 1

    print("\nRefresh the Audit trail panel - the chain pill is now red and names the row.")
    print("Put it back with: python -m backend.seed.demo_data --reset")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
