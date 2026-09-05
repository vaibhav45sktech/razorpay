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
| 2 | Failed payment (failure instrument) | pending | |
| 3 | Webhook only (close tab before handler fires) | pending — needs webhook registration (ngrok) | |
| 4 | Duplicate webhook (dashboard resend) | pending — needs webhook registration | |
| 5 | Invalid webhook signature via curl | pending — needs webhook secret | |
| 6 | Delayed webhook (kill ngrok, pay, restart) → reconciliation settles | pending | |
| 7 | Tampered checkout callback | pending | |
| 8 | RazorpayX test payout | **cut** (plan Step 10: optional, cut first) | |

**Definition-of-Done clause "next agent turn reports the new real balance":** pending (`Chat "What's my balance now?"` → expect ₹1,800).

Automated counterparts of rows 2–7 (with the provider faked at the adapter boundary) are in
`backend/tests/test_phase5_razorpay.py` — the 8 chaos tests — and pass in CI. This file records the
*live* runs.
