# CampusPool MVP — Senior-Engineer Execution Playbook

> **SUPERSEDED.** This document has been consolidated into `CampusPool_Master_Build_Plan.md`, which is now the single operational plan. This file is kept for its rationale and history.

**Companion to:** `CampusPool_Agent_HLD_LLD.md` (architecture), `Building_Your_First_AI_Agent.md` (concepts), `CampusPool_Build_Plan.md` (phase overview).
**What this document adds:** the other two tell you *what* to build and *why*. This one tells you the literal, numbered, command-level *how* — including the professional habits (git discipline, test-first, definition of done, commit hygiene) that separate "code that happened to work once" from an MVP you can confidently demo and extend. Follow it top to bottom; don't skip Part A even though it has no project code in it — it's the part that makes everything after it go smoothly.

---

## Part A — How a senior engineer actually works (read once, apply every day)

You don't need years of experience to work this way — you need a small number of habits, applied consistently. Here they are, because the rest of this document assumes you're using them.

### A.1 Git discipline: small steps, always revertible

A senior engineer never has "three days of uncommitted work" they're afraid to touch. The habit: **one branch per phase, one commit per working checkpoint, never commit broken code to `main`.**

```bash
git init
git branch -M main
git add .gitignore README.md
git commit -m "chore: initial repo skeleton"

# for every phase (or sub-step) below:
git checkout -b phase-1-data-layer
# ... do the work, in small commits ...
git add backend/models/entities.py
git commit -m "feat: add SQLAlchemy models for User, Goal, LedgerEvent"
# ... more small commits as you go ...
git checkout main
git merge phase-1-data-layer --no-ff
git tag v0.1-data-layer
```

Rule of thumb for commit size: if you can't describe the commit in one short sentence starting with a verb ("add", "fix", "wire"), it's probably two commits. This isn't bureaucracy — it's what lets you `git log` your way back to exactly the last point everything worked, the moment something breaks three phases later.

**`.gitignore` — set this up in commit #1, not later:**

```
.venv/
__pycache__/
*.pyc
.env
*.db
node_modules/
dist/
.DS_Store
```

If `.env` (with your real Razorpay keys) ever gets committed, treat it as compromised: rotate the keys in the dashboard immediately, even after removing it from git — history doesn't forget on its own.

### A.2 Definition of Done — the same checklist, every phase

A senior engineer doesn't consider a phase "done" because the happy path worked once in a terminal. Done means:

- [ ] The relevant tests exist and pass (`pytest` for backend/agent phases)
- [ ] You tried at least one failure case on purpose (bad input, missing data, denied policy) and it failed *gracefully*, not with a raw stack trace
- [ ] No secrets, no `rzp_live` keys, no `.env` in the diff
- [ ] Function and class names say what they do; no `data2`, `temp`, `foo`
- [ ] You re-read your own diff (`git diff --staged`) before committing, like reviewing a coworker's PR
- [ ] A short note added to `README.md` or a `CHANGELOG.md` saying what now works
- [ ] Commit made, branch merged, tag created

Print this list, or paste it into your PR/commit template. Apply it at the end of *every* phase in Part B without exception — this is the difference between "hackathon code that's secretly held together with hope" and an MVP you can actually stand behind in front of judges.

### A.3 Test-first where it's cheap, test-alongside everywhere else

You don't need strict TDD discipline everywhere, but for **anything involving money math or policy decisions**, write the test before — or immediately after, same sitting — the function, never "later." These are the functions where a silent bug is expensive to discover late. For UI glue or read-only display code, testing alongside (write the function, immediately write one test proving the obvious case and one proving an edge case) is enough.

The concrete cadence for a function like `check_policy`:
1. Write the test cases as a table first (see LLD §5.2) — this forces you to think through edge cases before you're biased by your own implementation.
2. Write the simplest implementation that passes them.
3. Run `pytest -x` (stop on first failure) after every small change — never write more than ~20 lines without running the tests again.

### A.4 Errors are handled, never silently swallowed

A senior engineer's code never has a bare `except: pass`. Every error either (a) is expected and handled with a clear fallback ("payment status unknown → open an exception record, don't guess"), or (b) is unexpected and allowed to surface loudly (so you see it in logs/tests, rather than it vanishing and corrupting state quietly). Silent failure is the single most expensive class of bug to debug later, because by the time you notice something's wrong, you don't know when it started.

### A.5 Config and secrets are never hardcoded

Every value that differs between your laptop and anyone else's (API keys, database URL, model name) lives in `.env`, loaded once in `config.py`, imported everywhere else. Nothing reads `os.environ` directly outside that one file — this makes it trivial to see your entire configuration surface in one place, and trivial to swap environments later.

### A.6 Logging over guessing

Add real logging early, not "when something breaks." At minimum: every tool call the agent makes, every policy decision, every state transition, every webhook received — one line each, with enough context to reconstruct what happened without re-running anything. You already have `AuditEvent` in the design for exactly this — treat populating it as part of "done," not an optional extra.

```python
import logging
logger = logging.getLogger("campuspool")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
```

`print()` is fine for a 2-minute debugging session; anything that stays in the codebase past that session should be a real `logger.info(...)` call.

### A.7 Read your own diff before every commit

This single habit catches more bugs than any other item on this list, and it costs 30 seconds: `git diff --staged` before every `git commit`, read top to bottom, ask "would I approve this if a coworker sent it to me?" If the answer is no, fix it before committing, not after.

---

## Part B — The literal, step-by-step build process

Each phase below follows the same shape: **Branch → Steps (numbered, in order) → Test → Definition of Done → Commit & tag**. This *is* the phase plan from `CampusPool_Build_Plan.md`, now expanded to command-level granularity with the engineering discipline from Part A woven into every step instead of bolted on at the end.

---

### Phase 0 — Repo, environment, and proof the core mechanism works

**Branch:** `phase-0-setup`

**Steps:**

1. Create the repo, `.gitignore`, initial commit (A.1).
2. Create the folder skeleton (empty `__init__.py` files are fine as placeholders):
   ```bash
   mkdir -p backend/{agent,tools,services,models,seed,benchmark,tests}
   touch backend/__init__.py backend/agent/__init__.py backend/tools/__init__.py \
         backend/services/__init__.py backend/models/__init__.py
   ```
3. `python -m venv .venv && source .venv/bin/activate`
4. `pip install fastapi "uvicorn[standard]" sqlalchemy pydantic httpx razorpay python-dotenv pytest pyyaml`
5. `pip freeze > requirements.txt` — commit this file. A senior engineer never asks a teammate (or their future self) to guess which package versions were used.
6. Write `backend/config.py`: load `.env` via `python-dotenv`, expose typed settings, and **add the `rzp_test_` guard now**, even with no Razorpay code yet — it costs nothing today and you will not remember to add it later under deadline pressure.
7. Write `backend/main.py`: a FastAPI app with `GET /health` returning `{"status": "ok"}`.
8. Run it: `uvicorn backend.main:app --reload --port 8000`, confirm `curl localhost:8000/health` works.
9. Install Ollama, `ollama pull qwen2.5:7b-instruct`, confirm `ollama run qwen2.5:7b-instruct "hi"` responds.
10. Run the standalone tool-calling proof script from `Building_Your_First_AI_Agent.md` Chapter 2 as a throwaway file (`scratch/prove_tool_calling.py`, not part of the app). Confirm you see a `tool_calls` response. **Do not proceed to Phase 1 until you've personally seen this work on your machine** — it's the one dependency in the whole project with no fallback if it fails, so fail fast on it now, not on day 6.

**Test:** none yet beyond the manual checks above — there's no logic to unit test in Phase 0.

**Definition of Done:** Part A.2 checklist, plus: `/health` responds, Ollama tool-calling proof works, `requirements.txt` committed.

**Commit & tag:**
```bash
git add . && git commit -m "chore: repo skeleton, FastAPI health check, Ollama tool-calling verified"
git checkout main && git merge phase-0-setup --no-ff && git tag v0.0-skeleton
```

---

### Phase 1 — Data layer

**Branch:** `phase-1-data-layer`

**Steps:**

1. Write `backend/models/entities.py` — every table from LLD §2.2 (`User, Goal, LedgerEvent, PoolCycle, PoolAllocation, Reward, Offer, SpendPolicy, ActionIntent, Approval, AuditEvent, ExceptionRecord`). Copy field lists from the LLD; don't improvise columns under time pressure — an undefined field you invent now becomes a "why does this exist" question in code review later.
2. Add a `Base.metadata.create_all()` bootstrap in a `backend/models/db.py` (engine + session factory), pointed at `DATABASE_URL` from config.
3. Write `backend/services/ledger_service.py` with exactly two public functions to start: `append(event)` and `get_balance(user_id, bucket)`. **Deliberately do not add an `update_balance` or `edit_event` function** — the append-only guarantee should be true because the capability to violate it doesn't exist in the code, not because you're being careful.
4. **Write the test before you trust the implementation** (A.3): `backend/tests/test_ledger.py` —
   ```python
   def test_balance_is_derived_from_ledger(db_session):
       ledger_service.append(db_session, user_id="u1", bucket="emergency_savings", amount_paise=50000, ...)
       ledger_service.append(db_session, user_id="u1", bucket="emergency_savings", amount_paise=-10000, ...)
       assert ledger_service.get_balance(db_session, "u1", "emergency_savings") == 40000
   ```
5. Write `backend/seed/demo_data.py` — 1–2 users, a goal each, an opening `emergency_savings` ledger event, a `SpendPolicy` row per user, 4–6 `Offer` rows tagged `is_synthetic=True`, one `PoolCycle`. Make it **idempotent**: running it twice should not duplicate rows (check-then-insert, or clear-then-seed in dev). Test this explicitly — it's a common source of "why do I have 40 duplicate offers" confusion three days from now.
6. `pytest backend/tests/test_ledger.py -v` — all green before moving on.

**Definition of Done:** A.2 checklist, plus: seed script runs twice without duplicating data, `pytest` green, a one-line README note: "data layer + seed data working."

**Commit & tag:** `git tag v0.2-data-layer` after merge.

---

### Phase 2 — Policy engine (deterministic, no LLM, no API)

**Branch:** `phase-2-policy-engine`

This is the phase to treat with the most care — it's the actual safety guarantee of the whole product, and it's pure functions, which means it's the easiest phase to get to 100% test confidence on. A senior engineer would not "move fast" here; this is where you go slow and thorough on purpose.

**Steps:**

1. Write `backend/policy_config.yaml` (copy from LLD §2.4).
2. **Write the test table first** (A.3 step 1) — literally type out the CASES table from LLD §5.2 as a pytest parametrize block *before* writing `check_policy` itself. This forces you to enumerate ALLOW/DENY/REQUIRE_APPROVAL boundaries deliberately instead of discovering them by accident while coding.
3. Implement `backend/services/policy_engine.py::check_policy()` to make the tests pass, following the structure in LLD §2.4: default-deny for unknown actions, protected-bucket check, monthly limit check (including *pending/committed* spend, not just settled spend), approval threshold check, contribution min/max band check, paused-user check.
4. Implement `money_action_service.committed_pending(user_id)` — a helper `check_policy` needs, summing amounts on intents that exist but haven't settled yet. Test it in isolation too.
5. Add the extra edge-case tests beyond the base table: paused user, emergency-bucket purchase, spend that's fine alone but pushes over the limit once pending amounts are included.
6. Run `pytest backend/tests/test_policy.py -v --tb=short` until every case is green. Don't move to Phase 3 with a single red test, even one that "shouldn't matter" — in a policy engine, "shouldn't matter" is exactly the kind of assumption that becomes a real incident.

**Definition of Done:** A.2, plus: every row of the CASES table passes, plus the extra edge cases; you can explain out loud what each test proves (if you can't, the test isn't specific enough).

**Commit & tag:** `git tag v0.3-policy-engine`.

---

### Phase 3 — Money state machine, fake executor

**Branch:** `phase-3-state-machine`

**Steps:**

1. Implement the `LEGAL` transition table and `transition()` function in `money_action_service.py` (LLD §2.5). Write a test proving an illegal transition **raises**, not silently no-ops (A.4 — errors surface loudly).
2. Implement `tools/payment_tools.py::create_payment_intent()` with the `client_ref` idempotency hash. Test: calling it twice with identical inputs returns the *same* intent, not a new one.
3. Add a debug-only route `POST /debug/intents/{id}/fake-settle`, gated behind `if not settings.DEBUG: raise HTTPException(404)`. This is temporary scaffolding, not production code — comment it clearly as such, and put a reminder in your Phase 5 checklist to confirm it's unreachable once real Razorpay is wired.
4. Wire the fake-settle route to call the *real* `settle_success()` logic (ledger append, goal update, reward recompute) — this is the piece that'll be reused unchanged by the real webhook in Phase 5, so building it correctly now saves you rework later.
5. Integration test: create intent → policy check → fake-settle → assert ledger balance and goal state updated correctly. This is your first test that spans multiple services — a good moment to also manually `curl` the same flow and watch it work outside of pytest, since a full integration test can pass while something about the actual HTTP flow is still broken.

**Definition of Done:** A.2, plus: full curl-able flow from intent creation to settled balance with zero LLM or Razorpay involvement; illegal-transition test passes; duplicate-intent test passes.

**Commit & tag:** `git tag v0.4-state-machine`.

---

### Phase 4 — The Agent (most detailed phase — budget the most care here)

**Branch:** `phase-4-agent` (consider sub-branches per LLD sub-step if you want finer-grained rollback points: `phase-4.1-tool-registry`, etc. — a senior engineer scales branch granularity to risk, and this phase has the most risk in the project.)

If you haven't done the hands-on exercises in `Building_Your_First_AI_Agent.md` Chapters 2–3 yet, stop and do them first — this phase assumes that muscle memory.

**Steps:**

1. **Schemas before handlers.** Write every tool's Pydantic input/output model in `backend/models/schemas.py`, straight from the LLD §2.3 table, before writing a single handler. This nails down the contract while it's cheap to change, and means handler code and prompt-writing can proceed independently once schemas are frozen.
2. Write `backend/agent/tool_registry.py`: the `Caller` enum and `TOOLS` dict, plus `llm_visible_tools()`.
3. **Test the registry in isolation, immediately**: call `llm_visible_tools()`, print the JSON, and assert (in an actual test, not just eyeballing) that backend-only tool names (`create_razorpay_payment`, `process_test_payout`, `get_payment_status`) are **absent**. This is a one-line test (`assert "create_razorpay_payment" not in [t["function"]["name"] for t in tools]`) that encodes one of your most important safety claims — write it now while it's fresh, not as an afterthought.
4. Implement the read-only tool handlers in this order (each is independently testable with zero LLM involved, since they're plain DB reads): `get_user_profile` → `get_wallet_or_ledger` / `get_transactions` → `get_pool_status` → `get_offers` / `get_eligible_rewards` → `calculate_safe_contribution`. Unit test each one against seeded data before moving to the next.
5. Write `backend/agent/llm_client.py` (LLD §2.6). Test it standalone with the manual script from the book's Chapter 3 before wiring it into anything — confirm the model reliably requests `get_wallet_or_ledger`-shaped calls for balance questions. If it doesn't, **stop and fix tool descriptions now** — every phase after this compounds on a shaky foundation here.
6. Write `backend/agent/prompts.py` with the system prompt from LLD §2.7.
7. Implement `backend/agent/orchestrator.py::run_agent_turn()` and `execute_tool()` (LLD §2.8), wired to the read-only tools + `check_policy` only (not `create_payment_intent` yet — one capability at a time).
8. **Write the `ScriptedLLM` test harness now** (`Building_Your_First_AI_Agent.md` Ch. 8, Layer 2) and prove, with a fake model, that: an unknown tool name is refused gracefully; the step budget terminates and reports honestly rather than hanging. These are cheap, instant, deterministic tests — get them green before layering on real-model tests.
9. Manual test against the real local model: ask 3–4 realistic questions ("what's my goal progress," "can I spend ₹2000 on shoes"), and for each one, print and read the full message transcript (A.6/A.7 habit) to confirm real data was used and the reasoning is sound — not just that the final reply "sounds right."
10. Add `create_payment_intent` to `llm_visible_tools()` and `MONEY_TOOLS`. Add the **structural re-check**: `execute_tool()` re-runs `policy_engine.check_policy()` itself before this tool executes, regardless of the model's own prior tool calls. Write the `ScriptedLLM` test proving a money tool call with no preceding real `check_policy` ALLOW is still blocked correctly (LLD §5.3).
11. Run the full adversarial manual test list from LLD §5.5 against the real model, and log the outcome of each one in your README/notes — this becomes evidence for your demo, not just a private sanity check.
12. Wire `POST /api/chat` to `run_agent_turn()`. End-to-end test: chat → tool calls → intent created → fake-settle (Phase 3's debug route) → next chat turn reflects the real updated balance, never an invented one.
13. Implement `POST /api/intents/{id}/approve` (LLD §4.4) as a plain structured endpoint, deliberately **not** an agent tool. Test that a `REQUIRE_APPROVAL` intent only proceeds after this endpoint is called — chat text alone, no matter how it's phrased, must never move it forward.

**Definition of Done (this phase specifically — hold it to a higher bar than others):** all `ScriptedLLM` tests green; all read-only tool unit tests green; the "backend-only tools are invisible" test green; every adversarial manual test behaves correctly and is logged; audit log shows a row for every tool call including blocked ones; you can narrate, from the audit log alone, an entire conversation's decisions truthfully.

**Commit & tag:** commit after each numbered step that reaches a green test (small commits, per A.1), then `git tag v0.5-agent` once the whole phase's Definition of Done is met.

---

### Phase 5 — Real Razorpay Test Mode

**Branch:** `phase-5-razorpay`

**Steps:**

1. Dashboard: Test Mode, generate `key_id`/`key_secret`, into `.env`, confirm `config.py`'s `rzp_test_` guard actually rejects a fake `rzp_live_` value (test this on purpose — it's a one-line change to verify a safety net you built in Phase 0 still works).
2. Implement `services/razorpay_adapter.py` (`create_order`, `verify_checkout_signature`, `fetch_payment`) — the **only** file in the codebase importing the `razorpay` package. Enforce this with a quick `grep -r "import razorpay" backend/` check before committing — should return exactly one file.
3. Implement `POST /api/intents/{id}/execute`, replacing the Phase 3 debug trigger's role (the debug route itself should now be effectively dead — confirm it 404s with `DEBUG=false`).
4. Build a minimal test HTML page with Checkout.js, wired to `POST /api/checkout/verify`.
5. `ngrok http 8000`, register the webhook URL + secret, implement `webhook_service.py` with signature validation and event-id dedupe (LLD §6.5).
6. Run every scenario in the LLD §6.6 test matrix, one at a time, and check off each as it passes — resist the urge to eyeball "it probably works," since payment integrations are exactly where subtle timing bugs (duplicate webhooks, delayed webhooks) hide.
7. Implement the reconciliation background job for stuck `EXECUTING` intents.
8. (Cut-if-short-on-time) RazorpayX test payout for the pool demo.

**Definition of Done:** A.2, plus: every row of the LLD §6.6 test matrix passes and is checked off; `grep` confirms only one file imports the SDK; reconciliation job proven with a manually-delayed webhook.

**Commit & tag:** `git tag v0.6-razorpay`.

---

### Phase 6 — Frontend

**Branch:** `phase-6-frontend`

**Steps:**

1. Scaffold React+Vite (or plain HTML if time-constrained) pointed at your now-stable API.
2. Build screens in order of how central they are to the demo: chat panel → state panel → approval card → checkout trigger → audit trail view → exception queue view.
3. Enforce "no client-side math" as a code-review rule on yourself — every number on screen is exactly what an API response said, no re-derivation in JS.
4. Manual end-to-end test: a friend (or you, pretending to be a first-time user) uses the UI with zero narration from you, start to finish, hitting at least one denial and one approval flow.

**Definition of Done:** A.2, plus: the "stranger test" in step 4 passes without you explaining anything.

**Commit & tag:** `git tag v0.7-frontend`.

---

### Phase 7 — Benchmark, hardening, demo rehearsal

**Branch:** `phase-7-benchmark`

**Steps:**

1. Write `benchmark/scenarios.yaml` in batches as you finish earlier phases (don't leave all 100 for the last day — write ~15–20 per phase while that phase's behavior is fresh in your mind).
2. Write `benchmark/run_benchmark.py`, asserting against the **database**, never the chat text (A.4 — don't trust a fuzzy "looks right").
3. Run it, read every failure, fix the actual bug (not the test) unless the test itself was wrong.
4. Final security/hygiene pass: `grep -r "rzp_live" .`, confirm `.env` was never committed (`git log --all --full-history -- .env` should be empty), confirm every synthetic data point is labeled as such in UI copy.
5. Rehearse the 5-minute demo script (LLD Part 7) twice, on the actual demo machine and network, timing yourself.
6. Update `README.md` with: what the project is, how to run it locally, how to run the tests, and the benchmark metrics table as of this commit — a senior engineer leaves a repo in a state a stranger could pick up and run.

**Definition of Done:** A.2, plus: benchmark metrics meet the PRD §6.1 targets (or you know exactly which ones don't and why, and can say so honestly rather than hiding it); demo rehearsed twice successfully; README complete.

**Commit & tag:** `git tag v1.0-mvp`.

---

---

### Phase 8 — Hardening

**Branch:** `phase-8-hardening`

Added after the production-readiness review. The 15 numbered steps are in `CampusPool_Build_Plan.md` (Phase 8); the reasoning, the triage of what was deliberately deferred, and the compliance corrections are in `CampusPool_Production_Readiness.md`.

Same Definition of Done as every other phase (A.2), with two additions specific to this phase:

- [ ] `verify_chain()` demonstrated live: tamper with a row via raw SQL and watch the system name the forged entry
- [ ] The degradation matrix is not just written but *tested* — kill each dependency in turn and confirm the documented behaviour is what actually happens

**Commit & tag:** `git tag v0.8-hardening`.

## Part C — The one-page version, pinned to your desk

1. Branch per phase. Small commits. Never commit broken code to `main`.
2. Tests for policy/money logic come first or alongside — never "later."
3. Every phase ends with the same Definition of Done checklist (A.2) — no exceptions, no "just this once."
4. Read your own diff before every commit.
5. Errors are handled or surfaced loudly — never silently swallowed.
6. Secrets live only in `.env`, never in code, never in a commit.
7. When something misbehaves, print and read the actual data (message transcript, DB state, logs) before guessing.
8. "Done" means tested, committed, tagged, and documented — not "worked once in my terminal."
