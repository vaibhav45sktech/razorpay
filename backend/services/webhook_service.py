"""Settlement from provider evidence — the authoritative channel (HLD s6.5).

Two callers, one set of rules:
  - POST /api/webhooks/razorpay   (payment.captured / payment.failed)
  - POST /api/checkout/verify     (the browser's fast path, re-verified
                                   against the Razorpay API, never trusted
                                   from the browser)

Both end in Phase 3's settle_success()/settle_failure(), unchanged. What this
module adds is the decision of WHICH of those to call, and the refusal to
call either when the evidence does not line up (unknown order, amount
mismatch, contradictory late events) - those open an ExceptionRecord.
"""

from __future__ import annotations

import hashlib
import json
import logging
from typing import Any

from sqlalchemy.orm import Session

from backend.models.entities import ActionIntent, AuditActor, ExceptionKind, IntentStatus, WebhookEvent
from backend.services import audit_service, exception_service
from backend.services import money_action_service as mas

logger = logging.getLogger("campuspool.webhooks")

S = IntentStatus


def _payment_entity(event: dict[str, Any]) -> dict[str, Any] | None:
    try:
        return event["payload"]["payment"]["entity"]
    except (KeyError, TypeError):
        return None


def apply_payment(session: Session, payment: dict[str, Any], *, channel: str,
                  actor: AuditActor = AuditActor.WEBHOOK) -> str:
    """Apply one authoritative payment record to its intent. Returns an
    outcome string (also written to the audit trail). Idempotent."""
    order_id = payment.get("order_id")
    payment_id = payment.get("id")
    status = payment.get("status")
    amount = payment.get("amount")

    intent = mas.get_by_provider_ref(session, order_id) if order_id else None
    if intent is None:
        exception_service.open(
            session, kind=ExceptionKind.WEBHOOK_FOR_UNKNOWN_ORDER,
            detail={"channel": channel, "order_id": order_id, "payment_id": payment_id, "status": status,
                    "amount": amount, "notes": payment.get("notes")},
        )
        return "unknown_order"

    if amount is not None and int(amount) != int(intent.amount_paise):
        exception_service.open(
            session, kind=ExceptionKind.UNKNOWN_PAYMENT_STATE, intent_id=intent.id, user_id=intent.user_id,
            detail={"channel": channel, "reason": "amount_mismatch", "provider_amount": amount,
                    "intent_amount": intent.amount_paise, "payment_id": payment_id},
        )
        return "amount_mismatch"

    if status == "captured":
        if intent.status is S.LEDGER_UPDATED:
            return "already_settled"
        if intent.status not in mas.SETTLEABLE_FROM:
            # captured evidence for an intent we never moved to EXECUTING
            # (e.g. paid outside our flow) - a human should look.
            exception_service.open(
                session, kind=ExceptionKind.UNKNOWN_PAYMENT_STATE, intent_id=intent.id, user_id=intent.user_id,
                detail={"channel": channel, "reason": f"captured_while_{intent.status.value}", "payment_id": payment_id},
            )
            return "captured_in_unexpected_state"
        mas.settle_success(session, intent, provider_evidence=payment,
                           source=f"razorpay_payment:{payment_id}", actor=actor)
        return "settled"

    if status == "failed":
        if intent.status in (S.LEDGER_UPDATED, S.SUCCESS, S.VERIFIED):
            # A late 'failed' for an earlier attempt on the same order, after a
            # later attempt was captured. Normal; the ledger is right. No-op.
            audit_service.write(session, actor=actor, action="late_failed_event_ignored", user_id=intent.user_id,
                                intent_id=intent.id, provider_result={"payment_id": payment_id})
            return "late_failed_ignored"
        if intent.status in (S.EXECUTING, S.UNKNOWN):
            mas.settle_failure(session, intent, provider_evidence=payment, actor=actor)
            return "failed"
        return "failed_noop"

    # created / authorized / refunded / anything else: not a settlement signal.
    audit_service.write(session, actor=actor, action=f"payment_status_noted:{status}", user_id=intent.user_id,
                        intent_id=intent.id, provider_result={"payment_id": payment_id})
    return f"noted:{status}"


def handle_webhook(session: Session, *, raw_body: bytes, event_id: str | None) -> tuple[int, dict[str, Any]]:
    """Signature already verified by the caller. Returns (http_status, body)."""
    body_sha = hashlib.sha256(raw_body).hexdigest()
    try:
        event = json.loads(raw_body)
    except json.JSONDecodeError:
        audit_service.write(session, actor=AuditActor.WEBHOOK, action="webhook_rejected:malformed_json")
        return 400, {"status": "malformed"}

    name = event.get("event", "")
    payment = _payment_entity(event)

    # Dedupe on Razorpay's event id (header). Missing header: fall back to a
    # body hash so a resend without the header is still recognised.
    key = event_id or f"sha256:{body_sha}"
    if session.get(WebhookEvent, key) is not None:
        audit_service.write(session, actor=AuditActor.WEBHOOK, action="webhook_duplicate_ignored",
                            provider_result={"event_id": key, "event": name})
        return 200, {"status": "already_processed"}

    if name in ("payment.captured", "payment.failed") and payment is not None:
        outcome = apply_payment(session, payment, channel=f"webhook:{name}")
    else:
        outcome = f"ignored_event:{name or 'unknown'}"
        audit_service.write(session, actor=AuditActor.WEBHOOK, action="webhook_event_ignored",
                            provider_result={"event": name})

    session.add(WebhookEvent(
        id=key, event=name or "unknown", order_id=(payment or {}).get("order_id"),
        payment_id=(payment or {}).get("id"), body_sha256=body_sha, outcome=outcome,
    ))
    session.flush()
    logger.info("webhook %s (%s) -> %s", key, name, outcome)
    return 200, {"status": "ok", "outcome": outcome}
