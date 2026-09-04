"""Passive watcher — background, advisory suggestions from the ledger.

    python -m backend.watcher            # run the loop (scripts/start_watcher.ps1 wraps this)
    python -m backend.watcher --once     # one pass, then exit (used by tests and for debugging)

Design in one paragraph: `rules.py` looks at verified state and decides,
deterministically, whether there is something worth saying (and what the
facts are). `phrasing.py` turns those facts into one friendly sentence with a
small model, or a template when the model is unavailable. `service.py` runs
that on a timer, dedups with a per-kind cooldown, stores rows in the
`suggestions` table and writes one audit entry each. Nothing here can create
an intent, call a tool, or touch the ledger — a suggestion is text plus the
facts it came from, and that is all it will ever be.
"""
