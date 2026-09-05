"""Checkout: the minimal test page (GET /checkout/{intent_id}) and the
browser's fast-path confirmation (POST /api/checkout/verify).

PCI note (master plan Phase 5 Step 4, decision recorded in the plan's Part
D2): embedded Checkout.js with a strict Content-Security-Policy. Card data
never touches this server — it is entered inside Razorpay's iframe. SRI on
checkout.js is deliberately NOT used: Razorpay ships that script without a
published hash and updates it in place, so a pinned hash would break checkout
on their next deploy without any change on our side. CSP's script-src
allowlist is the control we can actually keep true.
"""

from __future__ import annotations

import html

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from backend import config
from backend.models.db import get_session
from backend.models.entities import AuditActor, ExceptionKind
from backend.services import audit_service, exception_service, razorpay_adapter, webhook_service
from backend.services import money_action_service as mas

router = APIRouter(tags=["checkout"])

CSP = (
    "default-src 'none'; "
    "script-src 'self' 'unsafe-inline' https://checkout.razorpay.com; "
    "frame-src https://api.razorpay.com https://checkout.razorpay.com; "
    "connect-src 'self' https://api.razorpay.com https://lumberjack.razorpay.com; "
    "img-src 'self' data: https://*.razorpay.com; "
    "style-src 'self' 'unsafe-inline'; "
    "base-uri 'none'; form-action 'self'"
)

_PAGE = """<!doctype html><html><head><meta charset="utf-8"><title>CampusPool checkout (DEMO)</title>
<style>body{{font-family:system-ui;margin:2rem;max-width:34rem}} .n{{color:#b00;font-weight:600}} pre{{background:#f4f4f4;padding:.6rem}}</style>
</head><body>
<h2>CampusPool — Test Mode checkout</h2>
<p class="n">ALL DATA IS SYNTHETIC. This is Razorpay TEST MODE: no real money exists anywhere.</p>
<p>Intent <code>{intent_id}</code> · {kind} · <strong>₹{rupees}</strong> · status <code>{status}</code></p>
<button id="pay" {disabled}>Pay ₹{rupees} (test)</button>
<pre id="out">waiting…</pre>
<script src="{checkout_js}"></script>
<script>
const out = document.getElementById('out');
document.getElementById('pay').onclick = async () => {{
  out.textContent = 'creating order…';
  const r = await fetch('/api/intents/{intent_id}/execute', {{method:'POST', headers:{{'Content-Type':'application/json'}},
    body: JSON.stringify({{user_id: '{user_id}'}})}});
  const j = await r.json();
  if (!r.ok) {{ out.textContent = 'execute failed: ' + JSON.stringify(j); return; }}
  out.textContent = 'order ' + j.order_id + ' — opening checkout…';
  const rzp = new Razorpay({{
    key: '{key_id}', order_id: j.order_id, amount: j.amount_paise, currency: j.currency,
    name: 'CampusPool (DEMO — Test Mode)', description: '{kind} · synthetic demo',
    handler: async (resp) => {{
      out.textContent = 'verifying with server…';
      const v = await fetch('/api/checkout/verify', {{method:'POST', headers:{{'Content-Type':'application/json'}}, body: JSON.stringify(resp)}});
      out.textContent = 'verify → ' + v.status + '\\n' + JSON.stringify(await v.json(), null, 2);
    }},
    modal: {{ ondismiss: () => {{ out.textContent = 'checkout closed without paying (the webhook/reconciliation will still settle a completed payment)'; }} }}
  }});
  rzp.on('payment.failed', (e) => {{ out.textContent = 'payment.failed (browser event) → ' + JSON.stringify(e.error, null, 2) + '\\nThe server decides from Razorpay, not from this message.'; }});
  rzp.open();
}};
</script></body></html>"""


@router.get("/checkout/{intent_id}", response_class=HTMLResponse)
def checkout_page(intent_id: str, session: Session = Depends(get_session)) -> HTMLResponse:
    if not razorpay_adapter.enabled():
        raise HTTPException(status_code=503, detail="Razorpay test mode is not configured on this server")
    try:
        intent = mas.get(session, intent_id)
    except mas.IntentNotFound:
        raise HTTPException(status_code=404, detail="unknown intent")
    cfg = razorpay_adapter.public_checkout_config()
    payable = intent.status.value in ("ALLOWED", "APPROVED", "EXECUTING")
    page = _PAGE.format(
        intent_id=html.escape(intent.id), user_id=html.escape(intent.user_id), kind=html.escape(intent.type.value),
        rupees=f"{intent.amount_paise / 100:,.2f}", status=html.escape(intent.status.value),
        disabled="" if payable else "disabled", checkout_js=cfg["checkout_js"], key_id=html.escape(cfg["key_id"] or ""),
    )
    return HTMLResponse(page, headers={"Content-Security-Policy": CSP, "X-Frame-Options": "DENY",
                                       "Referrer-Policy": "no-referrer"})


class VerifyBody(BaseModel):
    razorpay_order_id: str = Field(..., min_length=1)
    razorpay_payment_id: str = Field(..., min_length=1)
    razorpay_signature: str = Field(..., min_length=1)


@router.post("/api/checkout/verify")
def verify_checkout(body: VerifyBody, session: Session = Depends(get_session)) -> dict:
    """Fast path. Signature first; then the payment is FETCHED from Razorpay -
    the browser's claim is never the evidence, the API's record is."""
    if not razorpay_adapter.verify_checkout_signature(body.razorpay_order_id, body.razorpay_payment_id,
                                                       body.razorpay_signature):
        intent = mas.get_by_provider_ref(session, body.razorpay_order_id)
        exception_service.open(
            session, kind=ExceptionKind.INVALID_CHECKOUT_SIGNATURE,
            intent_id=intent.id if intent else None, user_id=intent.user_id if intent else None,
            detail={"order_id": body.razorpay_order_id, "payment_id": body.razorpay_payment_id},
        )
        session.commit()
        raise HTTPException(status_code=400, detail="invalid checkout signature")

    try:
        payment = razorpay_adapter.fetch_payment(body.razorpay_payment_id)
    except razorpay_adapter.ProviderError as exc:
        audit_service.write(session, actor=AuditActor.BACKEND, action="checkout_verify:provider_error",
                            provider_result={"error": str(exc)})
        session.commit()
        raise HTTPException(status_code=502, detail="could not confirm payment with Razorpay; it will be reconciled")

    if payment.get("order_id") != body.razorpay_order_id:
        exception_service.open(session, kind=ExceptionKind.INVALID_CHECKOUT_SIGNATURE,
                               detail={"reason": "payment_belongs_to_other_order", **body.model_dump()})
        session.commit()
        raise HTTPException(status_code=400, detail="payment does not belong to this order")

    outcome = webhook_service.apply_payment(session, payment, channel="checkout_verify", actor=AuditActor.BACKEND)
    session.commit()
    intent = mas.get_by_provider_ref(session, body.razorpay_order_id)
    return {"ok": outcome in ("settled", "already_settled"), "outcome": outcome,
            "payment_status": payment.get("status"), "intent_status": intent.status.value if intent else None}
