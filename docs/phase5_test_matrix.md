# Phase 5 test-payment matrix — HLD §6.6, run against real Razorpay TEST MODE

Evidence for master plan Phase 5 Step 9 / Definition of Done. Each row is
filled in as it is run; the audit rows quoted come from
`scratch/inspect_agent_state.py` against the same database the API uses.

**Environment:** Windows laptop, backend `uvicorn` on 127.0.0.1:8000 (branch `phase-5-razorpay`),
Razorpay Test Mode key `rzp_test_…` (never committed), `DATABASE_URL=sqlite:///C:/campuspool_data/campuspool_demo.db`
(outside OneDrive), demo user Aarav `usr_e8b598e553764e3294de`.

| # | Scenario | Result | Evidence |
|---|---|---|---|
| 1 | Happy contribution (success instrument) | **PASS** 2026-09-05 | ₹300 CONTRIBUTION intent `int_3cbdb0a2f24d42139cae` → `POST /api/intents/{id}/execute` created order `order_TY8bl92MC0RfGe` (audit seq 8 `ALLOWED->EXECUTING`) → Razorpay Checkout, **Netbanking → mock bank page → Success** → checkout handler → `POST /api/checkout/verify` (signature verified, payment fetched from the API) → seq 9–12 `EXECUTING->SUCCESS`, `SUCCESS->VERIFIED`, `ledger_append:CONTRIBUTION`, `VERIFIED->LEDGER_UPDATED`. Ledger row `led_2423490c…` +30000 emergency_savings, **source `razorpay_payment:pay_TY8eKzZuVGHZca`**. Emergency savings ₹1,500 → ₹1,800. |
| 2 | Failed payment (failure instrument) | **PASS** 2026-09-05 | ₹200 intent `int_09b982b4ef7e404eae7a` → order → Checkout Netbanking → mock bank **Failure**. Browser-side failure event was NOT treated as evidence: intent stayed `EXECUTING`, ledger untouched. Webhooks were at that moment all being rejected (signature mismatch, see row 5 note), so the **reconciliation sweeper** resolved it from Razorpay's authoritative status after the 15-minute window: seq 31–32 `EXECUTING->FAILURE`, `FAILURE->CLOSED`, `actor: system`, 05:55:44. No ledger row. |
| 3 | Webhook only (browser never reports back) | **PASS** 2026-09-05 | Variant run: backend stopped (Ctrl+C) while the mock bank page was open, then **Success** clicked — the checkout handler's `/api/checkout/verify` call failed (server down). Backend restarted; Razorpay's `payment.captured` delivery (via ngrok) settled it alone: ₹150 intent `int_13a30f43b7fd4162ac82`, seq 42–45 `EXECUTING->SUCCESS`, `SUCCESS->VERIFIED`, `ledger_append`, `VERIFIED->LEDGER_UPDATED` with **`actor: webhook`**; ledger `razorpay_payment:pay_TYFgSOYjmrnytw`. Also observed: the ₹250 (`pay_TYFOwlb0jybhOh`) had been settled by the fast path first, and its webhook then returned 200 with outcome `already_settled` — two channels, one credit. |
| 4 | Duplicate webhook delivery | **PASS** 2026-09-05 | The `payment.captured` for `pay_TYFgSOYjmrnytw` was replayed from the ngrok inspector (identical bytes: same signature, same `x-razorpay-event-id`) three times → each returned 200 `{"status":"already_processed"}`; audit `webhook_duplicate_ignored` seq 46, 47, 49 (and 56); one ledger row only; balance unchanged. |
| 5 | Invalid webhook signature | **PASS** 2026-09-05 | `POST /api/webhooks/razorpay` with `X-Razorpay-Signature: deadbeef` and a captured-payment body for ₹999 → **HTTP 400 `{"status":"invalid_signature"}`**; audit `invalid_signature_rejected` (no user); Aarav's audit rows, intents and ledger unchanged. Earlier the same day, real Razorpay deliveries were rejected the same way while the backend ran without the webhook secret — six 400s in the ngrok inspector, no state change, Razorpay retried until the restart. Note: ngrok's inspector (http://127.0.0.1:4040) is the delivery log + replay tool; the Razorpay dashboard's webhook panel shows configuration only. |
| 6 | Delayed/blocked webhook → reconciliation | **PASS (observed live, unplanned)** | Between 11:22 and ~11:35 IST every real Razorpay delivery returned 400 (webhook secret in the dashboard did not match `.env`) — the channel was effectively down. The sweeper still closed the failed ₹200 intent (row 2) from `fetch_order_payments`. Planned kill-ngrok variant still to run for the *success* path. |
| 7 | Tampered checkout callback | **PASS** 2026-09-05 | `POST /api/checkout/verify` with the real order id + real payment id + a forged signature → **HTTP 400 `invalid checkout signature`**; `ExceptionRecord exc_247851afe6c747948c02` kind `invalid_checkout_signature` opened against the intent (audit seq 50); intent and ledger unchanged. |
| 8 | RazorpayX test payout | **cut** (plan Step 10: optional, cut first) | |

**Definition-of-Done clause "next agent turn reports the new real balance":** **MET** 2026-09-05 — `Chat "What's my emergency savings balance now?"` → *"Your emergency savings balance is ₹2,200.00."* (₹1,500 seed + ₹300 + ₹250 + ₹150 real test-mode payments; `steps: 1`, figure from the verified state snapshot).

Automated counterparts of rows 2–7 (with the provider faked at the adapter boundary) are in
`backend/tests/test_phase5_razorpay.py` — the 8 chaos tests — and pass in CI. This file records the
*live* runs.


## Phase 5 — Definition of Done

| Clause | Status |
|---|---|
| Every §6.6 row checked off | Rows 1–7 PASS live (row 6 in its blocked-webhook form; kill-ngrok variant for the success path not separately run — same code path as the row-2 sweep). Row 8 (RazorpayX payout) cut per plan Step 10. |
| All 8 chaos tests green | `backend/tests/test_phase5_razorpay.py` — 8/8, plus 16 more Phase 5 tests. |
| `grep` confirms one SDK importer | Enforced by `test_exactly_one_module_imports_the_razorpay_sdk`. |
| Reconciliation proven by a delayed webhook | Proven with webhooks *rejected* (secret mismatch): sweeper closed the failed ₹200 from Razorpay's status (rows 2/6). |
| Test card → webhook → ledger → agent reports new balance | ₹150 settled by webhook alone (row 3); agent: "Your emergency savings balance is ₹2,200.00." |

**Also found and fixed during this run (agent layer):** Guardrail 5 — write provenance (`update_goal` called on a balance question; now blocked unless the user asked); handler refusals become tool results instead of 500s; paused goals stay visible in state; per-call latency metrics, no fill call for argument-free tools, compact state, explicit `num_ctx`.

**Signed off:** 2026-09-05, Vaibhav Mishra with Claude. Tag `v0.5-razorpay`.
