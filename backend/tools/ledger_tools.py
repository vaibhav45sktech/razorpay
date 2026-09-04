"""get_wallet_or_ledger, get_transactions — read-only ledger views for the agent.

Every number here is derived the same way state_service derives it, from the
same append-only LedgerEvent table (HLD s2.2). Nothing is computed twice with
different logic: get_wallet_or_ledger reuses ledger_service exactly as
GET /api/state does, so the agent can never "know" a number the UI doesn't.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import timezone

from sqlalchemy.orm import Session

from backend.models.entities import User
from backend.models.schemas import (
    GetTransactionsArgs,
    GetTransactionsOut,
    GetWalletOrLedgerOut,
    NoArgs,
)
from backend.services import ledger_service, money_action_service


def get_wallet_or_ledger(session: Session, user_id: str, args: NoArgs) -> GetWalletOrLedgerOut:
    user = session.get(User, user_id)
    assert user is not None, f"get_wallet_or_ledger called for unknown user {user_id!r}"

    balances = ledger_service.get_balances(session, user_id)
    policy = user.spend_policy
    spending = (
        ledger_service.get_month_spend_summary(session, user_id, monthly_limit_paise=policy.monthly_limit_paise)
        if policy
        else None
    )
    reserved = money_action_service.committed_pending_paise(session, user_id)
    recent = ledger_service.get_recent_events(session, user_id, limit=5)

    return GetWalletOrLedgerOut(
        balances_paise=balances,
        spending_this_month=spending,
        reserved_pending_paise=reserved,
        recent_events=[
            {
                "event_id": e.id,
                "type": e.type.value,
                "amount_paise": e.amount_paise,
                "bucket": e.bucket.value,
                "source": e.source,
                "at": e.created_at.isoformat(),
            }
            for e in recent
        ],
    )


def get_transactions(session: Session, user_id: str, args: GetTransactionsArgs) -> GetTransactionsOut:
    # "all" still has a sane ceiling: this is a chat tool result, not an export.
    limit = 500 if args.period == "all" else 200
    events = ledger_service.get_recent_events(session, user_id, limit=limit)

    if args.period == "this_month":
        month_start = ledger_service.current_month_start()
        # SQLite does not preserve tzinfo (audit_service.canonical_timestamp
        # documents the same quirk): a value round-tripped through the DB can
        # come back naive even though it was written as UTC-aware. Treat a
        # naive created_at as UTC rather than comparing naive to aware.
        events = [
            e
            for e in events
            if (e.created_at if e.created_at.tzinfo else e.created_at.replace(tzinfo=timezone.utc)) >= month_start
        ]

    total_by_type: dict[str, int] = defaultdict(int)
    total_by_bucket: dict[str, int] = defaultdict(int)
    for e in events:
        total_by_type[e.type.value] += e.amount_paise
        total_by_bucket[e.bucket.value] += e.amount_paise

    return GetTransactionsOut(
        period=args.period,
        event_count=len(events),
        total_by_type=dict(total_by_type),
        total_by_bucket=dict(total_by_bucket),
        events=[
            {
                "event_id": e.id,
                "type": e.type.value,
                "amount_paise": e.amount_paise,
                "bucket": e.bucket.value,
                "source": e.source,
                "at": e.created_at.isoformat(),
            }
            for e in events
        ],
    )
