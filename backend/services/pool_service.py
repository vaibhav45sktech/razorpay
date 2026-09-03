"""Community pool: cycles, allocations, and the simulated payout authorisation.

PHASE 2 SCOPE: the single read the policy engine needs. Cycle mechanics arrive
with Phase 3/8 work.

Reminder of what this pool IS NOT (PRD s4.1, Production Readiness s2): it holds
no money. A PoolCycle records rules and membership; a PoolAllocation records a
benefit and the human-readable reason it was granted. Every participant's money
stays in their own individual ledger. Nothing in this module may ever compute
or store a pooled balance - test_pool_invariant (Phase 3) enforces that.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.models.entities import PoolAllocation


def find_allocations_for_payout(
    session: Session, user_id: str, amount_paise: int
) -> list[PoolAllocation]:
    """All allocations for this user at exactly this amount, newest first.

    Returns every status so the policy engine can distinguish "authorised",
    "already paid" and "cancelled" and give the right reason for each. Amount
    must match exactly: a payout is authorised by a specific allocation, not by
    the existence of some allocation.
    """
    return list(
        session.execute(
            select(PoolAllocation)
            .where(
                PoolAllocation.user_id == user_id,
                PoolAllocation.amount_paise == amount_paise,
            )
            .order_by(PoolAllocation.created_at.desc())
        )
        .scalars()
        .all()
    )
