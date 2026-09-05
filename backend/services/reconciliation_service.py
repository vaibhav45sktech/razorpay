"""Reconciliation — the answer to "the webhook never came" (HLD s6.5,
master plan Phase 5 Step 8, Production Readiness s3.10).

Three jobs, all read Razorpay and write only through Phase 3's settlement
functions or an ExceptionRecord. NOTHING here auto-corrects a mismatch:
silently fixing a financial discrepancy turns a detectable problem into an
undetectable one, so every discrepancy becomes an exception for a human.

  sweep_stuck_intents(): every RECONCILE_INTERVAL_SECONDS. Intents in
      EXECUTING (or UNKNOWN) older than RECONCILE_STUCK_AFTER_SECONDS ->
      fetch the order's payments -> settle success / failure from the
      authoritative status. Still nothing decisive after
      RECONCILE_EXCEPTION_AFTER_SECONDS -> UNKNOWN -> EXCEPTION + record.
  full_reconciliation(): daily / on demand. Razorpay's payments for a period
      vs our intents, both directions, three classes of discrepancy.
  ledger_integrity(): audit chain intact, derived balances recompute, and
      the Phase 3 pool invariant (all money attributable to individual
      ledgers) still holds.
"""

from __future__ import annotations

import dataclasses
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from backend import config
from backend.models.entities import ActionIntent, ExceptionKind, IntentStatus, LedgerEvent, User
from backend.services import audit_service, exception_service, ledger_service, razorpay_adapter
from backend.services import money_action_service as mas

logger = logging.getLogger("campuspool.reconcile")
S = IntentStatus


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


@dataclasses.dataclass
class SweepReport:
    checked: int = 0
    settled: int = 0
    failed: int = 0
    still_pending: int = 0
    escalated: int = 0
    provider_errors: int = 0
    details: list[dict[str, Any]] = dataclasses.field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


def sweep_stuck_intents(session: Session, *, now: datetime | None = None) -> SweepReport:
    now = now or datetime.now(timezone.utc)
    stuck_after = now - timedelta(seconds=config.RECONCILE_STUCK_AFTER_SECONDS)
    escalate_after = now - timedelta(seconds=config.RECONCILE_EXCEPTION_AFTER_SECONDS)
    report = SweepReport()

    candidates = session.execute(
        select(ActionIntent).where(ActionIntent.status.in_([S.EXECUTING, S.UNKNOWN]))
    ).scalars().all()

    for intent in candidates:
        if _aware(intent.updated_at) > stuck_after:
            continue  # give the webhook its chance first
        report.checked += 1
        if not intent.provider_ref:
            # EXECUTING without an order id cannot happen through execute();
            # if it ever does, it is exactly an exception.
            _escalate(session, intent, reason="executing_without_provider_ref", report=report)
            continue
        try:
            payments = razorpay_adapter.fetch_order_payments(intent.provider_ref)
        except razorpay_adapter.ProviderError as exc:
            report.provider_errors += 1
            audit_service.write(session, actor=mas.AuditActor.SYSTEM, action="reconcile:provider_error",
                                user_id=intent.user_id, intent_id=intent.id, provider_result={"error": str(exc)})
            continue

        captured = [p for p in payments if p.get("status") == "captured"]
        failed = [p for p in payments if p.get("status") == "failed"]
        if captured:
            p = captured[0]
            if int(p.get("amount", intent.amount_paise)) != int(intent.amount_paise):
                _escalate(session, intent, reason="amount_mismatch", report=report,
                          extra={"provider_amount": p.get("amount")})
                continue
            mas.settle_success(session, intent, provider_evidence={**p, "via": "reconciliation"},
                               source=f"razorpay_payment:{p.get('id')}", actor=mas.AuditActor.SYSTEM)
            report.settled += 1
            report.details.append({"intent_id": intent.id, "outcome": "settled", "payment_id": p.get("id")})
        elif failed and len(failed) == len(payments) and _aware(intent.updated_at) <= escalate_after:
            # Every attempt failed and the window has passed: nobody is coming.
            mas.settle_failure(session, intent, provider_evidence={**failed[-1], "via": "reconciliation"},
                               actor=mas.AuditActor.SYSTEM)
            report.failed += 1
            report.details.append({"intent_id": intent.id, "outcome": "failed"})
        elif _aware(intent.updated_at) <= escalate_after:
            _escalate(session, intent, reason="no_decisive_payment_status", report=report,
                      extra={"payment_statuses": [p.get("status") for p in payments]})
        else:
            report.still_pending += 1
            report.details.append({"intent_id": intent.id, "outcome": "still_pending",
                                   "payment_statuses": [p.get("status") for p in payments]})

    session.flush()
    if report.checked:
        logger.info("reconcile sweep: %s", report.as_dict())
    return report


def _escalate(session: Session, intent: ActionIntent, *, reason: str, report: SweepReport,
              extra: dict[str, Any] | None = None) -> None:
    evidence = {"reason": reason, **(extra or {})}
    if intent.status is S.EXECUTING:
        mas.mark_unknown(session, intent, evidence=evidence)
    if intent.status is S.UNKNOWN:
        mas.escalate_exception(session, intent, evidence=evidence)
    exception_service.open(session, kind=ExceptionKind.RECONCILIATION_TIMEOUT, intent_id=intent.id,
                           user_id=intent.user_id, detail={**evidence, "provider_ref": intent.provider_ref})
    report.escalated += 1
    report.details.append({"intent_id": intent.id, "outcome": "escalated", **evidence})


@dataclasses.dataclass
class FullReconciliationReport:
    period_from: str
    period_to: str
    provider_payments: int = 0
    our_settled_intents: int = 0
    they_have_we_dont: list[dict[str, Any]] = dataclasses.field(default_factory=list)
    we_think_settled_they_dont: list[dict[str, Any]] = dataclasses.field(default_factory=list)
    amount_mismatches: list[dict[str, Any]] = dataclasses.field(default_factory=list)
    exceptions_opened: int = 0

    def as_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


def full_reconciliation(session: Session, *, since: datetime, until: datetime | None = None) -> FullReconciliationReport:
    until = until or datetime.now(timezone.utc)
    report = FullReconciliationReport(period_from=since.isoformat(), period_to=until.isoformat())

    provider: list[dict[str, Any]] = []
    skip = 0
    while True:
        page = razorpay_adapter.list_payments(int(since.timestamp()), int(until.timestamp()), count=100, skip=skip)
        provider.extend(page)
        if len(page) < 100:
            break
        skip += 100
    captured_by_order = {p["order_id"]: p for p in provider if p.get("status") == "captured" and p.get("order_id")}
    report.provider_payments = len(provider)

    ours = session.execute(
        select(ActionIntent).where(ActionIntent.provider_ref.is_not(None), ActionIntent.created_at >= since)
    ).scalars().all()
    ours_by_order = {i.provider_ref: i for i in ours}
    settled = {i.provider_ref: i for i in ours if i.status is S.LEDGER_UPDATED}
    report.our_settled_intents = len(settled)

    # 1. They captured a payment for an order we have no settled intent for.
    for order_id, p in captured_by_order.items():
        intent = ours_by_order.get(order_id)
        if intent is None or intent.status is not S.LEDGER_UPDATED:
            item = {"order_id": order_id, "payment_id": p.get("id"), "amount": p.get("amount"),
                    "our_status": intent.status.value if intent else None}
            report.they_have_we_dont.append(item)
            exception_service.open(session, kind=ExceptionKind.UNKNOWN_PAYMENT_STATE,
                                   intent_id=intent.id if intent else None, user_id=intent.user_id if intent else None,
                                   detail={"class": "they_have_we_dont", **item})
            report.exceptions_opened += 1

    # 2. We think it settled; they show no captured payment.
    for order_id, intent in settled.items():
        if order_id not in captured_by_order:
            item = {"order_id": order_id, "intent_id": intent.id, "amount": intent.amount_paise}
            report.we_think_settled_they_dont.append(item)
            exception_service.open(session, kind=ExceptionKind.UNKNOWN_PAYMENT_STATE, intent_id=intent.id,
                                   user_id=intent.user_id, detail={"class": "we_think_settled_they_dont", **item})
            report.exceptions_opened += 1

    # 3. Both agree it happened; amounts differ.
    for order_id, intent in settled.items():
        p = captured_by_order.get(order_id)
        if p is not None and int(p.get("amount", -1)) != int(intent.amount_paise):
            item = {"order_id": order_id, "intent_id": intent.id, "ours": intent.amount_paise, "theirs": p.get("amount")}
            report.amount_mismatches.append(item)
            exception_service.open(session, kind=ExceptionKind.UNKNOWN_PAYMENT_STATE, intent_id=intent.id,
                                   user_id=intent.user_id, detail={"class": "amount_mismatch", **item})
            report.exceptions_opened += 1

    audit_service.write(session, actor=mas.AuditActor.SYSTEM, action="reconcile:full_run",
                        provider_result={k: v for k, v in report.as_dict().items() if not isinstance(v, list)})
    session.flush()
    return report


@dataclasses.dataclass
class IntegrityReport:
    audit_chain_ok: bool
    audit_chain_checked: int
    audit_chain_reason: str | None
    balances_recompute_ok: bool
    pool_invariant_ok: bool
    total_in_ledgers_paise: int
    total_in_events_paise: int

    @property
    def ok(self) -> bool:
        return self.audit_chain_ok and self.balances_recompute_ok and self.pool_invariant_ok

    def as_dict(self) -> dict[str, Any]:
        return {**dataclasses.asdict(self), "ok": self.ok}


def ledger_integrity(session: Session) -> IntegrityReport:
    chain = audit_service.verify_chain(session)
    users = session.execute(select(User)).scalars().all()

    # Derived balances recompute: get_balances (what the app shows) must equal
    # the raw per-bucket sums of the append-only events.
    recompute_ok = True
    total_in_ledgers = 0
    for u in users:
        raw = ledger_service.get_raw_bucket_totals(session, u.id)
        total_in_ledgers += sum(raw.values())
        shown = ledger_service.get_balances(session, u.id)
        for bucket, paise in raw.items():
            if bucket in shown and shown[bucket] != paise:
                recompute_ok = False

    total_in_events = int(session.execute(select(func.coalesce(func.sum(LedgerEvent.amount_paise), 0))).scalar_one())
    pool_ok = total_in_ledgers == total_in_events  # every paise belongs to some individual ledger

    return IntegrityReport(
        audit_chain_ok=chain.ok, audit_chain_checked=chain.checked, audit_chain_reason=chain.reason,
        balances_recompute_ok=recompute_ok, pool_invariant_ok=pool_ok,
        total_in_ledgers_paise=total_in_ledgers, total_in_events_paise=total_in_events,
    )
