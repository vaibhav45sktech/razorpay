# The compliance answer

Phase 9 item 4. `CampusPool_Production_Readiness.md` has the full analysis with
citations; this is the version you say out loud, and the reasoning behind each
claim so you are not reciting it.

---

## The 60-second version

> "Card data never touches our servers — Razorpay's own form collects it and we
> only ever see an order id, a payment id and a signature, which keeps us in the
> smallest PCI scope, SAQ A, rather than a full assessment.
>
> DPDP's substantive obligations start in May 2027, and this prototype
> processes no personal data at all — every user, merchant and pool member is
> synthetic. We've still built retention and purpose fields into the
> personal-data tables now, so compliance later is configuration rather than a
> rewrite.
>
> Our audit trail is a hash chain, so tampering is detectable rather than
> merely discouraged. Hand us a modified database and the system points at the
> forged row.
>
> And the biggest real risk in this product isn't technical. **A savings pool
> that holds real student money is, without the right legal structure, an
> unregulated deposit scheme** under the BUDS Act 2019, with chit funds
> separately regulated under the Chit Funds Act 1982. That's exactly why our
> pool is simulated and every member keeps an individual ledger — enforced by a
> test that fails the build if a pooled balance is ever created."

---

## The four claims, and why each one holds

### 1. PCI-DSS: SAQ A, not a full assessment

**Why:** the architecture never sees a card number. Razorpay Checkout collects
card details inside Razorpay's own iframe; `razorpay_adapter.py` — the only
module permitted to talk outward — handles an `order_id`, a `payment_id` and an
HMAC signature. Building your own card form is what pulls a merchant into the
heavy scope, and this deliberately does not.

**The one thing to know:** PCI DSS v4.0 added a script-integrity criterion
(6.4.3) that *does* apply to a page embedding a third-party payment form. The
answer is CSP plus Subresource Integrity on the Checkout script — cheap, and
named here so it is not a surprise.

**If pushed:** "we're a merchant in SAQ A scope, not a service provider. The
scope boundary is that no cardholder data enters our systems, and you can
verify that from the adapter module — it's the only outbound code path."

### 2. DPDP Act 2023: no gap today, designed for later

**Why:** DPDP governs personal data of *identifiable individuals*. Every record
here is synthetic and labelled as such in code and UI copy (a PRD §8.2
requirement, asserted by `test_seed.py`). There is no personal data to protect.

**What was done anyway:** `User` carries `purpose` and `retention_until` from
day one, and `AuditEvent` stores a **hash** of tool inputs rather than raw
argument text — so the decision trail stays largely outside the scope of an
erasure request by design, not by luck.

**The honest framing:** "this isn't an omission from the MVP; it's a design
constraint on the version that first onboards a real student. The substantive
deadline is May 2027."

**The genuinely hard part, named rather than glossed:** DPDP pushes toward
deletion, financial record-keeping rules push toward retention. That tension is
a legal question, not an engineering one, and it is escalated in the master
plan rather than silently resolved by whoever wrote the last migration.

### 3. The audit trail: tampering is detectable

**Why:** each `AuditEvent` commits to its own content *and* to the previous
entry's hash, with a monotonic `seq` giving the chain a defined order.
`verify_chain()` reports the exact index where it first breaks.

**Say the limitation out loud, because it is what makes the claim credible:**

> "This does not make the log immutable — nothing inside a single database can.
> It makes forgery impossible to do *silently*."

**Demo it** rather than describing it: `UPDATE audit_events SET action=… WHERE
seq=1`, refresh, and the chain pill names the row. That is direct database
access — the strongest attacker available — and it is still caught.

### 4. The pool: the real risk, and how it is contained

**This is the answer that matters, and it should not be buried.** India's
**Banning of Unregulated Deposit Schemes Act, 2019** prohibits accepting
deposits outside a listed regulated framework, with criminal liability for
promoters. **Chit funds** are separately regulated under the **Chit Funds Act,
1982**, requiring registration with the state Registrar of Chits. A company
cannot simply operate a chit fund because its product mechanics resemble one.

**So the pool holds nothing.** It is a set of *rules and membership*:

- `PoolCycle` records the rules; it has no balance column, because there is no
  balance.
- Every member keeps an individual `LedgerEvent` history. All balances are
  derived per user.
- Every allocation carries a written `reason`, and the policy engine surfaces
  that reason as the authorisation for any payout.
- `test_pool_invariant.py` asserts **structurally** that no code path can
  produce a pooled balance. It is not a convention; it fails the build.

**If asked "would you ever hold real money?"** — the correct answer is that it
is an open decision requiring counsel and possibly state registration or a
regulated partner, and it is recorded as such (PRD §13, master plan Part D).
Do not answer it in the room. "No architecture makes an unregulated deposit
scheme lawful" is the line.

---

## Two more that come up

**"Is this financial advice?"** No, and the product is careful about it. Offers
are labelled partner promotions, not recommendations; the agent's system prompt
requires it to say so; and the agent is structurally incapable of choosing an
amount for the user (amount provenance — it may only propose a figure the user
literally typed). Nothing in the UI ranks or recommends an investment.

**"Where does the AI sit in the regulated path?"** Nowhere. The model can cause
an `ActionIntent` row to exist and nothing else. Every decision that has a
consequence — policy, limits, settlement, payout authorisation — is
deterministic code, and the orchestrator re-runs the policy engine itself
before every money tool regardless of what the model claims. This is worth
stating plainly to a financial-services audience: **the model is not in the
control path, and that is a design property you can test, not a promise.**

---

## What is deliberately deferred

Named with trigger conditions, because "we didn't do it" and "we decided not to
do it yet, and here's when we would" are very different answers:

| Deferred | Trigger |
|---|---|
| Consent manager, DPO, grievance officer, breach runbook, DPIA | First real user (DPDP substantive: May 2027) |
| Postgres, PITR, drilled RTO/RPO, multi-AZ | Real user funds or an uptime commitment |
| Fraud/collusion detection (deterministic signals only — never the LLM) | Real money, or the first gaming attempt |
| Third-party pen test, DAST in CI, agent-surface threat model | Before first real user |
| Data tiering and erasure workflows | DPDP substantive obligations, or storage cost |
| Secondary payment provider | A commercial decision, not an engineering one |

The full designs and rationale are in `CampusPool_Production_Readiness.md` §4.
