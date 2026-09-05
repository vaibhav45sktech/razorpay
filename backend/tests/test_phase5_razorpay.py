"""Phase 5 — Razorpay test mode, with the provider FAKED at the adapter
boundary (services/razorpay_adapter.py). No network, no keys. Covers:

- the adapter is the only SDK importer (Step 2)
- POST /api/intents/{id}/execute is idempotent (Step 3)
- /debug/* 404s with DEBUG=false (Step 3)
- checkout page has a strict CSP (Step 4)
- POST /api/checkout/verify trusts the API, not the browser (Step 5)
- webhooks: HMAC on raw body, dedupe, captured/failed/unknown (Step 6)
- the 8 chaos tests (Step 7) - each ends in a CORRECT or explicitly-EXCEPTION state
- reconciliation: sweeper, full run, integrity (Step 8)
"""

from __future__ import annotations

import hashlib
import hmac
import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from backend import config
from backend.main import app
from backend.models.db import get_session
from backend.models.entities import (
    ActionIntent, AuditActor, AuditEvent, ExceptionKind, ExceptionRecord, IntentStatus, LedgerEvent, User, WebhookEvent,
)
from backend.seed import demo_data
from backend.services import ledger_service, razorpay_adapter, reconciliation_service, webhook_service
from backend.services import money_action_service as mas

RUPEE = 100
S = IntentStatus
KEY_SECRET = "test_key_secret_not_real"
WEBHOOK_SECRET = "test_webhook_secret_not_real"


# ---------------------------------------------------------------------------
# A fake Razorpay: orders, payments, and knobs for chaos.
# ---------------------------------------------------------------------------


class FakeRazorpay:
    def __init__(self) -> None:
        self.orders: dict[str, dict] = {}
        self.payments: dict[str, dict] = {}
        self.create_calls = 0
        self.fail_create_with: Exception | None = None
        self.timeout_create_but_succeed = False  # the classic bug
        self.fail_fetch_with: Exception | None = None

    # --- adapter surface ---
    def create_order(self, intent):
        self.create_calls += 1
        if self.fail_create_with:
            raise self.fail_create_with
        oid = f"order_fake{self.create_calls:03d}"
        order = {"id": oid, "amount": intent.amount_paise, "currency": "INR",
                 "receipt": razorpay_adapter.receipt_for(intent), "status": "created"}
        self.orders[oid] = order
        if self.timeout_create_but_succeed:
            self.timeout_create_but_succeed = False
            raise razorpay_adapter.ProviderTimeout("create_order: ReadTimeout")
        return order

    def find_order_by_receipt(self, receipt):
        for o in self.orders.values():
            if o["receipt"] == receipt:
                return o
        return None

    def fetch_payment(self, payment_id):
        if self.fail_fetch_with:
            raise self.fail_fetch_with
        if payment_id not in self.payments:
            raise razorpay_adapter.ProviderError("fetch_payment: BadRequestError: not found")
        return self.payments[payment_id]

    def fetch_order_payments(self, order_id):
        return [p for p in self.payments.values() if p["order_id"] == order_id]

    def list_payments(self, from_ts, to_ts, *, count=100, skip=0):
        return list(self.payments.values())[skip: skip + count]

    # --- test helpers ---
    def pay(self, order_id: str, *, status: str = "captured", amount: int | None = None, pid: str | None = None) -> dict:
        pid = pid or f"pay_fake{len(self.payments) + 1:03d}"
        p = {"id": pid, "order_id": order_id, "status": status,
             "amount": amount if amount is not None else self.orders[order_id]["amount"], "currency": "INR",
             "method": "card", "captured": status == "captured"}
        self.payments[pid] = p
        return p


def checkout_sig(order_id: str, payment_id: str, secret: str = KEY_SECRET) -> str:
    return hmac.new(secret.encode(), f"{order_id}|{payment_id}".encode(), hashlib.sha256).hexdigest()


def webhook_body(event: str, payment: dict) -> bytes:
    return json.dumps({"event": event, "payload": {"payment": {"entity": payment}}}).encode()


def webhook_sig(raw: bytes, secret: str = WEBHOOK_SECRET) -> str:
    return hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()


@pytest.fixture()
def rzp(monkeypatch) -> FakeRazorpay:
    fake = FakeRazorpay()
    monkeypatch.setattr(config, "RAZORPAY_ENABLED", True)
    monkeypatch.setattr(config, "RAZORPAY_KEY_ID", "rzp_test_fake")
    monkeypatch.setattr(config, "RAZORPAY_KEY_SECRET", KEY_SECRET)
    monkeypatch.setattr(config, "RAZORPAY_WEBHOOK_SECRET", WEBHOOK_SECRET)
    for name in ("create_order", "find_order_by_receipt", "fetch_payment", "fetch_order_payments", "list_payments"):
        monkeypatch.setattr(razorpay_adapter, name, getattr(fake, name))
    return fake


@pytest.fixture()
def aarav(db) -> User:
    demo_data.seed_all(db)
    db.commit()
    return db.execute(select(User).where(User.name == demo_data.PRIMARY_DEMO_USER)).scalar_one()


@pytest.fixture()
def client(db):
    def _override():
        yield db
    app.dependency_overrides[get_session] = _override
    try:
        with TestClient(app) as c:
            yield c
    finally:
        app.dependency_overrides.clear()


def _allowed_intent(db, user: User, *, rupees: int = 300, purpose: str = "savings_goal:t") -> ActionIntent:
    r = mas.create(db, user_id=user.id, action="CONTRIBUTION", amount_paise=rupees * RUPEE, purpose=purpose,
                   actor=AuditActor.USER)
    db.commit()  # a real intent is committed by its own request before execute() is ever called
    intent = mas.get(db, r.as_dict()["intent_id"])
    assert intent.status is S.ALLOWED
    return intent


def _actions(db, user_id=None) -> list[str]:
    q = select(AuditEvent.action).order_by(AuditEvent.seq)
    if user_id:
        q = q.where(AuditEvent.user_id == user_id)
    return list(db.execute(q).scalars().all())


def _emergency(db, user_id: str) -> int:
    return ledger_service.get_balances(db, user_id)["emergency_savings"]


# ---------------------------------------------------------------------------
# Step 2 — one SDK importer
# ---------------------------------------------------------------------------


def test_exactly_one_module_imports_the_razorpay_sdk() -> None:
    root = Path(__file__).resolve().parents[1]
    importers = []
    for py in root.rglob("*.py"):
        if "tests" in py.parts or "__pycache__" in py.parts:
            continue
        text = py.read_text(encoding="utf-8")
        if any(line.strip().startswith(("import razorpay", "from razorpay")) for line in text.splitlines()):
            importers.append(py.relative_to(root).as_posix())
    assert importers == ["services/razorpay_adapter.py"], importers


def test_secret_never_appears_in_public_checkout_config(rzp) -> None:
    assert KEY_SECRET not in json.dumps(razorpay_adapter.public_checkout_config())
    assert razorpay_adapter.public_checkout_config()["key_id"] == "rzp_test_fake"


# ---------------------------------------------------------------------------
# Step 3 — execute: idempotent, ownership, state gate; debug routes gone
# ---------------------------------------------------------------------------


def test_execute_creates_one_order_and_is_idempotent(client, db, aarav, rzp) -> None:
    intent = _allowed_intent(db, aarav)
    r1 = client.post(f"/api/intents/{intent.id}/execute", json={"user_id": aarav.id})
    assert r1.status_code == 200, r1.text
    b1 = r1.json()
    assert b1["order_id"].startswith("order_fake") and b1["amount_paise"] == 300 * RUPEE
    assert b1["key_id"] == "rzp_test_fake" and "secret" not in json.dumps(b1).lower()
    assert b1["status"] == "EXECUTING" and b1["reused_existing_order"] is False

    r2 = client.post(f"/api/intents/{intent.id}/execute", json={"user_id": aarav.id})
    assert r2.status_code == 200
    assert r2.json()["order_id"] == b1["order_id"] and r2.json()["reused_existing_order"] is True
    assert rzp.create_calls == 1, "a second execute must never create a second order"


def test_execute_refuses_other_users_intent_and_unallowed_states(client, db, aarav, rzp) -> None:
    intent = _allowed_intent(db, aarav)
    assert client.post(f"/api/intents/{intent.id}/execute", json={"user_id": "usr_someone_else"}).status_code == 403
    big = mas.create(db, user_id=aarav.id, action="PURCHASE", amount_paise=600 * RUPEE, purpose="purchase:big",
                     actor=AuditActor.USER)
    assert big.as_dict()["status"] == "AWAITING_APPROVAL"
    assert client.post(f"/api/intents/{big.as_dict()['intent_id']}/execute", json={"user_id": aarav.id}).status_code == 409
    assert rzp.create_calls == 0, "neither refusal may reach the provider"


def test_execute_is_503_when_razorpay_not_configured(client, db, aarav, monkeypatch) -> None:
    monkeypatch.setattr(config, "RAZORPAY_ENABLED", False)
    intent = _allowed_intent(db, aarav)
    assert client.post(f"/api/intents/{intent.id}/execute", json={"user_id": aarav.id}).status_code == 503


def test_debug_routes_404_when_debug_is_off(client, db, aarav, monkeypatch) -> None:
    monkeypatch.setattr(config, "DEBUG", False)
    r = client.post("/debug/intents", json={"user_id": aarav.id, "action": "CONTRIBUTION", "amount_paise": 10000,
                                            "purpose": "savings_goal:x"})
    assert r.status_code == 404
    assert client.post("/debug/seed").status_code == 404


# ---------------------------------------------------------------------------
# Step 4 — checkout page: strict CSP, key id only
# ---------------------------------------------------------------------------


def test_checkout_page_has_strict_csp_and_no_secret(client, db, aarav, rzp) -> None:
    intent = _allowed_intent(db, aarav)
    r = client.get(f"/checkout/{intent.id}")
    assert r.status_code == 200
    csp = r.headers["content-security-policy"]
    assert "default-src 'none'" in csp
    assert "script-src 'self' 'unsafe-inline' https://checkout.razorpay.com" in csp
    assert "frame-src https://api.razorpay.com https://checkout.razorpay.com" in csp
    assert r.headers["x-frame-options"] == "DENY"
    assert "rzp_test_fake" in r.text and KEY_SECRET not in r.text
    assert "TEST MODE" in r.text


# ---------------------------------------------------------------------------
# Step 5 — checkout verify: signature, then the API's word
# ---------------------------------------------------------------------------


def test_checkout_verify_settles_only_from_fetched_captured_payment(client, db, aarav, rzp) -> None:
    before = _emergency(db, aarav.id)
    intent = _allowed_intent(db, aarav)
    oid = client.post(f"/api/intents/{intent.id}/execute", json={"user_id": aarav.id}).json()["order_id"]
    p = rzp.pay(oid, status="captured")

    r = client.post("/api/checkout/verify", json={"razorpay_order_id": oid, "razorpay_payment_id": p["id"],
                                                  "razorpay_signature": checkout_sig(oid, p["id"])})
    assert r.status_code == 200, r.text
    assert r.json()["ok"] is True and r.json()["intent_status"] == "LEDGER_UPDATED"
    assert _emergency(db, aarav.id) == before + 300 * RUPEE


def test_checkout_verify_with_valid_signature_but_uncaptured_payment_does_not_settle(client, db, aarav, rzp) -> None:
    """The browser can only ever get the server to LOOK; the API's status decides."""
    before = _emergency(db, aarav.id)
    intent = _allowed_intent(db, aarav)
    oid = client.post(f"/api/intents/{intent.id}/execute", json={"user_id": aarav.id}).json()["order_id"]
    p = rzp.pay(oid, status="authorized")
    r = client.post("/api/checkout/verify", json={"razorpay_order_id": oid, "razorpay_payment_id": p["id"],
                                                  "razorpay_signature": checkout_sig(oid, p["id"])})
    assert r.status_code == 200 and r.json()["ok"] is False and r.json()["intent_status"] == "EXECUTING"
    assert _emergency(db, aarav.id) == before


# ---------------------------------------------------------------------------
# Step 6/7 — webhooks + the 8 chaos tests
# ---------------------------------------------------------------------------


def _post_webhook(client, raw: bytes, *, sig: str | None = None, event_id: str | None = "evt_1"):
    headers = {"X-Razorpay-Signature": webhook_sig(raw) if sig is None else sig, "Content-Type": "application/json"}
    if event_id:
        headers["x-razorpay-event-id"] = event_id
    return client.post("/api/webhooks/razorpay", content=raw, headers=headers)


def test_webhook_captured_settles_and_next_state_shows_new_balance(client, db, aarav, rzp) -> None:
    before = _emergency(db, aarav.id)
    intent = _allowed_intent(db, aarav, rupees=500)
    oid = client.post(f"/api/intents/{intent.id}/execute", json={"user_id": aarav.id}).json()["order_id"]
    p = rzp.pay(oid)
    r = _post_webhook(client, webhook_body("payment.captured", p))
    assert r.status_code == 200 and r.json()["outcome"] == "settled"
    assert client.get(f"/api/state/{aarav.id}").json()["balances_paise"]["emergency_savings"] == before + 500 * RUPEE
    ev = db.execute(select(LedgerEvent).where(LedgerEvent.intent_id == intent.id)).scalar_one()
    assert ev.source == f"razorpay_payment:{p['id']}"


def test_chaos_2_duplicate_webhook_is_a_noop(client, db, aarav, rzp) -> None:
    before = _emergency(db, aarav.id)
    intent = _allowed_intent(db, aarav)
    oid = client.post(f"/api/intents/{intent.id}/execute", json={"user_id": aarav.id}).json()["order_id"]
    raw = webhook_body("payment.captured", rzp.pay(oid))
    assert _post_webhook(client, raw, event_id="evt_dup").json()["outcome"] == "settled"
    second = _post_webhook(client, raw, event_id="evt_dup")
    assert second.status_code == 200 and second.json()["status"] == "already_processed"
    assert _emergency(db, aarav.id) == before + 300 * RUPEE
    assert db.execute(select(LedgerEvent).where(LedgerEvent.intent_id == intent.id)).scalars().all().__len__() == 1
    assert "webhook_duplicate_ignored" in _actions(db)


def test_chaos_2b_same_event_without_header_is_recognised_by_body_hash(client, db, aarav, rzp) -> None:
    intent = _allowed_intent(db, aarav)
    oid = client.post(f"/api/intents/{intent.id}/execute", json={"user_id": aarav.id}).json()["order_id"]
    raw = webhook_body("payment.captured", rzp.pay(oid))
    assert _post_webhook(client, raw, event_id=None).json()["outcome"] == "settled"
    assert _post_webhook(client, raw, event_id=None).json()["status"] == "already_processed"


def test_chaos_3_failed_after_captured_is_ignored_ledger_right(client, db, aarav, rzp) -> None:
    before = _emergency(db, aarav.id)
    intent = _allowed_intent(db, aarav)
    oid = client.post(f"/api/intents/{intent.id}/execute", json={"user_id": aarav.id}).json()["order_id"]
    failed_attempt = rzp.pay(oid, status="failed", pid="pay_first_failed")
    captured = rzp.pay(oid, status="captured", pid="pay_second_ok")
    assert _post_webhook(client, webhook_body("payment.captured", captured), event_id="e1").json()["outcome"] == "settled"
    late = _post_webhook(client, webhook_body("payment.failed", failed_attempt), event_id="e2")
    assert late.status_code == 200 and late.json()["outcome"] == "late_failed_ignored"
    assert mas.get(db, intent.id).status is S.LEDGER_UPDATED
    assert _emergency(db, aarav.id) == before + 300 * RUPEE


def test_chaos_4_webhook_for_unknown_order_opens_exception_never_guesses(client, db, aarav, rzp) -> None:
    before = _emergency(db, aarav.id)
    ghost = {"id": "pay_ghost", "order_id": "order_never_ours", "status": "captured", "amount": 30000}
    r = _post_webhook(client, webhook_body("payment.captured", ghost))
    assert r.status_code == 200 and r.json()["outcome"] == "unknown_order"
    exc = db.execute(select(ExceptionRecord)).scalars().all()
    assert len(exc) == 1 and exc[0].kind is ExceptionKind.WEBHOOK_FOR_UNKNOWN_ORDER
    assert _emergency(db, aarav.id) == before
    assert db.execute(select(ActionIntent).where(ActionIntent.provider_ref == "order_never_ours")).scalar_one_or_none() is None


def test_chaos_5_forged_signature_is_400_audited_no_state_change(client, db, aarav, rzp) -> None:
    before = _emergency(db, aarav.id)
    intent = _allowed_intent(db, aarav)
    oid = client.post(f"/api/intents/{intent.id}/execute", json={"user_id": aarav.id}).json()["order_id"]
    raw = webhook_body("payment.captured", rzp.pay(oid))
    r = _post_webhook(client, raw, sig="deadbeef" * 8, event_id="evt_forged")
    assert r.status_code == 400
    assert "invalid_signature_rejected" in _actions(db)
    assert mas.get(db, intent.id).status is S.EXECUTING
    assert _emergency(db, aarav.id) == before
    assert db.get(WebhookEvent, "evt_forged") is None, "a forger must not be able to burn an event id"
    # and the same event with a real signature still works afterwards
    assert _post_webhook(client, raw, event_id="evt_forged").json()["outcome"] == "settled"


def test_chaos_6_tampered_checkout_callback_rejected_and_exception_opened(client, db, aarav, rzp) -> None:
    before = _emergency(db, aarav.id)
    intent = _allowed_intent(db, aarav)
    oid = client.post(f"/api/intents/{intent.id}/execute", json={"user_id": aarav.id}).json()["order_id"]
    p = rzp.pay(oid)
    r = client.post("/api/checkout/verify", json={"razorpay_order_id": oid, "razorpay_payment_id": p["id"],
                                                  "razorpay_signature": checkout_sig(oid, p["id"], secret="wrong")})
    assert r.status_code == 400
    exc = db.execute(select(ExceptionRecord)).scalars().one()
    assert exc.kind is ExceptionKind.INVALID_CHECKOUT_SIGNATURE and exc.intent_id == intent.id
    assert mas.get(db, intent.id).status is S.EXECUTING and _emergency(db, aarav.id) == before


def test_chaos_7_provider_5xx_on_create_order_leaves_intent_allowed(client, db, aarav, rzp) -> None:
    rzp.fail_create_with = razorpay_adapter.ProviderError("create_order: ServerError: 502")
    intent = _allowed_intent(db, aarav)
    r = client.post(f"/api/intents/{intent.id}/execute", json={"user_id": aarav.id})
    assert r.status_code == 502
    db.expire_all()
    assert mas.get(db, intent.id).status is S.ALLOWED and mas.get(db, intent.id).provider_ref is None
    rzp.fail_create_with = None
    assert client.post(f"/api/intents/{intent.id}/execute", json={"user_id": aarav.id}).status_code == 200


def test_chaos_8_timeout_after_order_was_created_adopts_it_no_duplicate(client, db, aarav, rzp) -> None:
    """The classic payments bug: failed on our side, succeeded on theirs."""
    rzp.timeout_create_but_succeed = True
    intent = _allowed_intent(db, aarav)
    r1 = client.post(f"/api/intents/{intent.id}/execute", json={"user_id": aarav.id})
    assert r1.status_code == 503 and r1.headers.get("retry-after")
    db.expire_all()
    assert mas.get(db, intent.id).status is S.ALLOWED, "no transition on an unknown outcome"
    assert "provider_timeout:create_order" in _actions(db, aarav.id)
    assert len(rzp.orders) == 1, "the order DID get created at Razorpay"

    r2 = client.post(f"/api/intents/{intent.id}/execute", json={"user_id": aarav.id})
    assert r2.status_code == 200
    assert r2.json()["order_id"] == next(iter(rzp.orders)) and r2.json()["reused_existing_order"] is True
    assert len(rzp.orders) == 1 and rzp.create_calls == 1, "adopted by receipt; never a second order"
    assert "order_adopted_by_receipt" in _actions(db, aarav.id)
    assert mas.get(db, intent.id).status is S.EXECUTING


def test_chaos_1_delayed_webhook_is_settled_by_the_sweeper(db, aarav, rzp) -> None:
    before = _emergency(db, aarav.id)
    intent = _allowed_intent(db, aarav)
    mas.execute(db, intent_id=intent.id, user_id=aarav.id)
    rzp.pay(intent.provider_ref)  # captured at Razorpay; our webhook never arrives
    # Too fresh: the sweeper leaves it alone.
    fresh = reconciliation_service.sweep_stuck_intents(db, now=datetime.now(timezone.utc))
    assert fresh.checked == 0 and mas.get(db, intent.id).status is S.EXECUTING
    # Past the window: settled from the authoritative status.
    later = datetime.now(timezone.utc) + timedelta(seconds=config.RECONCILE_STUCK_AFTER_SECONDS + 5)
    rep = reconciliation_service.sweep_stuck_intents(db, now=later)
    assert rep.settled == 1 and mas.get(db, intent.id).status is S.LEDGER_UPDATED
    assert _emergency(db, aarav.id) == before + 300 * RUPEE
    ev = db.execute(select(LedgerEvent).where(LedgerEvent.intent_id == intent.id)).scalar_one()
    assert ev.source.startswith("razorpay_payment:")


# ---------------------------------------------------------------------------
# Step 8 — reconciliation: escalation, full run, integrity
# ---------------------------------------------------------------------------


def test_sweeper_escalates_to_exception_when_nothing_decisive_arrives(db, aarav, rzp) -> None:
    intent = _allowed_intent(db, aarav)
    mas.execute(db, intent_id=intent.id, user_id=aarav.id)
    # No payment at all, ever.
    much_later = datetime.now(timezone.utc) + timedelta(seconds=config.RECONCILE_EXCEPTION_AFTER_SECONDS + 5)
    rep = reconciliation_service.sweep_stuck_intents(db, now=much_later)
    assert rep.escalated == 1
    assert mas.get(db, intent.id).status is S.EXCEPTION
    exc = db.execute(select(ExceptionRecord)).scalars().one()
    assert exc.kind is ExceptionKind.RECONCILIATION_TIMEOUT and exc.intent_id == intent.id
    # Still counted as pending money (conservative), and settleable later by a human-confirmed payment.
    assert S.EXCEPTION in mas.PENDING_STATUSES and S.EXCEPTION in mas.SETTLEABLE_FROM


def test_sweeper_fails_intent_when_every_attempt_failed(db, aarav, rzp) -> None:
    before = _emergency(db, aarav.id)
    intent = _allowed_intent(db, aarav)
    mas.execute(db, intent_id=intent.id, user_id=aarav.id)
    rzp.pay(intent.provider_ref, status="failed")
    much_later = datetime.now(timezone.utc) + timedelta(seconds=config.RECONCILE_EXCEPTION_AFTER_SECONDS + 5)
    rep = reconciliation_service.sweep_stuck_intents(db, now=much_later)
    assert rep.failed == 1 and mas.get(db, intent.id).status is S.CLOSED
    assert _emergency(db, aarav.id) == before


def test_full_reconciliation_reports_three_classes_and_opens_exceptions(db, aarav, rzp) -> None:
    # Settled properly:
    good = _allowed_intent(db, aarav, purpose="savings_goal:good")
    mas.execute(db, intent_id=good.id, user_id=aarav.id)
    webhook_service.apply_payment(db, rzp.pay(good.provider_ref), channel="test")
    # They have, we don't (unknown order):
    rzp.orders["order_theirs"] = {"id": "order_theirs", "amount": 12300, "receipt": "x"}
    rzp.pay("order_theirs")
    # We think settled, they don't: fake a settled intent whose payment vanished at the provider.
    lonely = _allowed_intent(db, aarav, rupees=200, purpose="savings_goal:lonely")
    mas.execute(db, intent_id=lonely.id, user_id=aarav.id)
    vanished = rzp.pay(lonely.provider_ref)
    webhook_service.apply_payment(db, vanished, channel="test")
    del rzp.payments[vanished["id"]]
    # Amount mismatch: settled at 300 but provider now says 250.
    mism = _allowed_intent(db, aarav, purpose="savings_goal:mism")
    mas.execute(db, intent_id=mism.id, user_id=aarav.id)
    pm = rzp.pay(mism.provider_ref)
    webhook_service.apply_payment(db, pm, channel="test")
    pm["amount"] = 25000
    db.flush()

    rep = reconciliation_service.full_reconciliation(db, since=datetime.now(timezone.utc) - timedelta(days=1))
    assert [x["order_id"] for x in rep.they_have_we_dont] == ["order_theirs"]
    assert [x["intent_id"] for x in rep.we_think_settled_they_dont] == [lonely.id]
    assert [x["intent_id"] for x in rep.amount_mismatches] == [mism.id]
    assert rep.exceptions_opened == 3
    # Nothing auto-corrected: statuses and ledger untouched by the report.
    assert mas.get(db, lonely.id).status is S.LEDGER_UPDATED and mas.get(db, mism.id).status is S.LEDGER_UPDATED


def test_ledger_integrity_holds_after_real_settlements(db, aarav, rzp) -> None:
    intent = _allowed_intent(db, aarav)
    mas.execute(db, intent_id=intent.id, user_id=aarav.id)
    webhook_service.apply_payment(db, rzp.pay(intent.provider_ref), channel="test")
    db.commit()
    rep = reconciliation_service.ledger_integrity(db)
    assert rep.ok, rep.as_dict()
    assert rep.total_in_ledgers_paise == rep.total_in_events_paise


def test_reconcile_cli_integrity_runs(tmp_path) -> None:
    env = {"DATABASE_URL": f"sqlite:///{(tmp_path / 'r.db').as_posix()}", "PATH": "", "PYTHONPATH": str(Path(__file__).resolve().parents[2])}
    import os
    env["PATH"] = os.environ.get("PATH", "")
    out = subprocess.run([sys.executable, "-m", "backend.reconcile", "integrity"], capture_output=True, text=True,
                         env={**os.environ, **env}, cwd=Path(__file__).resolve().parents[2])
    assert out.returncode == 0, out.stderr
    assert '"ok": true' in out.stdout
