# Degradation matrix — Phase 8 item 8

What the app still does when each dependency is gone. **Every row is enforced
by a test in `backend/tests/test_degradation.py`** — an untested degradation
matrix is a wish, and the whole point of writing one is to find the row where
the code disagrees with the document.

Reported live at `GET /health/ready`.

| Dependency | Down means | What still works | What stops | User sees |
|---|---|---|---|---|
| **Ollama** (local model) | unreachable, wrong model pulled, or circuit breaker open | Everything except conversation: all reads, the Autopilot plan, the pool recommendation, offer matching, the Agentic Card monitor, policy decisions, approvals, Razorpay checkout | Chat replies in natural language | Their **real numbers**, with an "assistant unavailable" note. Not an error page, not a blank screen, and never a guessed figure. |
| **Razorpay** (test mode) | keys absent, or API erroring | Reads, policy, intents up to `ALLOWED`/`AWAITING_APPROVAL`, approvals, the whole audit trail | Executing an intent (`/api/intents/{id}/execute` → 503), so nothing settles | "Razorpay test mode is not configured on this server". The proposal is still on record and pays later, once configured. |
| **Razorpay webhook secret** missing | every webhook is rejected | Checkout still verifies server-side; reconciliation still asks Razorpay directly | Nothing — settlement takes the slower path | Nothing, until reconciliation resolves it. `/health/ready` flags it as `degraded`. |
| **Database** | file unreadable / corrupt / locked out | Only `/health` (liveness, touches nothing) | Everything | An error, honestly. Without the ledger there is nothing truthful left to say, so the app says nothing rather than guessing — this is the one dependency whose loss is `down`, not `degraded`. |
| **Ollama returns garbage** (malformed JSON, or parrots the question back) | model up but useless | All of the above | The reply is discarded rather than shown | A plain "I couldn't put together a proper answer — nothing was executed", and the event is audited (`parrot_retry`, `unkept_promise`). |
| **Poisoned tool result** (injected instruction in offer text) | untrusted data in context | Read-only answers, for the rest of the turn | Every money tool, for the rest of the turn (taint lock) | A factual answer plus a note that the text looks suspicious. The lock is **per-turn**: the user's own next request works normally. |

## Why "no Ollama" is degraded and not unready

This is the row worth arguing about, so it is stated plainly: `/health/ready`
returns **200 `degraded`** when Ollama is missing, not 503. The product's
claim is that the model is a convenience and the ledger is the truth. If the
model's absence took the service out of rotation, the architecture would be
admitting the opposite. A student with no Ollama can still see their savings,
their plan, their pool round and their card's rules — that is most of the
value, and it is all verified.

Only the database earns `down`.

## Rehearsing it

Each of these is worth doing by hand once before a demo — the tests prove the
code paths, but only the terminal proves the experience.

```bash
# 1. Ollama down
pkill ollama            # or: ollama stop qwen2.5:7b-instruct
curl -s localhost:8000/health/ready | python -m json.tool
#   -> status "degraded", ollama.reason "unreachable: ConnectError"
#   Then chat in the UI: real numbers, "assistant unavailable" note, no 500.

# 2. Razorpay absent
#   Comment RAZORPAY_KEY_ID out of .env and restart.
#   -> Autopilot still proposes; "Agree & pay" returns 503 with a clear reason.

# 3. Wrong model pulled
OLLAMA_MODEL=not-a-real-model python -m uvicorn backend.main:app
curl -s localhost:8000/health/ready | python -m json.tool
#   -> ollama.reason "model 'not-a-real-model' is not pulled on this Ollama"
#   This is the failure that would otherwise surface as a confusing runtime
#   error on the first chat instead of a clear one at startup.

# 4. Database gone
mv campuspool_demo.db /tmp/   # with the server running
curl -s -o /dev/null -w '%{http_code}\n' localhost:8000/health        # 200 (liveness)
curl -s -o /dev/null -w '%{http_code}\n' localhost:8000/health/ready  # 503
```

## The tamper demo (Phase 8 Definition of Done)

Not a degradation, but the same "prove it, don't claim it" spirit. Forge a row
by raw SQL and watch the system name it:

```bash
sqlite3 campuspool_demo.db \
  "UPDATE audit_events SET action='intent:POLICY_CHECK->ALLOWED' WHERE seq=(SELECT MIN(seq) FROM audit_events);"
curl -s localhost:8000/api/audit | python -c "import sys,json; print(json.load(sys.stdin)['chain'])"
#   -> {"ok": false, "reason": "seq 1", ...}   and the UI's chain pill turns red.
```

The hash chain does not make the log immutable — nothing in a single database
can — but it makes tampering **detectable**, and it names the exact entry where
the chain first breaks.
