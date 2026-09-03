# CampusPool — Production Readiness Review & Response

**Version:** 1.0 · **Date:** 2026-09-03
**Trigger:** A 10-category, 34-finding production-readiness review of the build plan.
**Companion to:** the PRD, `CampusPool_Agent_HLD_LLD.md`, `CampusPool_Build_Plan.md`, `CampusPool_MVP_Execution_Playbook.md`.

---

## 0. How this document answers the review

The review is correct that none of these 34 items are in the build plan. It is **not** correct that all of them should be. So every finding gets one of four verdicts, and the reasoning is stated rather than assumed:

| Verdict | Meaning | Count |
|---|---|---|
| **MVP-NOW** | Cheap, materially reduces real risk in *this* prototype, or fixes something that will visibly break in the demo. Added to the build plan. | 16 |
| **POST-MVP** | Genuinely necessary for production, genuinely wrong to build in a hackathon prototype. Specified here so nothing is lost, deliberately not built. | 11 |
| **CORRECTION** | The finding rests on a factual misunderstanding. Corrected below with sources. | 3 |
| **PRODUCT-DECISION** | Not an engineering gap. A legal, commercial or policy decision that must come from the product owner — and which the project instructions forbid me from inventing an answer to. | 4 |

The governing constraint is the project's own rule, from the coding-agent instructions:

> *"Do not add autonomous lending, investing, custody, KYC/AML claims, or real card issuing unless the PRD is explicitly updated"* and *"Do not add scaffolding for lending, investing, custody, KYC/AML, or card issuance 'for later' — that is scope creep even if unused."*

A hackathon prototype that ships Terraform modules, a Vault cluster and a PagerDuty rota has not become production-ready. It has become a prototype that no longer demos, built by a team that ran out of days. The senior-engineer move is not to build everything on the list — it is to **know** which items are load-bearing now, build exactly those well, and be able to speak precisely about the rest. That second half matters: "we deliberately deferred distributed tracing, here's the design and the trigger condition for building it" is a strong answer to a judge. "We didn't think about it" is not.

---

## 1. Three corrections — findings that rest on a misunderstanding

### 1.1 PCI-DSS is not "mandatory full compliance" here — it is SAQ A scope, but v4.0 added a criterion you *do* have to meet

**The finding said:** *"PCI-DSS: Not mentioned — mandatory for payment handling."*

**The correction:** Card data never touches this application. Razorpay Checkout collects card details inside Razorpay's own payment form; the backend only ever sees an `order_id`, a `payment_id` and a signature. That places the merchant in the **smallest** PCI scope — SAQ A — rather than the full PCI-DSS assessment the finding implies. Building your own card form is what triggers the heavy scope, and this architecture specifically does not.

**But there is a real, current obligation the finding missed.** PCI DSS v4.0 replaced SAQ A's old requirements with a new eligibility criterion: the merchant must confirm *"their site is not susceptible to attacks from scripts that could affect the merchant's e-commerce system(s)."* Per the PCI SSC's own FAQ, that criterion **applies only to pages hosting an embedded payment form (an iframe) — not to redirects or links to a payment page.**

Razorpay's standard Checkout.js opens an **embedded overlay**, which puts you inside that criterion. Two legitimate ways out, and you should pick one consciously:

- **Option A (lighter scope):** use Razorpay's redirect/hosted checkout mode, so the payment page is not embedded in your page at all, and the new criterion does not apply.
- **Option B (keep the embedded UX):** implement the two controls the SSC says are sufficient — **Requirement 6.4.3** (script authorization and integrity, i.e. a Content-Security-Policy allowlist plus Subresource Integrity on third-party scripts) and **Requirement 11.6.1** (detect unauthorised change to the payment page). These were removed as *mandatory* SAQ A requirements but remain the accepted way to satisfy the criterion.

**Verdict: MVP-NOW (partial).** The prototype handles no real cards, so nothing is legally triggered today. But a CSP header and SRI attributes on the Razorpay script are a combined ~10 lines of work, they cost nothing, and they mean the checkout page is built correctly from the start rather than retrofitted. Added to Phase 5. The choice between Option A and B is flagged as a product decision below.

### 1.2 DPDP Act 2023 does not bite on this prototype — and the real deadline is later than you'd think

**The finding said:** *"Data Privacy: India's DPDP Act 2023 compliance missing."*

**The correction, with the actual timeline.** The DPDP Rules were notified on 13 November 2025, and the Act phases in over three stages:

| Date | What comes into force |
|---|---|
| 13 Nov 2025 | Institutional provisions — Data Protection Board of India established |
| **13 Nov 2026** | Rule 4 — the Consent Manager framework |
| **13 May 2027** | Rules 3, 5–16, 22, 23 — the substantive obligations: consent and notice, data principal rights, breach notification, retention limits, security safeguards |

Penalties reach ₹250 crore per violation, and there is no indicated grace period once the substantive obligations commence.

Two things follow. First, **this prototype processes no personal data at all** — every user, offer, pool member and payout is synthetic and labelled as such, which is already a PRD requirement (§8.2). DPDP governs the processing of personal data of identifiable individuals; synthetic demo records are not that. So there is no compliance gap today. Second, the substantive deadline is **May 2027**, which means the honest framing is: this is not an omission from the MVP, it is a design constraint on the version that first onboards a real student.

**Verdict: MVP-NOW (design only), POST-MVP (compliance programme).** What's cheap and worth doing now is the design work that makes later compliance easy rather than a rewrite: data minimisation (don't collect what you don't need), a retention field on personal-data tables from day one, consent recorded as data rather than assumed, and purpose tagging. Added to Phase 8. The compliance programme itself — consent manager integration, DPO/grievance officer, breach notification runbook, DPIA — is correctly post-MVP and specified in §4 below.

### 1.3 Chargebacks and disputes cannot occur in this prototype, and in production are mostly Razorpay's mechanics

**The finding said:** *"Dispute resolution... Chargeback handling: Not mentioned."*

**The correction:** In Test Mode no real money moves, no real card is charged, and therefore no chargeback is possible — there is nothing to dispute. In production, the chargeback *mechanism* belongs to the payment aggregator: Razorpay receives the network dispute, notifies the merchant, collects evidence and represents the case. The merchant's engineering obligation is narrower and more specific than "handle chargebacks": (a) produce evidence on demand, which the append-only audit trail already does better than most systems, (b) have a **reversal path in the ledger** so a clawback is recorded as a `REVERSAL` event rather than an edit to history, and (c) never let a disputed transaction silently reappear as a duplicate.

**Verdict: MVP-NOW (the ledger reversal path only).** `LedgerEventType.REVERSAL` already exists in the schema — the finding correctly identifies that nothing *uses* it yet. Added to Phase 8 as a tested reversal path. Dispute *policy* (who decides, on what evidence, in what timeframe) is a product decision.

---

## 2. The largest compliance risk in this product is not on your list

The review covers PCI, DPDP, fraud and insurance — and misses the one that carries criminal, not civil, exposure.

**A "community savings pool" that accepts real money from students is, absent the right legal structure, an unregulated deposit scheme.** India's **Banning of Unregulated Deposit Schemes Act, 2019 (BUDS Act)** prohibits accepting deposits outside a listed regulated framework, with criminal penalties for promoters. Chit funds are separately regulated under the **Chit Funds Act, 1982**, which requires registration with the state Registrar of Chits and imposes structural obligations — a chit fund is *not* something a company can simply operate because its mechanics resemble one.

This is precisely why the PRD is emphatic that the pool is *"chit-fund inspired"*, *"simulated economics"*, and that *"each user has an individual ledger; do not model user money as an unrestricted common bank balance"* (§4.1) — and why §13 lists *"whether the community mechanism will ever use real pooled money and under what legal/regulated structure"* as an open decision before production.

**The engineering consequence, which is actionable today:** the codebase must make the *illegal* version structurally hard to build later. The schema already does some of this — `PoolCycle` holds rules and membership but no balance, and every `PoolAllocation` requires a human-readable `reason`. What should be added is an explicit invariant, enforced by a test: **no code path may produce a pooled balance.** A test asserting that the sum of all users' derived balances always equals the sum of ledger events, with no residual "pool account," turns a legal constraint into a failing build if anyone ever violates it.

**Verdict: MVP-NOW (the invariant test), PRODUCT-DECISION (everything else).** Added to Phase 8. The legal structure question is explicitly not mine to answer and is flagged in §5.

---

## 3. MVP-NOW: the 16 items being added to the build plan

These are added because each one is either cheap, or fixes something that will actually bite during the demo. Concrete technical steps for each.

### 3.1 LLM latency and the degraded-mode path *(your finding: "What if Ollama takes 30 seconds? User experience breaks")*

This is the single most likely thing to break your live demo, and it is correctly identified. A local 7B model on laptop hardware genuinely can take tens of seconds under load, and a multi-step agent turn multiplies that.

**Steps (Phase 4):**
- Hard timeout on every `llm_client.chat()` call. Start at 30s per step; `# TODO: confirm from measured p95 on demo hardware`.
- A wall-clock budget for the whole turn, not just per step — `MAX_STEPS` bounds *count*, not *time*. Once the budget is exhausted, stop and return honestly.
- **Streaming the first token** to the UI so the user sees motion, even while tools are still running.
- **Degraded mode:** if the model is unreachable or times out, the API returns real state from the ledger with a clear "assistant unavailable, here are your current numbers" response. The read-only tools are plain DB queries — balances, goal progress and offers must remain visible with the LLM entirely dead. This is the difference between "the AI is slow" and "the app is broken."
- Pre-warm the model at startup with a throwaway one-token call, so the first real user request doesn't pay model-load cost.
- Measure and record p50/p95 turn latency in the benchmark output (§3.6), so "is it fast enough" becomes a number rather than a feeling.

### 3.2 Rate limiting *(your finding: "No rate limiting (DoS protection, API abuse)")*

**Steps (Phase 8):** `slowapi` middleware on FastAPI. Per-IP limits on all endpoints; a **much tighter per-user limit on `/api/chat`** specifically, because each call costs real compute — this is the endpoint an abuser would target. Separate stricter limit on `/api/intents/{id}/execute`. Webhook endpoint exempted from per-IP limits (Razorpay's IPs would trip it) but protected by signature validation, which is stronger. Return `429` with `Retry-After`. Test that limits actually engage.

### 3.3 Velocity controls — the deterministic half of fraud prevention *(your finding: "What if someone creates 100 fake accounts to game the pool?")*

Full fraud detection is post-MVP, but the cheap deterministic layer belongs in the policy engine now, where it is testable:

**Steps (Phase 8, into `policy_engine.py`):**
- Max intents per user per hour and per day → `DENY` beyond it.
- Max *pending* intents per user at once (an abuser opens many and settles none).
- Pool membership is invite/seed-only in the demo, with a fixed cycle size the code enforces — so "100 fake accounts joining" is not reachable by design rather than by detection.
- Duplicate-device/account correlation, velocity scoring and behavioural models are post-MVP (§4.6).

Note the design point worth saying out loud: these are *policy rules*, so they live in the same deterministic engine as spending limits, get the same table-driven tests, and are enforced by the same structural re-check in the orchestrator. Fraud controls that live in the LLM's prompt would be worthless.

### 3.4 Audit trail tamper-evidence *(your finding: "How do you prevent admins from modifying logs?")*

Excellent finding, and cheap to solve properly. An append-only *convention* is not tamper-evident; a **hash chain** is.

**Steps (Phase 1 — schema change, do it now while the schema is fresh):**
- Add `prev_hash` and `entry_hash` to `AuditEvent`.
- On write: `entry_hash = SHA256(prev_hash || actor || action || inputs_hash || policy_result || provider_result || timestamp)`.
- `audit_service.verify_chain()` walks the table and reports the first index where the chain breaks.
- A test that writes N events, tampers with one row directly via SQL, and asserts `verify_chain()` detects it at the right index.

This is genuinely strong for a fintech demo: you can hand a judge a mutated database and have the system point at the forged row. Full write-once storage (S3 Object Lock, append-only WORM) is post-MVP; the hash chain is what makes tampering *detectable*, which is the property that actually matters.

### 3.5 Structured logging, metrics, health checks *(your findings: metrics collection, uptime monitoring, alerting)*

The full Prometheus/Grafana/Jaeger stack is post-MVP. What is cheap now is making the application *emit* the data those tools would consume, so adopting them later is configuration rather than instrumentation.

**Steps (Phase 8):**
- **Structured JSON logging** with a `request_id` propagated through the agent turn, every tool call, and any resulting webhook — so one identifier reconstructs an entire transaction. This is 90% of the value of distributed tracing at 2% of the cost, and it is the right stopping point for a single-service prototype.
- **`prometheus-client` + `/metrics`**: counters for tool calls by name and outcome, policy decisions by verdict, intent transitions by state, webhook events by type and validity; histograms for agent turn duration and LLM step latency. These are exactly the metrics an SRE would want, and they double as your benchmark instrumentation.
- **`/health` (deep):** already exists shallow; extend to report DB connectivity, Ollama reachability, and Razorpay configuration state, with an overall status. Keep the shallow `/health` for liveness and add `/health/ready` for readiness.
- **Alerting is deliberately not built.** With no production deployment and no on-call rotation, an alerting pipeline has no recipient. The metrics being exported is the precondition; the alert rules are specified in §4.4 for when there is someone to page.

### 3.6 Load testing and chaos testing *(your findings: "1000 concurrent users", "webhook delayed 10 minutes")*

**Steps (Phase 8):**
- **Load test with `k6` or `locust`**, and split the two very different profiles: read endpoints (`/api/state`) should sustain meaningful concurrency; `/api/chat` is bounded by a single local model and will not, which is a *finding to report honestly*, not a failure to hide. Target numbers: `# TODO: confirm expected demo concurrency with product owner` — inventing "1000 users" as a goal would itself violate the non-hallucination rule.
- **Chaos scenarios, as automated tests** rather than manual pokes: webhook delayed past the reconciliation window; webhook delivered twice; webhooks delivered out of order (`payment.failed` after `payment.captured`); webhook for an unknown order; Razorpay API returning 5xx during `create_order`; Razorpay timing out *after* the order was actually created (the dangerous one — resolved by `client_ref` idempotency plus reconciliation); Ollama killed mid-turn; database locked mid-transaction. Each asserts the system reaches a correct or explicitly-EXCEPTION state, never a wrong one.

The Razorpay-timeout-after-creation case deserves specific mention because it is the classic payments bug: the call fails from your side but succeeded on theirs. The design already handles it — reconciliation fetches authoritative status and `client_ref` prevents a duplicate — but it must be *tested*, not merely designed.

### 3.7 CI pipeline and secret scanning *(your findings: "CI/CD pipeline not detailed", secrets management)*

**Steps (Phase 8, GitHub Actions — you have `github.com/vaibhav45sktech`):**
- On every push and PR: install deps, run `pytest`, run `ruff` (lint) and `mypy` on `backend/`.
- **Fail the build on `rzp_live`** anywhere in the diff, and on any file matching `.env`. This is the automated version of a check the playbook currently asks a human to remember.
- `gitleaks` or `detect-secrets` as a pre-commit hook *and* a CI step — belt and braces, because the pre-commit hook only protects developers who installed it.
- `pip-audit` for known CVEs in dependencies.
- Deployment pipelines, environment promotion and IaC are post-MVP (§4.5) — there is no environment to promote to yet.

### 3.8 Database durability for the demo *(your finding: "No backup strategy")*

**Steps (Phase 8):** a `scripts/backup_db.py` using SQLite's `.backup` API (safe on a live database, unlike copying the file), run before the demo and on a timer during it; a documented, *rehearsed* restore — an unrehearsed restore procedure is a hope, not a plan; and `seed/demo_data.py` kept fully idempotent so the entire demo state is reproducible from code in seconds. For a prototype, "reproducible from seed" is a stronger guarantee than backups.

Postgres with PITR, replicas, RTO/RPO targets and multi-region: post-MVP (§4.4).

### 3.9 Connection pooling *(your finding: "No database connection pooling strategy")*

Partly a non-issue at this scope and partly worth being explicit about. SQLite with `StaticPool` in tests and default pooling in the app is already configured in `backend/models/db.py`. For SQLite the meaningful tuning is different from Postgres:

**Steps (Phase 8):** enable **WAL mode** (`PRAGMA journal_mode=WAL`) so readers don't block the writer — the actual concurrency constraint with SQLite; set `PRAGMA busy_timeout` so brief lock contention retries instead of erroring; keep `PRAGMA foreign_keys=ON` (already done). Document that the migration path is a connection-string change plus `pool_size`/`max_overflow` tuning, since SQLAlchemy already abstracts it.

### 3.10 Reconciliation automation and data integrity *(your finding: "Daily reconciliation mentioned, but automated?")*

**Steps (Phase 5 + Phase 8):**
- The stuck-intent sweeper (already planned) runs on a scheduler, not manually, and exports its results as metrics.
- **A daily full reconciliation job**: fetch all Razorpay payments for the period, compare against local intents in both directions, and report three classes — payments Razorpay has that we don't, intents we think succeeded that Razorpay doesn't confirm, and amount mismatches. Any discrepancy opens an `ExceptionRecord`; none are auto-corrected, because silently "fixing" a financial mismatch is how you turn a detectable problem into an undetectable one.
- **A ledger integrity check**: derived balances recomputed from events must match on every run, and the pool invariant from §2 must hold.

### 3.11 Remaining MVP-NOW items, briefly

- **Retention and purpose fields on personal-data tables** (DPDP-ready design, Phase 8) — a `retention_until` column and purpose tag cost nothing now and are painful to retrofit.
- **Ledger reversal path**, tested (§1.3, Phase 8).
- **Pool invariant test** (§2, Phase 8).
- **CSP + Subresource Integrity on the checkout page** (§1.1, Phase 5).
- **Model integrity check** *(your finding: "what if the model is corrupted?")* — record the model digest `ollama show` reports at startup and log it; a silently swapped or truncated model then shows up in logs rather than as mysterious behaviour. Phase 8.
- **Graceful degradation matrix documented and tested** — for each dependency (Ollama, Razorpay, DB), what the app still does when it's down. Phase 8.

---

## 4. POST-MVP: specified, deliberately not built

These are real and correctly identified. Each gets the design and, more usefully, **the trigger condition** — the thing that should make you actually build it.

### 4.1 Secrets management
**Trigger:** first deployment to shared infrastructure, or first real key. **Design:** AWS Secrets Manager or HashiCorp Vault; short-lived credentials injected at runtime, never in env files on disk; rotation schedule for Razorpay keys and webhook secrets; distinct keys per environment. **Until then:** `.env` locally, `.gitignore`d, CI-enforced (§3.7), and the `rzp_test_` guard means the worst case is a leaked sandbox credential.

### 4.2 Observability stack
**Trigger:** more than one instance, or the first incident nobody noticed. **Design:** Prometheus scraping the `/metrics` endpoint built in §3.5; Grafana dashboards for payment success rate, policy denial rate, agent latency p95, exception queue depth; OpenTelemetry tracing once there is more than one service to trace *between* — for a single FastAPI process, the `request_id` in structured logs is genuinely sufficient, and adopting Jaeger now would be cargo cult; Sentry or equivalent for exception aggregation (this one is cheap enough that it's a reasonable early add).

### 4.3 Alerting and on-call *(your finding: "How do you know when something breaks at 3 AM?")*
**Trigger:** the moment a real user depends on the system. **Design:** alert on payment success rate dropping below threshold, exception queue depth growing, reconciliation discrepancies, webhook signature failures spiking (an attack signal), agent p95 latency breaching budget, and dependency health failures. Routed to PagerDuty/Opsgenie with severity tiers and a documented escalation path. **Honest answer to the 3 AM question today:** you don't, and for a prototype with no users that is the correct trade — but the metrics are exported so this becomes rules-not-instrumentation later. Thresholds: `# TODO: confirm with product owner` once there's baseline data; picking numbers before you have baselines produces alerts people learn to ignore.

### 4.4 Disaster recovery and business continuity
**Trigger:** real user funds or a real uptime commitment. **Design:** Postgres with continuous archiving and point-in-time recovery; documented and *drilled* RTO/RPO (`# TODO: confirm targets with product owner` — these are business decisions about acceptable loss, not engineering preferences); multi-AZ; a runbook per failure mode; quarterly restore drills. **On "what if Razorpay is down for 6 hours":** the architecture's answer is already partly built — intents queue in `EXECUTING`, reconciliation settles them when the provider returns, and the UI shows "processing" rather than claiming success. What's post-MVP is a **circuit breaker** that stops hammering a dead provider, a user-facing status banner, and the commercial question of a secondary payment provider (§5).

### 4.5 Deployment, environments, IaC
**Trigger:** more than one developer deploying, or the first customer. **Design:** dev → staging → prod with promotion by artifact rather than rebuild; Terraform for infrastructure; containerised app; blue-green or canary once there is traffic worth protecting; automated rollback on health-check failure; database migrations via Alembic with an explicit backward-compatibility policy (the prototype uses `create_all`, which cannot alter tables — Alembic becomes necessary the first time schema changes must survive existing data).

### 4.6 Fraud detection beyond velocity limits
**Trigger:** real money, or the first gaming attempt. **Design:** device fingerprinting and account-correlation signals; graph analysis on pool membership for collusion clusters; anomaly scoring on spend patterns; manual review queue with reviewer tooling. **Important architectural note:** every one of these must produce a *deterministic* signal consumed by the policy engine. The LLM must never be the fraud decision-maker — same principle as spending limits.

### 4.7 Penetration testing and security review
**Trigger:** before first real user. **Design:** third-party pen test scoped to the API and payment flow; automated DAST in CI; threat model covering the agent-specific surface. That last item deserves emphasis, because it is not on standard security checklists: **prompt injection** is a live attack class here. A malicious offer description or merchant name flowing into the model's context is untrusted input. The current architecture already contains the blast radius — the model can only *request* tools, backend-only tools are invisible to it, and the orchestrator re-checks policy independently — which means a successful injection still cannot move money. Worth testing explicitly as part of the benchmark's adversarial category, and worth saying to judges, because most agent demos have no answer here.

### 4.8 Caching and horizontal scaling
**Trigger:** measured latency problems, not anticipated ones. **Design:** Redis for offer catalogue and derived-balance caching, with explicit invalidation on ledger append (**never** cache a balance without invalidation — a stale balance is a wrong balance, and this is precisely why the MVP derives them every time); stateless app instances behind a load balancer; the agent scales horizontally only if conversation state moves to shared storage; the local LLM is the real bottleneck and would need a served model with batching (vLLM) rather than per-instance Ollama. **Deliberately not built:** adding a cache to a system whose correctness depends on always-fresh balances is a way to introduce financial bugs in exchange for latency you haven't measured a need for.

### 4.9 Data archival and log retention
**Trigger:** DPDP substantive obligations (May 2027) or storage cost. **Design:** hot/cold tiering, archival to object storage with lifecycle rules, deletion workflows honouring data-principal erasure requests while preserving what financial regulation requires retained. **The conflict is the interesting part** and needs legal input: DPDP pushes toward deletion, financial record-keeping rules push toward retention. Resolving that tension is §5, not an engineering call.

### 4.10 User support and incident response
**Trigger:** first real user. **Design:** support ticketing with an agent-facing view of the audit trail (the trail already makes "what happened to my payment" answerable, which is most of support); documented incident severity levels and comms templates; a status page.

### 4.11 Vendor lock-in and provider abstraction *(your finding: "How hard is it to switch payment providers?")*
Already partly addressed by design and worth making explicit: **all Razorpay contact is confined to `razorpay_adapter.py`**, enforced by a CI check that exactly one file imports the SDK (§3.7). Swapping providers means reimplementing one module against the same internal interface — `create_order`, `verify_signature`, `fetch_payment` — plus a webhook handler. The same isolation exists for the LLM in `llm_client.py`. **Post-MVP:** a formal provider interface with two live implementations, which is the only way to actually prove portability rather than assume it.

---

## 5. PRODUCT-DECISION: four questions I must not answer for you

The project instructions are explicit: *"When a requirement is missing, they must ask or create a clearly marked TODO rather than inventing a financial rule."* These are the four that block real production and cannot be resolved in code.

| # | Question | Why it's not an engineering decision | Where it's already flagged |
|---|---|---|---|
| 1 | **Will the community pool ever hold real pooled money, and under what legal structure?** | BUDS Act 2019 and the Chit Funds Act 1982 make this a criminal-liability question requiring counsel and possibly state registration or a regulated partner. No architecture makes an unregulated deposit scheme lawful. | PRD §13, and §2 above |
| 2 | **Data retention periods, and how DPDP erasure rights reconcile with financial record-keeping duties** | Two legal regimes in tension; needs counsel, not a default value | §4.9 |
| 3 | **Promised uptime/SLA to users, and RTO/RPO targets** | Commercial commitments that determine engineering spend, not the reverse. Also: Razorpay's own contractual uptime guarantee is a matter for your merchant agreement — check `status.razorpay.com` for history, but don't take a blog figure as your SLA. | §4.3, §4.4 |
| 4 | **Cyber liability insurance, dispute policy, and secondary payment provider** | Business/finance decisions | §4.4, §1.3 |

Additionally, one narrower technical decision needs your call: **Razorpay embedded checkout (keep the nicer UX, implement CSP + SRI per §1.1 Option B) or redirect checkout (lighter PCI scope, slightly worse UX)?** I'd suggest embedded plus the two controls, since they're cheap and you learn the right pattern — but it's your call.

---

## 6. Where this lands in the build plan

| Item | Phase |
|---|---|
| Audit hash chain (schema change) | **Phase 1** — now, while the schema is fresh |
| LLM timeouts, wall-clock budget, degraded mode, pre-warm, streaming | **Phase 4** |
| CSP + SRI on checkout; automated reconciliation job | **Phase 5** |
| **New Phase 8 — Hardening:** rate limiting, velocity controls, structured logging + `request_id`, `/metrics`, deep health checks, CI with secret scanning, backup/restore rehearsal, WAL + busy_timeout, load tests, chaos tests, ledger reversal path, pool invariant test, retention fields, model digest logging, degradation matrix | **Phase 8** |
| Everything in §4 | Post-MVP, with trigger conditions |
| Everything in §5 | Blocked on product owner |

Phase 8 is roughly **1.5–2 days** of work. If the schedule tightens, the priority order within it is: audit hash chain and pool invariant (they're the compliance story) → rate limiting and velocity controls (they're the abuse story) → chaos tests (they're the reliability story) → metrics and CI (they're the operational story) → load tests last, since the honest answer there is already known.

---

## 7. What to say when a judge asks

Short version, and it's a stronger answer than a checklist would be:

> "Card data never touches our servers — Razorpay's form handles it, which keeps us in SAQ A scope, and we implement CSP and Subresource Integrity to meet PCI DSS 4.0's script-attack criterion for embedded forms. DPDP's substantive obligations start May 2027; our prototype processes only synthetic data, and we've built retention and purpose fields now so compliance is configuration later. Our audit trail is a hash chain, so tampering is detectable — hand us a modified database and the system points at the forged row. The biggest real risk in this product isn't technical: a savings pool holding real money implicates the BUDS Act, which is why the pool is simulated and every user keeps an individual ledger, enforced by a test that fails the build if a pooled balance is ever created. Distributed tracing, Vault and blue-green deploys are deliberately deferred — here's the design and the trigger condition for each."

---

## Sources

- [PCI SSC FAQ — SAQ A eligibility criteria for e-commerce merchants](https://www.pcisecuritystandards.org/faqs/1292/)
- [The Hidden Trap in the PCI DSS SAQ A Changes — TrustedSec](https://trustedsec.com/blog/the-hidden-trap-in-the-pci-dss-saq-a-changes)
- [FAQ Clarifies New SAQ A Eligibility Criteria — PCI SSC Blog](https://blog.pcisecuritystandards.org/faq-clarifies-new-saq-a-eligibility-criteria-for-e-commerce-merchants)
- [PCI SSC Clarifies Obligations for Ecommerce Merchants That Outsource Payment Card Processing — Davis Wright Tremaine](https://www.dwt.com/blogs/privacy--security-law-blog/2025/03/pci-faqs-card-processing-ecommerce-merchants)
- [DPDP Act and Rules 2025: The 2026 Compliance Milestones — Mondaq](https://www.mondaq.com/india/data-protection/1830402/dpdp-act-and-rules-2025-the-2026-compliance-milestones-businesses-cant-afford-to-miss)
- [India's New Data Privacy Rules Are Here — Fisher Phillips](https://www.fisherphillips.com/en/insights/insights/indias-new-data-privacy-rules-are-here)
- [The Banning of Unregulated Deposit Schemes Bill, 2019 — PRS India](https://prsindia.org/billtrack/the-banning-of-unregulated-deposit-schemes-bill-2019)
- [BUDS Act 2019 overview — Legal Service India](https://www.legalserviceindia.com/legal/article-17681-buds-act-banning-of-unregulated-deposit-schemes-act-2019.html)
- [Payment Gateway Uptime SLAs in 2026 — Razorpay blog](https://razorpay.com/blog/payment-gateway-uptime-slas/)
- [Razorpay status history — StatusGator](https://statusgator.com/services/razorpay)
