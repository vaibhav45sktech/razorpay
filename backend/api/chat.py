"""POST /api/chat — the one HTTP entry point into the agent loop (HLD s2.8/s4.1).

Approval is deliberately NOT here and never will be (PRD s5.4): granting a
REQUIRE_APPROVAL intent only ever happens through the structured
POST /api/intents/{id}/approve endpoint (backend/api/intents.py). No tool in
agent/tool_registry.py can grant an approval, so there is structurally no
path from chat phrasing to an approval, however insistently worded — this
endpoint doesn't need to defend against it because the capability to do it
does not exist anywhere the model can reach.
"""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from backend import observability
from backend.agent import orchestrator
from backend.models.db import get_session
from backend.services import state_service

router = APIRouter(prefix="/api", tags=["chat"])

#: The only roles a client is trusted to replay back as conversation history.
#: "tool" and "system" messages are synthesized by the orchestrator itself
#: each turn (fresh, verified state; fresh tool catalog) — accepting them
#: from the client would let it forge fake tool results or system rules
#: straight into the model's context.
_TRUSTED_HISTORY_ROLES = {"user", "assistant"}


class ChatHistoryMessage(BaseModel):
    role: Literal["user", "assistant"]
    content: str


class ChatRequest(BaseModel):
    # No auth in this prototype (matches ApprovalBody in api/intents.py): the
    # body names the acting user. A real deployment replaces this with a
    # session-derived user_id — orchestrator.run_agent_turn() already treats
    # user_id as server-injected and never accepts one from the model itself.
    user_id: str = Field(..., min_length=1)
    message: str = Field(..., min_length=1)
    history: list[ChatHistoryMessage] = Field(default_factory=list)

    @field_validator("user_id", "message", mode="before")
    @classmethod
    def _strip(cls, v):  # noqa: ANN001
        # A stray space around an id ("  usr_x ") is an easy mistake for a
        # client to make and a confusing 404 to debug (seen 2026-09-04).
        return v.strip() if isinstance(v, str) else v


class ChatResponse(BaseModel):
    reply: str
    steps: int
    exhausted: bool
    degraded: bool
    state: dict[str, Any]


@router.post("/chat")
@observability.limiter.limit("12/minute", key_func=observability._chat_key)
def chat(request: Request, body: ChatRequest, session: Session = Depends(get_session)) -> ChatResponse:
    """Rate limited PER USER, not per IP (Phase 8 item 1): every call is
    several inference passes against one local model, so this is the endpoint
    an abuser would target and the one where a handful of callers is a denial
    of service against every other student. `request` is in the signature
    because slowapi needs it; the handler itself does not use it."""
    history = [
        {"role": m.role, "content": m.content} for m in body.history if m.role in _TRUSTED_HISTORY_ROLES
    ]

    try:
        reply = orchestrator.run_agent_turn(session, body.user_id, body.message, history)
        session.commit()
    except state_service.UnknownUser:
        session.rollback()
        raise HTTPException(status_code=404, detail="unknown user")
    except OperationalError as exc:
        # The database itself was unavailable (locked by a sync client,
        # another process, a full disk). Nothing was committed. Say so with a
        # retryable status instead of a generic 500 — this is an outage of
        # the storage, not a bug in the turn.
        session.rollback()
        raise HTTPException(
            status_code=503,
            detail="database temporarily unavailable; nothing was executed - please retry",
            headers={"Retry-After": "2"},
        ) from exc
    except Exception:
        # Anything unexpected mid-turn must not leave a half-applied
        # financial write committed (Playbook A.4). Whatever guardrail
        # should have caught this instead is a bug to go fix, not something
        # to paper over here.
        session.rollback()
        raise

    return ChatResponse(
        reply=reply.text,
        steps=reply.steps,
        exhausted=reply.exhausted,
        degraded=reply.degraded,
        state=reply.state or {},
    )
