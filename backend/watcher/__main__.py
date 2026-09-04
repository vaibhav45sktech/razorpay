"""`python -m backend.watcher [--once] [--backfill-minutes N] [--poll S]`"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime, timedelta, timezone

from backend import config
from backend.models import db as database
from backend.watcher import service


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="CampusPool passive watcher (advisory suggestions from the ledger)")
    parser.add_argument("--once", action="store_true", help="run one pass and exit")
    parser.add_argument("--backfill-minutes", type=int, default=None,
                        help="with --once: also react to ledger events from the last N minutes")
    parser.add_argument("--poll", type=float, default=None, help=f"seconds between passes (default {config.WATCHER_POLL_SECONDS})")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
        stream=sys.stdout,
    )

    if args.once:
        database.create_all()
        since = None
        if args.backfill_minutes:
            since = datetime.now(timezone.utc) - timedelta(minutes=args.backfill_minutes)
        with database.session_scope() as session:
            made = service.run_once(session, react_since=since)
        for s in made:
            print(f"[{s.kind}] ({s.phrased_by}) {s.text}")
        print(f"{len(made)} suggestion(s) created")
        return 0

    service.run_forever(args.poll)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
