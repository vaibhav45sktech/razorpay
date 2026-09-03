# CampusPool

An AI-controlled student money manager that turns small recurring savings into an
emergency-oriented cushion, adds community and merchant incentives, and lets
students spend through explicit rules with Razorpay-backed payment execution.

> **This is a hackathon prototype.** It is not a real chit fund, credit card,
> lending, or investment product. It holds no real money. All seed data is
> **synthetic/demo** and all payments run in **Razorpay Test Mode**.

## Documentation

| Document | What it covers |
|---|---|
| `Student_AI_Financial_Ecosystem_PRD (1).pdf` | Product requirements — the highest source of truth |
| `CampusPool_Agent_HLD_LLD.md` | Architecture: high- and low-level design of the agent + payment flow |
| `Building_Your_First_AI_Agent.md` | Beginner's conceptual guide to how AI agents actually work |
| `CampusPool_Build_Plan.md` | Phase-by-phase build plan |
| `CampusPool_MVP_Execution_Playbook.md` | Literal step-by-step execution process + engineering practices |
| `CampusPool_Production_Readiness.md` | Production-readiness review: what's built now, what's deferred, and the compliance position |

## Architecture in one paragraph

Exactly **one** LLM agent (the Financial Agent) reasons about the user's request
and may *request* tools. Every consequence is carried out by deterministic,
LLM-free backend code: a policy engine decides ALLOW/DENY/REQUIRE_APPROVAL, an
append-only ledger derives all balances, and a single adapter module is the only
code allowed to talk to Razorpay. The LLM is never a source of truth for
financial state.

## Local setup

Requires Python 3.10+ and (from Phase 4 onward) [Ollama](https://ollama.com).

```bash
# 1. Virtual environment
python -m venv .venv
.venv\Scripts\activate        # Windows
# source .venv/bin/activate     # macOS / Linux

# 2. Dependencies
pip install -r requirements.txt

# 3. Configuration
copy .env.example .env          # Windows
# cp .env.example .env            # macOS / Linux
# Razorpay keys can stay blank until Phase 5.

# 4. Run
uvicorn backend.main:app --reload --port 8000
```

Then open http://localhost:8000/health and http://localhost:8000/docs

## Tests

```bash
pytest backend/tests -v
```

## Build status

| Phase | Status |
|---|---|
| 0 — Repo, environment, tool-calling proof | **done** — app boots, config guard tested (7 tests) |
| 1 — Data layer | in progress — models, session layer, tamper-evident audit trail done (31 tests) |
| 2 — Policy engine | not started |
| 3 — Money state machine (fake executor) | not started |
| 4 — The Financial Agent | not started |
| 5 — Razorpay Test Mode | not started |
| 6 — Frontend | not started |
| 7 — Benchmark + hardening | not started |
| 8 — Hardening (rate limits, metrics, chaos + load tests, CI) | not started |
