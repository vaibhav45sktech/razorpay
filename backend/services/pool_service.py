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


def mark_allocation_paid(session: Session, *, user_id: str, amount_paise: int) -> PoolAllocation:
    """Flip the authorising allocation to PAID once its payout has settled.

    Called only from money_action_service.settle_success for a TEST_PAYOUT.
    Marking PAID is what makes a second identical payout request a
    "payout_already_paid" DENY in the policy engine - duplicate prevention for
    payouts lives in the allocation's status, not in anyone's memory.
    """
    from backend.models.entities import AllocationStatus

    candidates = [
        a for a in find_allocations_for_payout(session, user_id, amount_paise)
        if a.status in (AllocationStatus.PROPOSED, AllocationStatus.CONFIRMED)
    ]
    if not candidates:
        raise LookupError(
            f"no authorising allocation for user {user_id} amount {amount_paise}; "
            "settlement should never have reached this point"
        )
    allocation = candidates[0]
    allocation.status = AllocationStatus.PAID
    session.flush()
    return allocation


def cycle_summary(session: Session, user_id: str) -> dict | None:
    """The user's view of their active cycle, for /api/state and the pool tool.

    Reports rules, membership size, contribution amount and the user's own
    allocations with their reasons. It never reports a pooled balance, because
    there is none to report.
    """
    from backend.models.entities import PoolCycle, PoolCycleStatus

    cycles = session.execute(
        select(PoolCycle).where(PoolCycle.status == PoolCycleStatus.ACTIVE)
    ).scalars().all()
    mine = [c for c in cycles if user_id in (c.members or [])]
    if not mine:
        return None
    cycle = mine[0]
    allocations = session.execute(
        select(PoolAllocation).where(PoolAllocation.cycle_id == cycle.id, PoolAllocation.user_id == user_id)
    ).scalars().all()
    return {
        "cycle_id": cycle.id,
        "label": cycle.label,
        "status": cycle.status.value,
        "size": cycle.size,
        "member_count": len(cycle.members or []),
        "contribution_amount_paise": cycle.contribution_amount_paise,
        "virtual_cycle_amount_paise": cycle.size * cycle.contribution_amount_paise,
        "rules": cycle.rules,
        "my_allocations": [
            {"allocation_id": a.id, "amount_paise": a.amount_paise, "status": a.status.value, "reason": a.reason}
            for a in allocations
        ],
        "is_synthetic": cycle.is_synthetic,
        "note": "Simulated. No money is pooled; every member keeps an individual ledger.",
    }
