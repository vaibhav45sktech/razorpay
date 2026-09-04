"""`python -m backend.reconcile sweep|full|integrity` - run a reconciliation
job by hand (the sweeper also runs inside the API process on a timer)."""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime, timedelta, timezone

from backend.models import db as database
from backend.services import reconciliation_service as rs


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("job", choices=["sweep", "full", "integrity"])
    parser.add_argument("--days", type=int, default=1, help="for full: period length ending now")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-8s %(name)s: %(message)s", stream=sys.stdout)

    database.create_all()
    with database.session_scope() as session:
        if args.job == "sweep":
            out = rs.sweep_stuck_intents(session).as_dict()
        elif args.job == "full":
            out = rs.full_reconciliation(session, since=datetime.now(timezone.utc) - timedelta(days=args.days)).as_dict()
        else:
            out = rs.ledger_integrity(session).as_dict()
    print(json.dumps(out, indent=2, default=str))
    return 0 if out.get("ok", True) else 1


if __name__ == "__main__":
    raise SystemExit(main())
