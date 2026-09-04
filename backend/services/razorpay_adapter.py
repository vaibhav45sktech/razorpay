"""razorpay_adapter — THE ONLY MODULE THAT IMPORTS THE `razorpay` SDK.

Enforced by tests/test_razorpay_adapter.py (a grep), per master build plan
Phase 5 Step 2 and Production Readiness s4.11 (vendor lock-in: swapping the
provider means rewriting this file and nothing else).

Everything here is a thin, typed wrapper that:
- speaks paise (our unit and Razorpay's), never floats;
- raises exactly two exception types the rest of the codebase can reason
  about: ProviderError (Razorpay said no / unexpected answer) and
  ProviderTimeout (we do not know whether the call took effect — the
  dangerous one, handled by receipt lookup + reconciliation);
- never logs the key secret, and never returns it.

Tests never touch this module's network functions; they monkeypatch them.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
from typing import Any

import razorpay  # noqa: F401  (the one and only import - see module docstring)

from backend import config
from backend.models.entities import ActionIntent

logger = logging.getLogger("campuspool.razorpay")

#: Razorpay's Checkout.js — the only third-party script the checkout page loads.
CHECKOUT_JS_URL = "https://checkout.razorpay.com/v1/checkout.js"


class ProviderError(RuntimeError):
    """Razorpay returned an error or something we cannot interpret."""


class ProviderTimeout(ProviderError):
    """The request did not complete. The call MAY have taken effect on
    Razorpay's side — callers must not assume either way."""


_client: Any = None


def enabled() -> bool:
    return config.RAZORPAY_ENABLED


def _get_client():
    global _client
    if not config.RAZORPAY_ENABLED:
        raise ProviderError("Razorpay is not configured (RAZORPAY_KEY_ID/RAZORPAY_KEY_SECRET missing)")
    if _client is None:
        _client = razorpay.Client(auth=(config.RAZORPAY_KEY_ID, config.RAZORPAY_KEY_SECRET))
        _client.set_app_details({"title": "CampusPool (DEMO, TEST MODE)", "version": "0.5"})
    return _client


def _wrap(call, what: str):
    """Run one SDK call; translate its failures into our two exceptions."""
    try:
        return call()
    except Exception as exc:  # noqa: BLE001 - the SDK raises a zoo; we classify, not swallow
        name = type(exc).__name__
        text = str(exc)
        if "timeout" in name.lower() or "timed out" in text.lower() or "Timeout" in name:
            logger.warning("razorpay %s: TIMEOUT (%s)", what, name)
            raise ProviderTimeout(f"{what}: {name}") from exc
        logger.warning("razorpay %s: error %s: %s", what, name, text[:200])
        raise ProviderError(f"{what}: {name}: {text[:200]}") from exc


def receipt_for(intent: ActionIntent) -> str:
    """The receipt ties Razorpay's order to our intent; max 40 chars."""
    return intent.client_ref[:40]


def create_order(intent: ActionIntent) -> dict[str, Any]:
    """HLD s6.3. Amount in paise, our unit."""
    client = _get_client()
    payload = {
        "amount": int(intent.amount_paise),
        "currency": config.CURRENCY,
        "receipt": receipt_for(intent),
        "notes": {"intent_id": intent.id, "user_id": intent.user_id, "demo": "SYNTHETIC / TEST MODE"},
    }
    order = _wrap(lambda: client.order.create(payload), "create_order")
    logger.info("razorpay order %s created for intent %s (%d paise)", order.get("id"), intent.id, intent.amount_paise)
    return order


def find_order_by_receipt(receipt: str) -> dict[str, Any] | None:
    """Did an earlier create_order that timed out on our side actually
    succeed on theirs? Razorpay lets us list orders by receipt."""
    client = _get_client()
    res = _wrap(lambda: client.order.all({"receipt": receipt, "count": 5}), "find_order_by_receipt")
    items = res.get("items") or []
    return items[0] if items else None


def fetch_payment(payment_id: str) -> dict[str, Any]:
    """Authoritative status: created / authorized / captured / failed / refunded."""
    client = _get_client()
    return _wrap(lambda: client.payment.fetch(payment_id), "fetch_payment")


def fetch_order_payments(order_id: str) -> list[dict[str, Any]]:
    """All payment attempts against an order (reconciliation uses this)."""
    client = _get_client()
    res = _wrap(lambda: client.order.payments(order_id), "fetch_order_payments")
    return list(res.get("items") or [])


def list_payments(from_ts: int, to_ts: int, *, count: int = 100, skip: int = 0) -> list[dict[str, Any]]:
    """Payments in a period (daily full reconciliation)."""
    client = _get_client()
    res = _wrap(
        lambda: client.payment.all({"from": from_ts, "to": to_ts, "count": count, "skip": skip}), "list_payments"
    )
    return list(res.get("items") or [])


def verify_checkout_signature(order_id: str, payment_id: str, signature: str) -> bool:
    """HMAC-SHA256(order_id|payment_id, key_secret) — computed here, constant-
    time compared. We do not call the SDK's helper so that this function has
    no network or client-state dependency and is trivially testable."""
    if not (config.RAZORPAY_KEY_SECRET and order_id and payment_id and signature):
        return False
    expected = hmac.new(
        config.RAZORPAY_KEY_SECRET.encode(), f"{order_id}|{payment_id}".encode(), hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, signature)


def verify_webhook_signature(raw_body: bytes, signature: str) -> bool:
    """HMAC-SHA256 over the RAW request body with the webhook secret (a
    different secret from the key secret). Guardrail 6."""
    if not (config.RAZORPAY_WEBHOOK_SECRET and signature):
        return False
    expected = hmac.new(config.RAZORPAY_WEBHOOK_SECRET.encode(), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


def public_checkout_config() -> dict[str, Any]:
    """What the browser is allowed to know. The key ID is public by design;
    the secret never appears in any response."""
    return {"key_id": config.RAZORPAY_KEY_ID, "currency": config.CURRENCY, "checkout_js": CHECKOUT_JS_URL}
