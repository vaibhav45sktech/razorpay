# Passive watcher — background suggestions from the ledger

A small background process that watches the user's ledger and leaves short,
advisory suggestions ("you've used 60% of your budget by the 10th", "that
₹300 took your goal past 50%"). It runs independently of the API and of any
terminal, uses a deliberately small model only to phrase sentences, and can
never act on anything.

## Start / stop (Windows)

```powershell
.\scripts\start_watcher.ps1          # one command; hidden, detached, survives closing the terminal
.\scripts\watcher_status.ps1         # running? + last log lines
.\scripts\stop_watcher.ps1
.\scripts\install_watcher_autostart.ps1   # optional: start at every login (Task Scheduler)
```

First start pulls `qwen2.5:1.5b-instruct` (~1 GB) once. Ollama itself already
runs as a Windows background service, so nothing else needs to stay open.
Logs: `logs\watcher.log`. Override with `WATCHER_MODEL`, `WATCHER_POLL_SECONDS`
(default 15), `WATCHER_COOLDOWN_HOURS` (default 24) in `.env` or the shell.

Debug in the foreground: `python -m backend.watcher --once [--backfill-minutes 60]`.

## What it says, and why it is safe to leave running

| Layer | Who decides | Can it be wrong in a way that matters? |
|---|---|---|
| **Detection** (`backend/watcher/rules.py`) | Deterministic code over `state_service.get_state()` — the same numbers the UI shows | Only if the ledger is wrong |
| **Phrasing** (`backend/watcher/phrasing.py`) | Small model rewrites a code-written template; output is rejected if any ₹ figure or % changed, or if the model is unreachable → template is used | No: numbers are verified against the template |
| **Storage** (`suggestions` table) | Text + the facts it came from + `phrased_by` (`llm:<model>` or `template`) | It is data. Nothing reads it to decide anything |
| **Surface** | `GET /api/suggestions/{user_id}`, `POST /api/suggestions/{id}/dismiss`, top 3 in `GET /api/state`, two lines in the chat agent's state summary (marked advisory) | No route or tool can act on a suggestion |

Every suggestion writes one `AuditEvent` (`actor=system`, `action=suggestion:<kind>`),
so the trail shows what the watcher said and from which facts, without the model.

## Rules (initial set)

| kind | fires when | dedup |
|---|---|---|
| `spend_pace` | ≥40% of monthly limit used and ≥25% ahead of the calendar | once per month |
| `large_purchase` | a single purchase ≥25% of the monthly limit | per event |
| `goal_milestone` | a contribution crosses 25/50/75/100% of a goal | per goal + milestone |
| `savings_nudge` | no contribution in 14 days while a goal is open | per ISO week, 24h cooldown |
| `pending_approval` | an intent has awaited approval for 10+ minutes | per intent |
| `offer_match` | a purchase whose source carries a category (`purchase:<category>:…`) has an eligible partner offer | per event + offer |

Restart-safe: the "which events have I already reacted to" mark is derived from
the database each pass (newest event that already has a suggestion); a cold
start reacts only to events from now on, never to seeded history.
