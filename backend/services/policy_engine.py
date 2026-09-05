"""The deterministic policy engine. The actual safety mechanism of CampusPool.

    The LLM may only REQUEST an action. This module DECIDES.

Everything here is plain arithmetic and if-statements over the ledger, the
user's own rules and policy_config.yaml. The same inputs always produce the
same answer, which is why it can be table-tested (test_policy.py) and why
repeated user insistence changes nothing (PRD s5.4): there is no memory of
persuasion to appeal to.

DESIGN RULES
    Default-deny.   Unknown action, unknown user, missing rules, bad amount -
                    every unrecognised situation is a DENY with a reason.
    Pure.           check_policy reads; it never writes. Callers (the
                    orchestrator, intent creation) record the result in the
                    audit trail. This keeps the engine trivially testable and
                    safe to call from anywhere.
    Ordered.        Rules are evaluated in a fixed order so that the MOST
                    restrictive applicable answer always wins: a protected
                    bucket is denied before limits are even looked at, and a
                    limit DENY outranks an approval REQUIRE_APPROVAL.
    Explained.      Every result carries a human-readable reason with the
                    actual numbers, because the user, the agent and the audit
                    trail all need to see WHY, not just what.

HOW THE AGENT IS BOUND BY THIS
    The orchestrator (Phase 4) calls check_policy itself before executing any
    money tool, regardless of whether the model already asked for a check. The
    prompt requests good behaviour; that call guarantees it.
"""

from __future__ import annotations

import dataclasses
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from sqlalchemy.orm import Session

from backend import config as app_config
from backend.models.entities import (
    AllocationStatus,
    Bucket,
    IntentType,
    PolicyDecision,
    SpendPolicy,
    User,
)
from backend.services import ledger_service, money_action_service, pool_service

logger = logging.getLogger("campuspool.policy")

DEFAULT_CONFIG_PATH: Path = app_config.BASE_DIR / "backend" / "policy_config.yaml"

#: The only actions the engine knows. Anything else is denied as unknown.
KNOWN_ACTIONS: frozenset[str] = frozenset(t.value for t in IntentType)


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PolicyConfig:
    """Typed, immutable view of policy_config.yaml.

    Frozen so a config object can be shared safely, and so tests derive
    variants with with_velocity() instead of mutating shared state.
    """

    version: int
    currency: str
    contribution_min_paise: int
    contribution_max_paise: int
    default_monthly_limit_paise: int
    default_approval_threshold_paise: int
    approval_is_strictly_above: bool
    default_per_tx_limit_paise: int | None
    protected_buckets: tuple[str, ...]
    pause_blocks_actions: tuple[str, ...]
    max_intents_per_hour: int
    max_intents_per_day: int
    max_pending_intents: int

    _VELOCITY_FIELDS = ("max_intents_per_hour", "max_intents_per_day", "max_pending_intents")

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> PolicyConfig:
        contribution = raw["contribution"]
        purchase = raw["purchase"]
        velocity = raw["velocity"]
        return cls(
            version=int(raw["version"]),
            currency=str(raw["currency"]),
            contribution_min_paise=int(contribution["min_paise"]),
            contribution_max_paise=int(contribution["max_paise"]),
            default_monthly_limit_paise=int(purchase["default_monthly_limit_paise"]),
            default_approval_threshold_paise=int(purchase["default_approval_threshold_paise"]),
            approval_is_strictly_above=bool(purchase.get("approval_is_strictly_above", True)),
            default_per_tx_limit_paise=(
                int(purchase["default_per_tx_limit_paise"])
                if purchase.get("default_per_tx_limit_paise") is not None
                else None
            ),
            protected_buckets=tuple(str(b) for b in raw.get("protected_buckets", [])),
            pause_blocks_actions=tuple(
                str(a).upper() for a in raw.get("pause", {}).get("blocks_actions", [])
            ),
            max_intents_per_hour=int(velocity["max_intents_per_hour"]),
            max_intents_per_day=int(velocity["max_intents_per_day"]),
            max_pending_intents=int(velocity["max_pending_intents"]),
        )

    def with_velocity(self, **overrides: int) -> PolicyConfig:
        """Derive a config with different velocity limits (used by tests)."""
        unknown = set(overrides) - set(self._VELOCITY_FIELDS)
        if unknown:
            raise ValueError(f"not velocity fields: {sorted(unknown)}")
        return dataclasses.replace(self, **overrides)


@lru_cache(maxsize=4)
def _load_config_cached(path: str) -> PolicyConfig:
    with open(path, encoding="utf-8") as fh:
        raw = yaml.safe_load(fh)
    cfg = PolicyConfig.from_dict(raw)
    logger.info("Policy config v%s loaded from %s", cfg.version, path)
    return cfg


def load_config(path: Path | str | None = None) -> PolicyConfig:
    """Load and cache policy_config.yaml. Cheap to call repeatedly."""
    return _load_config_cached(str(path or DEFAULT_CONFIG_PATH))


# ---------------------------------------------------------------------------
# Result
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PolicyResult:
    """The engine's answer. Frozen: a decision is a fact, not a draft.

    Attributes:
        decision: ALLOW, DENY or REQUIRE_APPROVAL - nothing else exists.
        reason:   human-readable, with figures. Shown to the user verbatim.
        rule:     machine-readable name of the rule that produced the answer
                  ("monthly_limit", "protected_bucket", "ok", ...). Tests and
                  metrics key on this.
        details:  the numbers the decision was made from, for the audit trail.
    """

    decision: PolicyDecision
    reason: str
    rule: str
    details: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "decision": self.decision.value,
            "reason": self.reason,
            "rule": self.rule,
            "details": dict(self.details),
        }


def _count(result: PolicyResult) -> PolicyResult:
    """Every verdict this engine returns is counted for /metrics (Phase 8
    item 3). Done in the three constructors rather than at call sites, so a
    future rule cannot be added and silently escape the metric. Import is
    local and failure is swallowed: the policy engine must stay pure and
    must never fail to decide because a counter is unhappy."""
    try:
        from backend.observability import POLICY_DECISIONS
        POLICY_DECISIONS.labels(result.decision.value, result.rule).inc()
    except Exception:  # noqa: BLE001
        pass
    return result


def _deny(rule: str, reason: str, **details: Any) -> PolicyResult:
    return _count(PolicyResult(PolicyDecision.DENY, reason, rule, details))


def _allow(reason: str, **details: Any) -> PolicyResult:
    return _count(PolicyResult(PolicyDecision.ALLOW, reason, "ok", details))


def _require_approval(reason: str, **details: Any) -> PolicyResult:
    return _count(PolicyResult(PolicyDecision.REQUIRE_APPROVAL, reason, "approval_threshold", details))


def _rupees(paise: int) -> str:
    """Format paise as rupees for reason strings: 100000 -> '₹1,000'."""
    if paise % 100 == 0:
        return f"₹{paise // 100:,}"
    return f"₹{paise / 100:,.2f}"


# ---------------------------------------------------------------------------
# Input normalisation
# ---------------------------------------------------------------------------


def _normalise_action(action: Any) -> str:
    if hasattr(action, "value"):
        action = action.value
    return str(action).strip().upper()


def _normalise_bucket(bucket: Any) -> tuple[Bucket | None, str | None]:
    """Resolve a bucket argument. Returns (bucket, None) or (None, offending_text).

    Returns a pair rather than a Bucket-or-str union because Bucket is a str
    subclass (class Bucket(str, enum.Enum)), so `isinstance(x, str)` is True for
    a valid member as well - a union return type would be indistinguishable at
    the call site. That exact confusion was caught by the test table.
    """
    if bucket is None:
        return None, None
    if isinstance(bucket, Bucket):
        return bucket, None
    raw = str(bucket).strip().lower()
    try:
        return Bucket(raw), None
    except ValueError:
        return None, raw


def _valid_amount(amount_paise: Any) -> bool:
    return isinstance(amount_paise, int) and not isinstance(amount_paise, bool) and amount_paise > 0


# ---------------------------------------------------------------------------
# The engine
# ---------------------------------------------------------------------------


def check_policy(
    session: Session,
    *,
    user_id: str,
    action: str | IntentType,
    amount_paise: int,
    purpose: str,
    bucket: Bucket | str | None = None,
    config: PolicyConfig | None = None,
) -> PolicyResult:
    """Decide whether a proposed money action is permitted.

    Args:
        user_id:      injected server-side by the caller, never trusted from the model.
        action:       PURCHASE, CONTRIBUTION or TEST_PAYOUT. Anything else is denied.
        amount_paise: positive integer paise. Anything else is denied.
        purpose:      structured purpose string, recorded in details for the audit trail.
        bucket:       for PURCHASE, the bucket the money would leave. Defaults to
                      discretionary. A protected bucket is denied before anything
                      else is considered.
        config:       override the loaded policy_config (tests). Defaults to the file.

    Returns:
        PolicyResult. Never raises on bad input - bad input is a DENY with a reason,
        because the caller may be relaying an LLM's request and needs an answer it
        can explain, not an exception.
    """
    cfg = config or load_config()
    action_name = _normalise_action(action)

    # ---- 0. Input validity (default-deny on anything malformed) ----
    if not _valid_amount(amount_paise):
        return _deny(
            "invalid_amount",
            "Amount must be a positive whole number of paise.",
            requested=repr(amount_paise),
            purpose=purpose,
        )
    if action_name not in KNOWN_ACTIONS:
        return _deny(
            "unknown_action",
            f"'{action_name}' is not an action this product supports.",
            action=action_name,
            purpose=purpose,
        )

    # ---- 1. The user and their rules must exist (fail closed) ----
    user = session.get(User, user_id)
    if user is None:
        return _deny("unknown_user", "No such user.", user_id=user_id)
    policy: SpendPolicy | None = user.spend_policy
    if policy is None:
        return _deny(
            "no_spend_policy",
            "No spending rules are configured for this account, so no action is permitted.",
            user_id=user_id,
        )

    # ---- 2. Pause (PRD s4.3) ----
    if policy.paused and action_name in cfg.pause_blocks_actions:
        return _deny(
            "paused",
            "Spending is paused by your own rule. Resume spending to continue.",
            action=action_name,
        )

    # ---- 3. Velocity / abuse controls (Production Readiness s3.3) ----
    velocity_denial = _check_velocity(session, user_id, cfg)
    if velocity_denial is not None:
        return velocity_denial

    # ---- 4. Action-specific rules ----
    if action_name == IntentType.PURCHASE.value:
        return _check_purchase(session, user_id, policy, cfg, amount_paise, purpose, bucket)
    if action_name == IntentType.CONTRIBUTION.value:
        return _check_contribution(cfg, amount_paise, purpose)
    if action_name == IntentType.TEST_PAYOUT.value:
        return _check_test_payout(session, user_id, amount_paise, purpose)

    # Unreachable given KNOWN_ACTIONS, kept so a future enum addition cannot
    # silently fall through to ALLOW.
    return _deny("unknown_action", f"'{action_name}' has no policy defined.", action=action_name)


# ---------------------------------------------------------------------------
# Rule groups
# ---------------------------------------------------------------------------


def _check_velocity(session: Session, user_id: str, cfg: PolicyConfig) -> PolicyResult | None:
    """Per-user rate and concurrency caps. Returns a DENY or None."""
    now = datetime.now(timezone.utc)

    last_hour = money_action_service.count_created_since(session, user_id, now - timedelta(hours=1))
    if last_hour >= cfg.max_intents_per_hour:
        return _deny(
            "velocity_hourly",
            f"Too many actions in the last hour ({last_hour}). "
            f"The limit is {cfg.max_intents_per_hour}; please try again later.",
            count_last_hour=last_hour,
            max_per_hour=cfg.max_intents_per_hour,
        )

    last_day = money_action_service.count_created_since(session, user_id, now - timedelta(days=1))
    if last_day >= cfg.max_intents_per_day:
        return _deny(
            "velocity_daily",
            f"Too many actions in the last 24 hours ({last_day}). "
            f"The limit is {cfg.max_intents_per_day}.",
            count_last_day=last_day,
            max_per_day=cfg.max_intents_per_day,
        )

    pending = money_action_service.count_pending(session, user_id)
    if pending >= cfg.max_pending_intents:
        return _deny(
            "pending_cap",
            f"You already have {pending} actions in progress. "
            f"Let those complete before starting another (limit {cfg.max_pending_intents}).",
            pending_count=pending,
            max_pending=cfg.max_pending_intents,
        )
    return None


def _check_purchase(
    session: Session,
    user_id: str,
    policy: SpendPolicy,
    cfg: PolicyConfig,
    amount_paise: int,
    purpose: str,
    bucket: Any,
) -> PolicyResult:
    # -- Protected buckets: decided before limits are even looked at --
    resolved, bad_text = _normalise_bucket(bucket)
    if bad_text is not None:
        return _deny("invalid_bucket", f"'{bad_text}' is not a known bucket.", bucket=bad_text)
    source_bucket: Bucket = resolved or Bucket.DISCRETIONARY

    protected = set(cfg.protected_buckets) | set(policy.protected_buckets or [])
    if source_bucket.value in protected:
        return _deny(
            "protected_bucket",
            "Your emergency savings are protected and cannot be spent by the assistant. "
            "This rule exists to keep your cushion intact; it can't be overridden in chat."
            if source_bucket is Bucket.EMERGENCY_SAVINGS
            else f"The {source_bucket.value.replace('_', ' ')} bucket is protected from spending.",
            bucket=source_bucket.value,
            requested_paise=amount_paise,
        )

    # -- Per-transaction cap (only when the user, or config, has set one) --
    per_tx = policy.per_tx_limit_paise if policy.per_tx_limit_paise is not None else cfg.default_per_tx_limit_paise
    if per_tx is not None and amount_paise > per_tx:
        return _deny(
            "per_tx_limit",
            f"{_rupees(amount_paise)} exceeds your per-purchase limit of {_rupees(per_tx)}.",
            requested_paise=amount_paise,
            per_tx_limit_paise=per_tx,
        )

    # -- Monthly limit: settled + committed-but-unsettled + this request --
    monthly_limit = policy.monthly_limit_paise
    settled = ledger_service.month_spend(session, user_id, Bucket.DISCRETIONARY)
    committed = money_action_service.committed_pending_paise(session, user_id)
    projected = settled + committed + amount_paise
    details = {
        "monthly_limit_paise": monthly_limit,
        "settled_this_month_paise": settled,
        "committed_pending_paise": committed,
        "requested_paise": amount_paise,
        "projected_total_paise": projected,
        "bucket": source_bucket.value,
        "purpose": purpose,
    }
    if projected > monthly_limit:
        already = settled + committed
        return _deny(
            "monthly_limit",
            f"This would take you over your monthly limit of {_rupees(monthly_limit)}. "
            f"You've used {_rupees(already)} so far"
            + (f" (including {_rupees(committed)} still in progress)" if committed else "")
            + f", leaving {_rupees(max(0, monthly_limit - already))}.",
            **details,
        )

    # -- Approval threshold --
    threshold = policy.approval_threshold_paise
    needs_approval = amount_paise > threshold if cfg.approval_is_strictly_above else amount_paise >= threshold
    if needs_approval:
        return _require_approval(
            f"{_rupees(amount_paise)} is above your {_rupees(threshold)} approval threshold, "
            "so this needs your explicit approval before it can go ahead.",
            approval_threshold_paise=threshold,
            **details,
        )

    return _allow(
        f"Within limits: {_rupees(amount_paise)} leaves {_rupees(monthly_limit - projected)} "
        f"of your {_rupees(monthly_limit)} monthly budget.",
        **details,
    )


def _check_contribution(cfg: PolicyConfig, amount_paise: int, purpose: str) -> PolicyResult:
    lo, hi = cfg.contribution_min_paise, cfg.contribution_max_paise
    details = {
        "requested_paise": amount_paise,
        "min_paise": lo,
        "max_paise": hi,
        "purpose": purpose,
    }
    if not (lo <= amount_paise <= hi):
        return _deny(
            "contribution_band",
            f"Contributions must be between {_rupees(lo)} and {_rupees(hi)}; "
            f"{_rupees(amount_paise)} is outside that range.",
            **details,
        )
    return _allow(f"{_rupees(amount_paise)} is within the {_rupees(lo)}–{_rupees(hi)} contribution range.", **details)


def _check_test_payout(session: Session, user_id: str, amount_paise: int, purpose: str) -> PolicyResult:
    """A payout is authorised ONLY by an explainable pool allocation (PRD s4.1)."""
    allocations = pool_service.find_allocations_for_payout(session, user_id, amount_paise)
    details = {"requested_paise": amount_paise, "purpose": purpose}

    authorising = [a for a in allocations if a.status in (AllocationStatus.PROPOSED, AllocationStatus.CONFIRMED)]
    if authorising:
        alloc = authorising[0]
        # The allocation's own reason IS the authorisation - it is shown verbatim.
        return _allow(alloc.reason, allocation_id=alloc.id, cycle_id=alloc.cycle_id, **details)

    if any(a.status is AllocationStatus.PAID for a in allocations):
        return _deny(
            "payout_already_paid",
            f"A payout of {_rupees(amount_paise)} for this allocation has already been made.",
            **details,
        )

    return _deny(
        "no_pool_authorization",
        f"No pool rule authorises a payout of {_rupees(amount_paise)} to this account.",
        **details,
    )
