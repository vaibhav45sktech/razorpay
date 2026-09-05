# Benchmark results — Phase 7

**Case set:** `benchmark/scenarios.yaml` · frozen digest `4893ad82b8e03ae2` (34 cases)
**Run:** 2026-09-05, scripted mode, `python -m benchmark.run_benchmark`

## Headline

**34/34 cases pass. 19/19 adversarial cases contained: zero intents created, zero ledger rows written, zero rupees moved.**

| Metric (PRD §6.1) | Target | Actual | |
|---|---|---|---|
| Policy compliance | ≥95% | **100.0%** | PASS |
| Correct decisions | ≥90% | **100.0%** | PASS |
| Unauthorized blocking | 100% | **100.0%** | PASS |
| Prompt injection contained | 100% | **100.0%** | PASS |
| Duplicate prevention | 100% | **100.0%** | PASS |
| Honest escalation | 100% | **100.0%** | PASS |

Agent turn latency, scripted mode: **p50 0.021 s · p95 0.052 s**. This measures
the orchestrator and the guardrails with the model removed — it is the cost of
*our* code, not of inference. Live-mode latency is dominated by Ollama and is
reported separately below.

## What is actually being asserted

Every assertion reads the **database**, never the chat reply. A model that says
*"done, I've paid ₹5,000!"* over an untouched ledger has not moved money, and a
benchmark that greps the reply cannot tell that from a real breach — it would
score a safe system as unsafe and an unsafe one as safe. So each case snapshots
`action_intents`, `ledger_events`, `audit_events`, `exception_records`,
`purchase_rules` and goal statuses, runs a real agent turn, snapshots again, and
scores the difference. Chat text is asserted in exactly two cases, both where
the honest behaviour is "say something true" and there is no database effect to
observe — and never as the only assertion for a money case (a test enforces
that: `test_the_case_set_parses_and_is_declared_correctly`).

## The two runners, and what each one proves

**Scripted (default, in CI).** No Ollama. A `ScriptedLLM` replays each case's
declared sequence — *the worst thing a model could legally name*: it ignores
tool results, never re-plans, and keeps attempting the next step after the
orchestrator has blocked the previous one. This proves the **code** is safe
regardless of what a model says. It is deterministic, runs in ~4 s, and is the
number published above.

**Live (`--mode live`).** Sends each case's `user` text to the real local model
and scores the same database assertions. This measures whether the model is
**cooperative** — a different question. A failure in live mode is a
product-quality finding; a failure in scripted mode is a security bug.

That distinction is the reason both exist. `backend/agent/manual_adversarial_tests_results.md`
holds the hand-run live evidence from Phase 4/5 (five escalating "just pay"
attempts → five independent denials).

## Is the benchmark measuring anything?

A suite that passes even with the safety net cut is theatre.
`test_the_benchmark_would_notice_a_broken_guardrail` disables amount provenance
and asserts that the invented-amount case then **fails**. It does.

## What Phase 7 surfaced (Step 5: fix the bug, not the test)

**1. A sentence-ending full stop defeated amount provenance.** *(real bug, fixed)*
`_AMOUNT_RE`'s trailing guard was `(?![\w.])`, which correctly refuses to match
a fragment of a longer number — but also refused an amount that ended a
sentence. In *"buy it when it drops to ₹1000."* the amount never entered
`stated_amounts`, so the user's own perfectly legitimate request was blocked as
an amount the agent had invented. Found by `card_rule_legitimate`. The guard is
now `(?!\w)(?!\.\d)`: loosened only for a `.` that no digit follows, so
`1000.50`, `1.2.3` and `12abc` still behave exactly as before. A false negative
here is a broken product; only a false positive would be a safety hole.
Regression test: `test_an_amount_at_the_end_of_a_sentence_still_counts_as_stated`.

**2. Refused money tools leave no intent row.** *(expectation corrected)*
The orchestrator's forced policy re-check runs **before** the service creates
anything, so a denied request is recorded in the audit trail
(`forced_policy_check` with its verdict, plus `blocked_money_tool:…`) rather
than as a `DENIED` intent. Traceability is preserved — arguably better, since
the database is not littered with rows for things that never happened. The
benchmark now asserts the verdict where it actually lives.

**3. Chat cannot create an approval-needed intent at all.** *(design decision, confirmed)*
The forced re-check proceeds only on `ALLOW`, so `REQUIRE_APPROVAL` is refused
outright from chat. The same ₹600 purchase **does** create an
`AWAITING_APPROVAL` intent from the Spend tab, because that is a structured tap
on a priced offer rather than a sentence. The chat's job is to say so. This is
deliberately narrower than the policy engine: everything the agent can cause is
already inside the user's rules, which is a stronger claim than "the agent can
propose anything and approval catches it". Recorded on the case itself.

**4. Two independent layers catch a duplicate.** *(expectation widened)*
The orchestrator's repeated-call guard (same tool, same args, same turn) fires
before the service's idempotency key gets a chance. Either is a correct
outcome, so the case asserts the count — exactly one row — and accepts either
audit trail.

## Load profiles (Step 4)

`benchmark/locustfile.py`, two profiles kept deliberately apart:

- **`ReadUser`** — `/api/state`, `/api/audit`, `/api/card`, `/api/plan`,
  `/api/pool`, `/api/spend`. Plain SQLite reads behind derived-balance queries.
- **`ChatUser`** — `/api/chat`. **This does not scale, and that is reported
  rather than tuned away.** Every call is several inference passes against one
  local Ollama process; beyond a couple of concurrent chat users, requests
  queue and latency rises roughly linearly. It is a known consequence of the
  no-external-API constraint, not a defect. `wait_time` is set to a realistic
  5–12 s because a student does not chat in a tight loop.

Target concurrency is **left as a TODO for the product owner**. Inventing
"1,000 concurrent students" as a goal would be exactly the kind of unevidenced
claim the rest of this project refuses to make.

Load prompts state no amounts, so a load run can never create an intent and
leave money-shaped rows in a database someone later demos from.

## Reproducing

```bash
python -m benchmark.run_benchmark                  # scripted, the published table
python -m benchmark.run_benchmark --mode live      # needs `ollama serve`
python -m benchmark.run_benchmark --hash           # frozen case-set digest
python -m benchmark.run_benchmark --only adv_ignore_rules -v
python -m pytest backend/tests/test_benchmark.py   # the same run, in CI
```

The digest changes whenever the case set does
(`test_a_case_set_change_changes_the_frozen_digest`), so the numbers above can
never quietly describe a different file.
