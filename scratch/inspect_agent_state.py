"""Dev-only inspector for Phase 4 manual adversarial testing.

manual_adversarial_tests.md says: "log the relevant AuditEvent rows for that
turn ... GET /api/state/{user_id} plus a direct look at audit_events is
enough." This script IS that direct look — there is deliberately no HTTP
endpoint for it (PRD s6: the audit trail is evidence, not a public feed), so
it reads the same SQLite file the running backend is using.

Usage (run from the repo root, with the same DATABASE_URL/.env the backend
is using):

    python scratch/inspect_agent_state.py
        -> lists every seeded user and their id

    python scratch/inspect_agent_state.py --user-id usr_xxxxxxxx
        -> last N audit_events (newest first), action_intents, and
           ledger_events for that user

    python scratch/inspect_agent_state.py --user-id usr_xxxxxxxx --limit 30
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select  # noqa: E402

from backend.models import db as database  # noqa: E402
from backend.models.entities import ActionIntent, AuditEvent, LedgerEvent, User  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--user-id", default=None, help="omit to list all seeded users and exit")
    parser.add_argument("--limit", type=int, default=15, help="rows per table (default 15)")
    args = parser.parse_args()

    session = database.SessionLocal()
    try:
        if args.user_id is None:
            users = session.execute(select(User)).scalars().all()
            print(f"{len(users)} user(s) in {database.engine.url}:")
            for u in users:
                print(f"  {u.id}  {u.name!r}  status={u.status.value}")
            print("\nRe-run with --user-id <id> to inspect that user's audit trail.")
            return

        rows = (
            session.execute(
                select(AuditEvent)
                .where(AuditEvent.user_id == args.user_id)
                .order_by(AuditEvent.seq.desc())
                .limit(args.limit)
            )
            .scalars()
            .all()
        )
        print(f"=== last {len(rows)} audit_events for {args.user_id} (newest first) ===")
        for r in rows:
            print(
                json.dumps(
                    {
                        "seq": r.seq,
                        "actor": r.actor.value,
                        "action": r.action,
                        "intent_id": r.intent_id,
                        "policy_result": r.policy_result,
                        "created_at": r.created_at.isoformat(),
                    }
                )
            )

        intents = (
            session.execute(
                select(ActionIntent)
                .where(ActionIntent.user_id == args.user_id)
                .order_by(ActionIntent.created_at.desc())
                .limit(args.limit)
            )
            .scalars()
            .all()
        )
        print(f"\n=== last {len(intents)} action_intents ===")
        for i in intents:
            print(
                f"  {i.id}  type={i.type.value}  amount_paise={i.amount_paise}  "
                f"status={i.status.value}  purpose={i.purpose!r}"
            )

        ledger = (
            session.execute(
                select(LedgerEvent)
                .where(LedgerEvent.user_id == args.user_id)
                .order_by(LedgerEvent.created_at.desc())
                .limit(args.limit)
            )
            .scalars()
            .all()
        )
        print(f"\n=== last {len(ledger)} ledger_events ===")
        for l in ledger:
            print(f"  {l.id}  type={l.type.value}  amount_paise={l.amount_paise:+d}  bucket={l.bucket.value}  source={l.source!r}")
    finally:
        session.close()


if __name__ == "__main__":
    main()
