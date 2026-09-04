"""get_pool_status — the user's own view of their community pool cycle.

Reuses pool_service.cycle_summary exactly, the same function GET /api/state
calls, so the agent's numbers and the UI's numbers can never diverge (HLD
s2.2). This never reports a pooled balance because there is none: PRD s4.1,
enforced structurally by test_pool_invariant.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from backend.models.schemas import GetPoolStatusOut, NoArgs
from backend.services import pool_service


def get_pool_status(session: Session, user_id: str, args: NoArgs) -> GetPoolStatusOut:
    summary = pool_service.cycle_summary(session, user_id)
    return GetPoolStatusOut(
        in_a_cycle=summary is not None,
        cycle=summary,
        note="Simulated. No money is pooled; every member keeps an individual ledger.",
    )
