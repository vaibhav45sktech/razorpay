"""check_policy — the model's own (advisory) look at the policy engine.

IMPORTANT: this tool result is advisory to the model, never binding on the
backend. The orchestrator's execute_tool() independently re-runs
policy_engine.check_policy() itself before any money tool executes,
regardless of what this tool already returned in the conversation — see
agent/orchestrator.py's module docstring. A confused or adversarial model
skipping this call, or lying about its result, changes nothing.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from backend.models.schemas import CheckPolicyArgs, CheckPolicyOut
from backend.services import policy_engine


def check_policy(session: Session, user_id: str, args: CheckPolicyArgs) -> CheckPolicyOut:
    result = policy_engine.check_policy(
        session,
        user_id=user_id,
        action=args.action,
        amount_paise=args.amount_paise,
        purpose=args.purpose,
        bucket=args.bucket,
    )
    return CheckPolicyOut(**result.as_dict())
