"""Back up and restore the SQLite database (Phase 8 item 6).

Uses SQLite's own online-backup API, NOT a file copy. That distinction is the
whole point of this script: copying a live SQLite file with `cp` can capture a
torn page mid-write and produce a backup that opens fine and is quietly
corrupt. Under WAL (which this app enables) a plain copy is worse still,
because the recent commits live in a separate `-wal` file the copy misses.
`Connection.backup()` takes a transactionally consistent snapshot of a
database that is being written to.

    python -m scripts.backup_db backup                  # -> backups/campuspool_YYYYmmdd-HHMMSS.db
    python -m scripts.backup_db list
    python -m scripts.backup_db verify backups/x.db
    python -m scripts.backup_db restore backups/x.db    # asks first; keeps a pre-restore copy
    python -m scripts.backup_db rehearse                # backup -> verify -> restore into a temp file

REHEARSE THE RESTORE. An unrehearsed restore procedure is a hope, not a plan,
so `rehearse` runs the whole loop end to end and checks the row counts match.
It never touches the live database.

Worth saying plainly: for THIS prototype, "reproducible from seed in seconds"
(`python -m backend.seed.demo_data --reset`) is a stronger guarantee than any
backup, because every row is synthetic. This script exists because the habit
is what transfers to a system holding real financial history, and because the
Definition of Done asks for a rehearsed restore rather than an asserted one.
"""

from __future__ import annotations

import argparse
import shutil
import sqlite3
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from backend import config

BACKUP_DIR = config.BASE_DIR / "backups"

#: Tables whose row counts a verify compares. Chosen because they are the ones
#: whose loss would actually matter.
CORE_TABLES = ("users", "ledger_events", "action_intents", "audit_events",
               "goals", "spend_policies", "purchase_rules")


def _db_path() -> Path:
    url = config.DATABASE_URL
    if not url.startswith("sqlite:///"):
        sys.exit(f"This script only handles SQLite; DATABASE_URL is {url!r}")
    return Path(url[len("sqlite:///"):]).expanduser()


def _counts(path: Path) -> dict[str, Any]:
    """Row counts for display. Never raises: this is called to DESCRIBE a file,
    including a file that turns out to be unreadable, and a crash here would
    hide the very problem the caller is trying to report."""
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    except sqlite3.Error as exc:
        return {"error": f"{type(exc).__name__}: {exc}"}
    try:
        out: dict[str, Any] = {}
        existing = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        for table in CORE_TABLES:
            if table in existing:
                out[table] = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]  # noqa: S608
        return out
    except sqlite3.Error as exc:
        return {"error": f"{type(exc).__name__}: {exc}"}
    finally:
        conn.close()


def backup(*, quiet: bool = False) -> Path:
    src = _db_path()
    if not src.exists():
        sys.exit(f"No database at {src}. Nothing to back up.")
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    dest = BACKUP_DIR / f"campuspool_{stamp}.db"

    source = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
    target = sqlite3.connect(dest)
    try:
        # The online backup API: safe against concurrent writers, and it folds
        # the WAL into the snapshot so the result is a single complete file.
        source.backup(target)
    finally:
        target.close()
        source.close()

    if not quiet:
        print(f"backed up {src.name} -> {dest}")
        print(f"  {dest.stat().st_size / 1024:,.0f} KiB  ·  {_counts(dest)}")
    return dest


def verify(path: Path, *, quiet: bool = False) -> bool:
    """Open the backup read-only, run SQLite's integrity check, and confirm the
    audit hash chain still verifies. A backup that opens is not the same as a
    backup that is INTACT, and for this app the chain is the strongest
    available statement that the history inside it was not mangled."""
    if not path.exists():
        sys.exit(f"no such backup: {path}")
    # A file that is not a database at all raises rather than returning a
    # non-"ok" integrity result. A verify() that crashes on exactly the input
    # it exists to detect is broken, so the failure is caught and REPORTED.
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
        finally:
            conn.close()
    except sqlite3.Error as exc:
        integrity = f"not a readable SQLite database ({type(exc).__name__}: {exc})"

    chain_ok, chain_note = _verify_chain(path)
    ok = integrity == "ok" and chain_ok
    if not quiet:
        print(f"verify {path.name}")
        print(f"  integrity_check : {integrity}")
        print(f"  audit chain     : {chain_note}")
        print(f"  row counts      : {_counts(path)}")
        print(f"  => {'INTACT' if ok else 'PROBLEM'}")
    return ok


def _verify_chain(path: Path) -> tuple[bool, str]:
    """Reuse the app's own chain verification against the backup file, so the
    check here can never drift from the check the API reports."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    engine = create_engine(f"sqlite:///{path}", future=True)
    try:
        from backend.services import audit_service
        with sessionmaker(bind=engine, future=True)() as session:
            result = audit_service.verify_chain(session)
        return result.ok, ("intact, %d entries" % result.checked) if result.ok else f"BROKEN at seq {result.broken_at_seq}: {result.reason}"
    except Exception as exc:  # noqa: BLE001
        return False, f"could not verify: {type(exc).__name__}: {exc}"
    finally:
        engine.dispose()


def restore(path: Path, *, assume_yes: bool = False) -> None:
    dest = _db_path()
    if not verify(path, quiet=True):
        sys.exit(f"refusing to restore from a backup that does not verify: {path}\n"
                 f"run: python -m scripts.backup_db verify {path}")
    print(f"restore {path.name}  ->  {dest}")
    print(f"  backup has  : {_counts(path)}")
    if dest.exists():
        print(f"  live has    : {_counts(dest)}")
    if not assume_yes:
        if input("  this REPLACES the live database. type 'yes' to continue: ").strip() != "yes":
            sys.exit("aborted")

    if dest.exists():
        # Never destroy the thing being replaced: if the backup turns out to be
        # the wrong one, the state before the restore is still on disk.
        safety = dest.with_suffix(dest.suffix + f".pre-restore-{datetime.now(timezone.utc):%Y%m%d-%H%M%S}")
        shutil.copy2(dest, safety)
        print(f"  kept a pre-restore copy at {safety.name}")
        # WAL sidecars belong to the OLD database; leaving them beside a
        # restored file is how a "successful" restore comes back with the
        # wrong data.
        for sidecar in (dest.with_suffix(dest.suffix + "-wal"), dest.with_suffix(dest.suffix + "-shm")):
            sidecar.unlink(missing_ok=True)
        # Remove the destination rather than writing over it. sqlite3.connect()
        # on a CORRUPT file raises "file is not a database", so backing up into
        # it fails - and a corrupt live database is exactly the situation a
        # restore exists for. The pre-restore copy above is what makes deleting
        # it safe. Found by test_backup_verify_and_restore_round_trip.
        dest.unlink()

    dest.parent.mkdir(parents=True, exist_ok=True)
    source = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    target = sqlite3.connect(dest)
    try:
        source.backup(target)
    finally:
        target.close()
        source.close()
    print(f"  restored. now has: {_counts(dest)}")


def rehearse() -> int:
    """The whole loop, against a COPY. Proves the procedure works without
    betting the live database on it."""
    print("REHEARSAL — the live database is never written to.\n")
    src = _db_path()
    if not src.exists():
        sys.exit(f"no database at {src}; run: python -m backend.seed.demo_data")

    print("1. back up")
    snapshot = backup()
    print("\n2. verify the backup")
    if not verify(snapshot):
        return 1

    print("\n3. restore into a throwaway location and compare")
    with tempfile.TemporaryDirectory() as tmp:
        fake_live = Path(tmp) / "restored.db"
        source = sqlite3.connect(f"file:{snapshot}?mode=ro", uri=True)
        target = sqlite3.connect(fake_live)
        try:
            source.backup(target)
        finally:
            target.close(); source.close()
        before, after = _counts(snapshot), _counts(fake_live)
        print(f"  backup   : {before}")
        print(f"  restored : {after}")
        if before != after:
            print("\n  FAIL: the restored database does not match the backup.")
            return 1
        if not _verify_chain(fake_live)[0]:
            print("\n  FAIL: the restored database's audit chain does not verify.")
            return 1

    print("\nREHEARSAL PASSED — backup, verify and restore all work, and the")
    print("restored copy's audit chain still verifies.")
    print(f"\nFor a real restore:  python -m scripts.backup_db restore {snapshot}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("backup")
    sub.add_parser("list")
    sub.add_parser("rehearse")
    v = sub.add_parser("verify"); v.add_argument("path", type=Path)
    r = sub.add_parser("restore"); r.add_argument("path", type=Path); r.add_argument("--yes", action="store_true")
    args = ap.parse_args()

    if args.cmd == "backup":
        backup(); return 0
    if args.cmd == "list":
        found = sorted(BACKUP_DIR.glob("campuspool_*.db")) if BACKUP_DIR.exists() else []
        if not found:
            print(f"no backups in {BACKUP_DIR}"); return 0
        for f in found:
            print(f"  {f.name:<34} {f.stat().st_size / 1024:>8,.0f} KiB")
        return 0
    if args.cmd == "verify":
        return 0 if verify(args.path) else 1
    if args.cmd == "restore":
        restore(args.path, assume_yes=args.yes); return 0
    if args.cmd == "rehearse":
        return rehearse()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
