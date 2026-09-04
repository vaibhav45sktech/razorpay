"""POST /api/webhooks/razorpay — raw-body HMAC first, everything else second."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request, Response
from sqlalchemy.orm import Session

from backend.models.db import get_session
from backend.models.entities import AuditActor
from backend.services import audit_service, razorpay_adapter, webhook_service

router = APIRouter(prefix="/api/webhooks", tags=["webhooks"])


@router.post("/razorpay")
async def razorpay_webhook(request: Request, session: Session = Depends(get_session)) -> Response:
    raw = await request.body()  # RAW bytes: never re-serialise before verifying
    signature = request.headers.get("X-Razorpay-Signature", "")
    event_id = request.headers.get("x-razorpay-event-id")

    if not razorpay_adapter.verify_webhook_signature(raw, signature):
        # Guardrail 6: refused, audited, nothing else touched. 400 so Razorpay
        # does not treat it as delivered.
        audit_service.write(session, actor=AuditActor.WEBHOOK, action="invalid_signature_rejected",
                            provider_result={"event_id": event_id, "body_bytes": len(raw)})
        session.commit()
        return Response(status_code=400, content='{"status":"invalid_signature"}', media_type="application/json")

    status, body = webhook_service.handle_webhook(session, raw_body=raw, event_id=event_id)
    session.commit()
    import json

    return Response(status_code=status, content=json.dumps(body), media_type="application/json")
