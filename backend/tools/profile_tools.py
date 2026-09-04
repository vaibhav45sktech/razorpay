"""get_user_profile — the model's view of who it's talking to and what rules apply.

Thin on purpose: this is a read-only shape over User + SpendPolicy, already
built in Phases 1-3. No new domain logic lives here.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from backend.models.entities import User
from backend.models.schemas import GetUserProfileOut, NoArgs


def get_user_profile(session: Session, user_id: str, args: NoArgs) -> GetUserProfileOut:
    # run_agent_turn() already resolved user_id via state_service.get_state()
    # before the loop starts, so a missing user here would mean a bug in the
    # caller, not a normal runtime condition worth a soft error.
    user = session.get(User, user_id)
    assert user is not None, f"get_user_profile called for unknown user {user_id!r}"

    policy = user.spend_policy
    return GetUserProfileOut(
        user_id=user.id,
        name=user.name,
        status=user.status.value,
        is_synthetic=user.is_synthetic,
        spend_policy=(
            None
            if policy is None
            else {
                "monthly_limit_paise": policy.monthly_limit_paise,
                "approval_threshold_paise": policy.approval_threshold_paise,
                "per_tx_limit_paise": policy.per_tx_limit_paise,
                "protected_buckets": policy.protected_buckets,
                "paused": policy.paused,
            }
        ),
        flags={"paused": bool(policy and policy.paused)},
    )
