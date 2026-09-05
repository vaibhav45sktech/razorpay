"""Agentic Card — a rule-driven purchase agent that lives INSIDE the policy engine.

The student writes a rule ("buy the headphones when they drop to ₹2,000, but
only after the 15th and only if I still have ₹2,000 of budget left"). From
then on the card does the watching. Everything below is deterministic code
over a SYNTHETIC price feed and the ledger — the language model is not in the
loop. It can read this state and explain it; it cannot fire a rule.

THE LOOP (one tick, run by the background monitor in main.py)

    tick()  ->  quote every product on every platform        (price_service part)
            ->  for each ACTIVE rule:      evaluate()         price + compound checks
                    all met  ->  _fire()   PURCHASE intent through policy_engine
                                            ALLOW  + manual  -> notify, wait for YES
                                            ALLOW  + auto    -> simulated settlement (DEBUG)
                                            REQUIRE_APPROVAL -> notify, wait for approve
                                            DENY             -> rule BLOCKED, notify
            ->  for each AWAITING rule:    _follow_up()      settled? -> DONE
                                                             declined/failed? -> back to ACTIVE
                                                             window lapsed? -> expire, back to ACTIVE

WHAT THE CARD'S LIMITS ARE
    There is no second set of limits. The card's monthly cap, per-purchase cap,
    "ask me above" line and frozen switch are SpendPolicy.monthly_limit_paise,
    per_tx_limit_paise, approval_threshold_paise and paused - the very fields
    policy_engine enforces on every purchase from any source. Editing them on
    the card screen therefore changes what the whole product will allow, which
    is the point: the card IS the rules.

WHAT MOVES MONEY
    Only the existing settlement path. Manual mode ends in Razorpay test
    checkout completed by the student. Auto mode settles through the same
    settle_success() the webhook uses, with evidence stamped simulated, and is
    available only when DEBUG is on - exactly like the pool's simulated payout.
"""

from __future__ import annotations

import hashlib
import logging
from datetime import date, datetime, timedelta, timezone
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from backend import config
from backend.models.entities import (
    ActionIntent, ApprovalMode, AuditActor, Bucket, IntentStatus, LedgerEvent, LedgerEventType,
    Notification, NotificationKind, PriceTick, PurchaseRule, RuleStatus, SpendPolicy, User, VirtualCard,
    WatchedProduct,
)
from backend.services import audit_service, ledger_service, policy_engine
from backend.services import money_action_service as mas

logger = logging.getLogger("campuspool.card")

S = IntentStatus

# ---------------------------------------------------------------------------
# Synthetic platforms and the condition vocabulary
# ---------------------------------------------------------------------------

PLATFORMS: dict[str, str] = {
    "shopkart": "ShopKart (synthetic)",
    "bazaario": "Bazaario (synthetic)",
}

#: Compound condition types the rule engine understands. Anything else is
#: refused at rule creation (default-deny, same stance as the policy engine).
#: value_kind tells the form what to collect; "rupees" values arrive in rupees
#: over the API and are stored in paise.
CONDITION_TYPES: dict[str, dict[str, str]] = {
    "date_after":           {"label": "Only on or after a date",            "value_kind": "date"},
    "date_before":          {"label": "Only on or before a date",           "value_kind": "date"},
    "budget_remaining_gte": {"label": "Only if my monthly budget left is ≥", "value_kind": "rupees"},
    "min_discount_pct":     {"label": "Only if the discount off MRP is ≥ %", "value_kind": "percent"},
    "min_stock":            {"label": "Only if at least N units are left",   "value_kind": "int"},
    "stock_available":      {"label": "Only if in stock",                    "value_kind": "bool"},
}

_HISTORY_POINTS = 24


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _rs(paise: int) -> str:
    return f"₹{paise // 100:,}" if paise % 100 == 0 else f"₹{paise / 100:,.2f}"


# ---------------------------------------------------------------------------
# Price feed (synthetic, deterministic)
# ---------------------------------------------------------------------------


def _step(seed: str, lo: float, hi: float) -> float:
    """Deterministic pseudo-random float in [lo, hi) from a string seed, so a
    replayed tick produces the same quote and tests are reproducible."""
    h = int(hashlib.sha256(seed.encode()).hexdigest()[:12], 16) / float(16 ** 12)
    return lo + (hi - lo) * h


def quote_all(session: Session, *, at: datetime | None = None) -> int:
    """Advance every product's price on every platform by one step of a bounded
    random walk (60%..100% of MRP, gentle downward bias, bounce back up when it
    hits the floor - a 'sale' ending). Records a PriceTick per quote."""
    at = at or _now()
    bucket = at.strftime("%Y%m%d%H%M%S")
    n = 0
    for p in session.execute(select(WatchedProduct)).scalars().all():
        prices = dict(p.prices or {})
        for plat in PLATFORMS:
            q = dict(prices.get(plat) or {"price_paise": int(p.list_price_paise * 0.92), "stock": 6, "held": 0})
            if q.get("pinned"):          # DEBUG set_price pinned this quote; leave it
                continue
            price = int(q["price_paise"])
            drift = _step(f"{p.id}|{plat}|{bucket}|px", -0.045, 0.035)
            price = int(round(price * (1 + drift) / 100.0)) * 100
            floor, ceil = int(p.list_price_paise * 0.6), p.list_price_paise
            if price <= floor:
                price = int(p.list_price_paise * _step(f"{p.id}|{plat}|{bucket}|bounce", 0.9, 0.98)) // 100 * 100
            price = max(floor, min(ceil, price))
            stock = int(q.get("stock", 6))
            r = _step(f"{p.id}|{plat}|{bucket}|st", 0, 1)
            if r < 0.12:
                stock = max(0, stock - 1)
            elif r > 0.9:
                stock = min(9, stock + 2)
            q.update({"price_paise": price, "stock": stock})
            prices[plat] = q
            session.add(PriceTick(product_id=p.id, platform=plat, price_paise=price, stock=stock, observed_at=at))
            n += 1
        p.prices = prices
    session.flush()
    return n


def set_price(session: Session, *, product_id: str, platform: str, price_paise: int, stock: int | None = None,
              pinned: bool = True) -> dict[str, Any]:
    """DEBUG helper: put a product at a chosen price so a rule can be shown
    firing in a demo. Recorded as a tick like any other quote, so the
    sparkline shows the drop and the rule's evidence is real."""
    p = session.get(WatchedProduct, product_id)
    if p is None:
        raise LookupError(product_id)
    if platform not in PLATFORMS:
        raise ValueError(f"unknown platform {platform!r}")
    if price_paise <= 0:
        raise ValueError("price must be positive")
    prices = dict(p.prices or {})
    q = dict(prices.get(platform) or {"stock": 6, "held": 0})
    q["price_paise"] = int(price_paise)
    if stock is not None:
        q["stock"] = max(0, int(stock))
    q["pinned"] = bool(pinned)
    prices[platform] = q
    p.prices = prices
    session.add(PriceTick(product_id=p.id, platform=platform, price_paise=q["price_paise"], stock=q["stock"]))
    session.flush()
    return _product_view(session, p)


def _history(session: Session, product_id: str) -> dict[str, list[int]]:
    rows = session.execute(
        select(PriceTick).where(PriceTick.product_id == product_id)
        .order_by(PriceTick.observed_at.desc(), PriceTick.id.desc()).limit(_HISTORY_POINTS * len(PLATFORMS))
    ).scalars().all()
    out: dict[str, list[int]] = {k: [] for k in PLATFORMS}
    for t in reversed(rows):
        out.setdefault(t.platform, []).append(t.price_paise)
    return {k: v[-_HISTORY_POINTS:] for k, v in out.items()}


def _best_quote(p: WatchedProduct, platforms: list[str] | None = None) -> tuple[str, dict[str, Any]] | None:
    """Cheapest in-stock quote among the allowed platforms, or None."""
    allowed = [x for x in (platforms or list(PLATFORMS)) if x in PLATFORMS]
    best = None
    for plat in allowed:
        q = (p.prices or {}).get(plat)
        if not q:
            continue
        avail = int(q.get("stock", 0)) - int(q.get("held", 0))
        if avail <= 0:
            continue
        if best is None or q["price_paise"] < best[1]["price_paise"]:
            best = (plat, q)
    return best


def _product_view(session: Session, p: WatchedProduct) -> dict[str, Any]:
    quotes = []
    for plat, label in PLATFORMS.items():
        q = (p.prices or {}).get(plat) or {}
        if not q:
            continue
        stock = int(q.get("stock", 0)); held = int(q.get("held", 0))
        quotes.append({
            "platform": plat, "platform_label": label, "price_paise": int(q["price_paise"]),
            "stock": stock, "available_stock": max(0, stock - held), "held": held,
            "discount_pct": round((p.list_price_paise - int(q["price_paise"])) / p.list_price_paise * 100, 1),
            "pinned": bool(q.get("pinned")),
        })
    best = _best_quote(p)
    return {
        "product_id": p.id, "name": p.name, "category": p.category, "list_price_paise": p.list_price_paise,
        "quotes": quotes,
        "best": None if best is None else {"platform": best[0], "platform_label": PLATFORMS[best[0]],
                                           "price_paise": int(best[1]["price_paise"])},
        "history": _history(session, p.id),
        "is_synthetic": True,
    }


def list_products(session: Session) -> list[dict[str, Any]]:
    return [_product_view(session, p) for p in
            session.execute(select(WatchedProduct).order_by(WatchedProduct.list_price_paise)).scalars().all()]


# ---------------------------------------------------------------------------
# The card and its limits (= the spend policy)
# ---------------------------------------------------------------------------


def _card(session: Session, user_id: str) -> VirtualCard:
    card = session.execute(select(VirtualCard).where(VirtualCard.user_id == user_id)).scalar_one_or_none()
    if card is None:
        if session.get(User, user_id) is None:
            raise LookupError(user_id)
        # Issue on first use. last4 is derived from the user id: obviously not a PAN.
        card = VirtualCard(user_id=user_id, last4=str(int(hashlib.sha256(user_id.encode()).hexdigest()[:6], 16) % 10000).zfill(4))
        session.add(card)
        session.flush()
        audit_service.write(session, actor=AuditActor.BACKEND, action="card_issued", user_id=user_id,
                            inputs={"card_id": card.id, "last4": card.last4})
    return card


def _policy(session: Session, user_id: str) -> SpendPolicy:
    user = session.get(User, user_id)
    if user is None or user.spend_policy is None:
        raise LookupError(user_id)
    return user.spend_policy


def card_summary(session: Session, user_id: str) -> dict[str, Any]:
    card = _card(session, user_id)
    pol = _policy(session, user_id)
    settled = ledger_service.month_spend(session, user_id, Bucket.DISCRETIONARY)
    committed = mas.committed_pending_paise(session, user_id)
    return {
        "card_id": card.id, "label": card.label, "last4": card.last4, "is_synthetic": True,
        "holder": session.get(User, user_id).name,
        "frozen": bool(pol.paused),
        "monthly_limit_paise": pol.monthly_limit_paise,
        "per_tx_limit_paise": pol.per_tx_limit_paise,
        "approval_threshold_paise": pol.approval_threshold_paise,
        "spent_this_month_paise": settled,
        "committed_pending_paise": committed,
        "headroom_paise": max(0, pol.monthly_limit_paise - settled - committed),
        "protected_buckets": list(pol.protected_buckets or []),
        "auto_mode_available": bool(config.DEBUG),
        "issued_at": card.created_at.isoformat(),
    }


def update_limits(session: Session, user_id: str, *, monthly_limit_paise: int | None = None,
                  per_tx_limit_paise: int | None = None, clear_per_tx: bool = False,
                  approval_threshold_paise: int | None = None, frozen: bool | None = None) -> dict[str, Any]:
    """The student sets the card's lines. Structured USER action; audited with
    before/after so a later 'why was this allowed?' has an answer."""
    pol = _policy(session, user_id)
    before = {"monthly_limit_paise": pol.monthly_limit_paise, "per_tx_limit_paise": pol.per_tx_limit_paise,
              "approval_threshold_paise": pol.approval_threshold_paise, "paused": pol.paused}
    if monthly_limit_paise is not None:
        if monthly_limit_paise < 0 or monthly_limit_paise > 50_000_00:
            raise ValueError("monthly cap must be between ₹0 and ₹50,000")
        pol.monthly_limit_paise = int(monthly_limit_paise)
    if clear_per_tx:
        pol.per_tx_limit_paise = None
    elif per_tx_limit_paise is not None:
        if per_tx_limit_paise <= 0 or per_tx_limit_paise > 50_000_00:
            raise ValueError("per-purchase cap must be between ₹1 and ₹50,000")
        pol.per_tx_limit_paise = int(per_tx_limit_paise)
    if approval_threshold_paise is not None:
        if approval_threshold_paise < 0:
            raise ValueError("approval threshold cannot be negative")
        pol.approval_threshold_paise = int(approval_threshold_paise)
    if frozen is not None:
        pol.paused = bool(frozen)
    session.flush()
    after = {"monthly_limit_paise": pol.monthly_limit_paise, "per_tx_limit_paise": pol.per_tx_limit_paise,
             "approval_threshold_paise": pol.approval_threshold_paise, "paused": pol.paused}
    audit_service.write(session, actor=AuditActor.USER, action="card_limits_changed", user_id=user_id,
                        inputs={"before": before, "after": after})
    changes = [k for k in after if after[k] != before[k]]
    if changes:
        _notify(session, user_id, NotificationKind.CARD_LIMITS_CHANGED, "Card rules updated",
                _describe_limits(after) + " Every purchase — from a rule, an offer or the chat — is checked against these.")
    return card_summary(session, user_id)


def _describe_limits(p: dict[str, Any]) -> str:
    bits = [f"Monthly cap {_rs(p['monthly_limit_paise'])}",
            f"ask me above {_rs(p['approval_threshold_paise'])}",
            f"per purchase {'no cap' if p['per_tx_limit_paise'] is None else _rs(p['per_tx_limit_paise'])}",
            "card frozen" if p["paused"] else "card active"]
    return "; ".join(bits) + "."


# ---------------------------------------------------------------------------
# Notifications (in-app only)
# ---------------------------------------------------------------------------


def _notify(session: Session, user_id: str, kind: NotificationKind, title: str, body: str, *,
            rule: PurchaseRule | None = None, intent_id: str | None = None, actions: dict | None = None) -> Notification:
    n = Notification(user_id=user_id, kind=kind, title=title, body=body,
                     rule_id=rule.id if rule else None, intent_id=intent_id, actions=actions or {})
    session.add(n)
    session.flush()
    audit_service.write(session, actor=AuditActor.SYSTEM, action=f"notification:{kind.value}", user_id=user_id,
                        intent_id=intent_id, inputs={"notification_id": n.id, "title": title})
    return n


def list_notifications(session: Session, user_id: str, *, limit: int = 30) -> dict[str, Any]:
    rows = session.execute(
        select(Notification).where(Notification.user_id == user_id)
        .order_by(Notification.created_at.desc()).limit(limit)
    ).scalars().all()
    unread = int(session.execute(
        select(func.count(Notification.id)).where(Notification.user_id == user_id, Notification.read.is_(False))
    ).scalar_one())
    items = []
    for n in rows:
        intent = session.get(ActionIntent, n.intent_id) if n.intent_id else None
        rule = session.get(PurchaseRule, n.rule_id) if n.rule_id else None
        actionable = bool(intent) and intent.status in (S.AWAITING_APPROVAL, S.ALLOWED, S.APPROVED) \
            and rule is not None and rule.status is RuleStatus.AWAITING_APPROVAL
        items.append({
            "notification_id": n.id, "kind": n.kind.value, "title": n.title, "body": n.body, "read": n.read,
            "rule_id": n.rule_id, "intent_id": n.intent_id, "actions": n.actions,
            "actionable": actionable, "intent_status": intent.status.value if intent else None,
            "created_at": n.created_at.isoformat(),
        })
    return {"unread": unread, "items": items}


def mark_read(session: Session, user_id: str, *, notification_id: str | None = None) -> int:
    q = select(Notification).where(Notification.user_id == user_id, Notification.read.is_(False))
    if notification_id:
        q = q.where(Notification.id == notification_id)
    rows = session.execute(q).scalars().all()
    for n in rows:
        n.read = True
    session.flush()
    return len(rows)


def respond(session: Session, user_id: str, *, notification_id: str, answer: str) -> dict[str, Any]:
    """The student's tap on a notification: delegates to respond_rule()."""
    n = session.get(Notification, notification_id)
    if n is None or n.user_id != user_id:
        raise LookupError(notification_id)
    n.read = True
    if not n.rule_id:
        raise ValueError("this notification has nothing to act on")
    return respond_rule(session, user_id, rule_id=n.rule_id, answer=answer)


def respond_rule(session: Session, user_id: str, *, rule_id: str, answer: str) -> dict[str, Any]:
    """YES approves (if approval is what is pending) and tells the browser to
    open checkout; NO declines and the rule goes back to watching. Structured
    USER action - the chat cannot do this."""
    rule = session.get(PurchaseRule, rule_id)
    if rule is None or rule.user_id != user_id:
        raise LookupError(rule_id)
    if rule.status is not RuleStatus.AWAITING_APPROVAL or not rule.intent_id:
        raise ValueError("this rule is no longer waiting for you")
    intent = mas.get(session, rule.intent_id)
    if answer == "yes":
        if intent.status is S.AWAITING_APPROVAL:
            mas.approve(session, intent_id=intent.id, user_id=user_id)   # honours expires_at
        if intent.status in (S.APPROVED, S.ALLOWED):
            audit_service.write(session, actor=AuditActor.USER, action="card_rule_yes", user_id=user_id,
                                intent_id=intent.id, inputs={"rule_id": rule.id})
            return {"next": "pay", "intent_id": intent.id, "status": intent.status.value, "rule_id": rule.id,
                    "amount_paise": intent.amount_paise}
        return {"next": "none", "intent_id": intent.id, "status": intent.status.value, "rule_id": rule.id}
    if answer == "no":
        if intent.status is S.AWAITING_APPROVAL:
            mas.deny_approval(session, intent_id=intent.id, user_id=user_id)
        elif intent.status in (S.ALLOWED, S.APPROVED):
            mas.expire_unexecuted(session, intent, reason="declined by user")
        audit_service.write(session, actor=AuditActor.USER, action="card_rule_no", user_id=user_id,
                            intent_id=intent.id, inputs={"rule_id": rule.id})
        _release(session, rule, why="You said no. Back to watching the price.", kind=NotificationKind.RULE_RESUMED)
        return {"next": "none", "intent_id": intent.id, "status": intent.status.value, "rule_id": rule.id}
    raise ValueError("answer must be 'yes' or 'no'")


# ---------------------------------------------------------------------------
# Rules: create / list / cancel / resume
# ---------------------------------------------------------------------------


def _validate_conditions(conds: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for c in conds or []:
        t = str(c.get("type", "")).strip()
        if t not in CONDITION_TYPES:
            raise ValueError(f"unsupported condition type {t!r}")
        kind = CONDITION_TYPES[t]["value_kind"]
        v = c.get("value")
        if kind == "date":
            date.fromisoformat(str(v))            # raises ValueError if malformed
            v = str(v)
        elif kind == "rupees":
            v = int(round(float(v) * 100))         # stored in paise
            if v < 0:
                raise ValueError("amount cannot be negative")
        elif kind == "percent":
            v = float(v)
            if not 0 <= v <= 100:
                raise ValueError("percent must be 0-100")
        elif kind == "int":
            v = int(v)
            if v < 1:
                raise ValueError("count must be at least 1")
        elif kind == "bool":
            v = True
        out.append({"type": t, "value": v})
    return out


def create_rule(session: Session, user_id: str, *, product_id: str, target_price_paise: int,
                platforms: list[str] | None = None, conditions: list[dict[str, Any]] | None = None,
                approval_mode: str = "manual", approval_window_seconds: int | None = None,
                actor: AuditActor = AuditActor.USER) -> dict[str, Any]:
    if session.get(User, user_id) is None:
        raise LookupError(user_id)
    p = session.get(WatchedProduct, product_id)
    if p is None:
        raise LookupError(product_id)
    if target_price_paise <= 0:
        raise ValueError("target price must be positive")
    if target_price_paise > p.list_price_paise:
        raise ValueError(f"target price is above the MRP of {_rs(p.list_price_paise)}; the rule would fire immediately")
    plats = [x for x in (platforms or []) if x in PLATFORMS]
    if platforms and not plats:
        raise ValueError("no known platform in the list")
    mode = ApprovalMode(approval_mode)
    window = int(approval_window_seconds or config.CARD_APPROVAL_WINDOW_SECONDS)
    if not 30 <= window <= 24 * 3600:
        raise ValueError("approval window must be between 30 seconds and 24 hours")
    _card(session, user_id)   # make sure the card exists
    rule = PurchaseRule(
        user_id=user_id, product_id=product_id, target_price_paise=int(target_price_paise), platforms=plats,
        conditions=_validate_conditions(conditions), approval_mode=mode, approval_window_seconds=window,
    )
    session.add(rule)
    session.flush()
    audit_service.write(session, actor=actor, action="card_rule_created", user_id=user_id,
                        inputs={"rule_id": rule.id, "product_id": product_id, "target_price_paise": target_price_paise,
                                "conditions": rule.conditions, "approval_mode": mode.value})
    evaluate(session, rule)   # first look right away, so the screen never shows "never checked"
    return rule_view(session, rule)


def cancel_rule(session: Session, user_id: str, rule_id: str) -> dict[str, Any]:
    rule = session.get(PurchaseRule, rule_id)
    if rule is None or rule.user_id != user_id:
        raise LookupError(rule_id)
    if rule.status is RuleStatus.DONE:
        raise ValueError("this rule already completed a purchase")
    if rule.status is RuleStatus.AWAITING_APPROVAL and rule.intent_id:
        intent = mas.get(session, rule.intent_id)
        if intent.status is S.AWAITING_APPROVAL:
            mas.deny_approval(session, intent_id=intent.id, user_id=user_id)
        elif intent.status in (S.ALLOWED, S.APPROVED):
            mas.expire_unexecuted(session, intent, reason="rule cancelled")
        _unhold(session, rule)
    rule.status = RuleStatus.CANCELLED
    session.flush()
    audit_service.write(session, actor=AuditActor.USER, action="card_rule_cancelled", user_id=user_id,
                        inputs={"rule_id": rule.id})
    return rule_view(session, rule)


def resume_rule(session: Session, user_id: str, rule_id: str) -> dict[str, Any]:
    """BLOCKED -> ACTIVE after the student changed something (raised a cap,
    unfroze the card). The next tick re-evaluates from scratch."""
    rule = session.get(PurchaseRule, rule_id)
    if rule is None or rule.user_id != user_id:
        raise LookupError(rule_id)
    if rule.status is not RuleStatus.BLOCKED:
        raise ValueError("only a blocked rule can be resumed")
    rule.status = RuleStatus.ACTIVE
    rule.intent_id = None
    session.flush()
    audit_service.write(session, actor=AuditActor.USER, action="card_rule_resumed", user_id=user_id,
                        inputs={"rule_id": rule.id})
    evaluate(session, rule)
    return rule_view(session, rule)


def list_rules(session: Session, user_id: str) -> list[dict[str, Any]]:
    rows = session.execute(
        select(PurchaseRule).where(PurchaseRule.user_id == user_id, PurchaseRule.status != RuleStatus.CANCELLED)
        .order_by(PurchaseRule.created_at.desc())
    ).scalars().all()
    return [rule_view(session, r) for r in rows]


def rule_view(session: Session, rule: PurchaseRule) -> dict[str, Any]:
    p = session.get(WatchedProduct, rule.product_id)
    intent = session.get(ActionIntent, rule.intent_id) if rule.intent_id else None
    exp = _aware(rule.lock_expires_at)
    return {
        "rule_id": rule.id, "status": rule.status.value,
        "product": {"product_id": p.id, "name": p.name, "category": p.category, "list_price_paise": p.list_price_paise},
        "target_price_paise": rule.target_price_paise,
        "platforms": rule.platforms or list(PLATFORMS),
        "conditions": [{**c, "label": CONDITION_TYPES[c["type"]]["label"],
                        "value_kind": CONDITION_TYPES[c["type"]]["value_kind"]} for c in rule.conditions],
        "approval_mode": rule.approval_mode.value, "approval_window_seconds": rule.approval_window_seconds,
        "last_eval": rule.last_eval, "last_checked_at": rule.last_checked_at.isoformat() if rule.last_checked_at else None,
        "triggered_at": rule.triggered_at.isoformat() if rule.triggered_at else None,
        "lock_expires_at": exp.isoformat() if exp else None,
        "snoozed_until": _aware(rule.snoozed_until).isoformat() if rule.snoozed_until and _aware(rule.snoozed_until) > _now() else None,
        "seconds_left": max(0, int((exp - _now()).total_seconds())) if exp and _waiting_on(rule, intent) == "you" else None,
        "waiting_on": _waiting_on(rule, intent),
        "intent": None if intent is None else {"intent_id": intent.id, "status": intent.status.value,
                                               "amount_paise": intent.amount_paise, "policy": intent.policy_result},
        "result": rule.result, "created_at": rule.created_at.isoformat(),
    }


def _waiting_on(rule: PurchaseRule, intent: ActionIntent | None) -> str | None:
    """Who the ball is with, for a fired rule. Once checkout has begun the
    countdown is meaningless - no timer may close an intent the provider may
    already have taken money for, so the screen must say 'the payment is
    being confirmed', not show 0 seconds left."""
    if rule.status is not RuleStatus.AWAITING_APPROVAL or intent is None:
        return None
    if intent.status in (S.AWAITING_APPROVAL, S.ALLOWED, S.APPROVED):
        return "you"
    if intent.status in (S.EXECUTING, S.SUCCESS, S.VERIFIED):
        return "payment"
    if intent.status in (S.UNKNOWN, S.EXCEPTION):
        return "reconciliation"
    return None


# ---------------------------------------------------------------------------
# Evaluation
# ---------------------------------------------------------------------------


def _headroom(session: Session, user_id: str) -> int:
    pol = _policy(session, user_id)
    settled = ledger_service.month_spend(session, user_id, Bucket.DISCRETIONARY)
    committed = mas.committed_pending_paise(session, user_id)
    return pol.monthly_limit_paise - settled - committed


def evaluate(session: Session, rule: PurchaseRule, *, now: datetime | None = None) -> dict[str, Any]:
    """Look at the price once and decide. Writes rule.last_eval either way, so
    'watching' always shows the student exactly what was checked and why it
    did not fire yet. Fires the rule when everything holds."""
    now = now or _now()
    p = session.get(WatchedProduct, rule.product_id)
    best = _best_quote(p, rule.platforms or None)
    checks: list[dict[str, Any]] = []

    if best is None:
        price_block = {"met": False, "detail": "Out of stock on every allowed platform."}
        platform, price, stock = None, None, 0
    else:
        platform, q = best
        price, stock = int(q["price_paise"]), int(q.get("stock", 0)) - int(q.get("held", 0))
        met = price <= rule.target_price_paise
        price_block = {"met": met, "platform": platform, "platform_label": PLATFORMS[platform], "price_paise": price,
                       "stock": stock,
                       "detail": f"{PLATFORMS[platform]} has it at {_rs(price)}; your target is {_rs(rule.target_price_paise)}."
                                 + ("" if met else f" Still {_rs(price - rule.target_price_paise)} above target.")}

    for c in rule.conditions:
        t, v = c["type"], c["value"]
        label = CONDITION_TYPES[t]["label"]
        if t == "date_after":
            ok = now.date() >= date.fromisoformat(v); detail = f"today is {now.date().isoformat()}, gate opens {v}"
        elif t == "date_before":
            ok = now.date() <= date.fromisoformat(v); detail = f"today is {now.date().isoformat()}, gate closes {v}"
        elif t == "budget_remaining_gte":
            h = _headroom(session, rule.user_id); ok = h >= int(v); detail = f"{_rs(max(0, h))} of budget left, need {_rs(int(v))}"
        elif t == "min_discount_pct":
            if price is None:
                ok, detail = False, "no price to compare"
            else:
                d = (p.list_price_paise - price) / p.list_price_paise * 100; ok = d >= float(v); detail = f"{d:.1f}% off MRP, need {float(v):g}%"
        elif t == "min_stock":
            ok = stock >= int(v); detail = f"{stock} left, need {int(v)}"
        elif t == "stock_available":
            ok = stock >= 1; detail = f"{stock} in stock" if stock else "out of stock"
        else:  # unreachable: create_rule validated the type
            ok, detail = False, f"unknown condition {t!r}"
        checks.append({"type": t, "label": label, "value": v, "met": bool(ok), "detail": detail})

    all_met = bool(price_block["met"]) and all(c["met"] for c in checks)
    snooze = _aware(rule.snoozed_until)
    snoozed = bool(snooze and now < snooze)
    rule.last_eval = {"at": now.isoformat(), "price": price_block, "checks": checks, "all_met": all_met,
                      "snoozed_until": snooze.isoformat() if snoozed else None}
    rule.last_checked_at = now
    session.flush()

    if all_met and not snoozed and rule.status is RuleStatus.ACTIVE:
        _fire(session, rule, platform=platform, price_paise=price, now=now)
    return rule.last_eval


def _hold(session: Session, rule: PurchaseRule, platform: str) -> None:
    """Soft-lock one unit while the student decides (the doc's cart lock)."""
    p = session.get(WatchedProduct, rule.product_id)
    prices = dict(p.prices or {}); q = dict(prices.get(platform) or {})
    q["held"] = int(q.get("held", 0)) + 1
    prices[platform] = q; p.prices = prices
    rule.result = {**(rule.result or {}), "hold_platform": platform}
    session.flush()


def _unhold(session: Session, rule: PurchaseRule, *, consume: bool = False) -> None:
    plat = (rule.result or {}).get("hold_platform")
    if not plat:
        return
    p = session.get(WatchedProduct, rule.product_id)
    prices = dict(p.prices or {}); q = dict(prices.get(plat) or {})
    q["held"] = max(0, int(q.get("held", 0)) - 1)
    if consume:
        q["stock"] = max(0, int(q.get("stock", 0)) - 1)
    prices[plat] = q; p.prices = prices
    r = dict(rule.result or {}); r.pop("hold_platform", None); rule.result = r or None
    session.flush()


def _fire(session: Session, rule: PurchaseRule, *, platform: str, price_paise: int, now: datetime) -> None:
    """Every condition holds. Ask the policy engine - the same call every
    purchase makes - and act on ITS answer, never around it."""
    p = session.get(WatchedProduct, rule.product_id)
    purpose = f"card_rule:{rule.id}:{platform}"
    r = mas.create(session, user_id=rule.user_id, action="PURCHASE", amount_paise=price_paise, purpose=purpose,
                   bucket=Bucket.DISCRETIONARY, actor=AuditActor.SYSTEM)
    intent = r.intent
    d = r.as_dict()
    policy = d.get("policy") or {}
    rule.intent_id = intent.id
    rule.triggered_at = now
    saving = p.list_price_paise - price_paise
    head = f"{p.name} is {_rs(price_paise)} on {PLATFORMS[platform]}"
    tail = f" ({_rs(saving)} off MRP)" if saving > 0 else ""

    if intent.status is S.LEDGER_UPDATED:            # duplicate of an already-settled intent
        _complete(session, rule, intent, platform=platform, now=now)
        return

    if intent.status in (S.DENIED, S.CLOSED):
        rule.status = RuleStatus.BLOCKED
        rule.last_eval = {**(rule.last_eval or {}), "blocked_reason": policy.get("reason"), "blocked_rule": policy.get("rule")}
        session.flush()
        _notify(session, rule.user_id, NotificationKind.RULE_BLOCKED, f"Blocked: {p.name}",
                f"{head}{tail} — every condition you set is met, but the card refused it: {policy.get('reason', 'policy denied')} "
                "Change the card's limits and resume the rule, or leave it.",
                rule=rule, intent_id=intent.id, actions={"rule_id": rule.id, "resume": True})
        return

    _hold(session, rule, platform)
    rule.status = RuleStatus.AWAITING_APPROVAL
    rule.lock_expires_at = now + timedelta(seconds=rule.approval_window_seconds)
    mins = max(1, rule.approval_window_seconds // 60)
    left = max(0, int((p.prices.get(platform) or {}).get("stock", 0)) - int((p.prices.get(platform) or {}).get("held", 0)))
    stock_note = f" Only {left} left." if left <= 2 else ""

    if intent.status is S.AWAITING_APPROVAL:
        mas.set_approval_window(session, intent, seconds=rule.approval_window_seconds)
        why_ask = policy.get("reason", "above your approval line")
        auto_note = " (Auto mode cannot skip this: it is above your approval line.)" if rule.approval_mode is ApprovalMode.AUTO else ""
        _notify(session, rule.user_id, NotificationKind.RULE_NEEDS_APPROVAL, f"Approve? {p.name} at {_rs(price_paise)}",
                f"{head}{tail}.{stock_note} {why_ask}{auto_note} Tap YES within {mins} min to approve and pay.",
                rule=rule, intent_id=intent.id, actions={"rule_id": rule.id, "intent_id": intent.id, "yes_no": True})
        session.flush()
        return

    # ALLOWED
    if rule.approval_mode is ApprovalMode.AUTO and config.DEBUG:
        mas.begin_execution(session, intent, evidence={"simulated": True, "auto_rule": rule.id})
        mas.settle_success(session, intent, provider_evidence={"simulated": True, "auto_rule": rule.id,
                                                               "platform": platform, "price_paise": price_paise},
                           source=f"simulated:card_rule:{rule.id}", actor=AuditActor.SYSTEM)
        _complete(session, rule, intent, platform=platform, now=now, auto=True)
        return

    demo_note = "" if config.DEBUG or rule.approval_mode is ApprovalMode.MANUAL else " (Auto mode needs DEBUG for simulated settlement; asking you instead.)"
    _notify(session, rule.user_id, NotificationKind.RULE_TRIGGERED, f"Buy now? {p.name} at {_rs(price_paise)}",
            f"{head}{tail}.{stock_note} Within your card's rules — tap YES within {mins} min and the card pays.{demo_note}",
            rule=rule, intent_id=intent.id, actions={"rule_id": rule.id, "intent_id": intent.id, "yes_no": True})
    session.flush()


def _complete(session: Session, rule: PurchaseRule, intent: ActionIntent, *, platform: str | None, now: datetime,
              auto: bool = False) -> None:
    p = session.get(WatchedProduct, rule.product_id)
    plat = platform or (rule.result or {}).get("hold_platform") or (intent.purpose.split(":")[-1] if intent.purpose else None)
    _unhold(session, rule, consume=True)
    order_id = "SIM-" + hashlib.sha256(intent.id.encode()).hexdigest()[:8].upper()
    eta = (now + timedelta(days=3)).date().isoformat()
    rule.status = RuleStatus.DONE
    rule.lock_expires_at = None
    rule.result = {"platform": plat, "platform_label": PLATFORMS.get(plat, plat), "price_paise": intent.amount_paise,
                   "order_id": order_id, "eta": eta, "auto": auto, "simulated": True, "settled_at": now.isoformat()}
    session.flush()
    how = "auto-executed by your rule (simulated settlement)" if auto else "paid with your tap"
    _notify(session, rule.user_id, NotificationKind.PURCHASE_DONE, f"Ordered: {p.name}",
            f"{_rs(intent.amount_paise)} on {PLATFORMS.get(plat, plat)} — {how}. Order {order_id}, arrives by {eta} (demo). "
            "It is on your ledger and counts toward this month's cap.",
            rule=rule, intent_id=intent.id, actions={"rule_id": rule.id})


def _release(session: Session, rule: PurchaseRule, *, why: str, kind: NotificationKind) -> None:
    p = session.get(WatchedProduct, rule.product_id)
    _unhold(session, rule)
    rule.status = RuleStatus.ACTIVE
    rule.snoozed_until = _now() + timedelta(seconds=config.CARD_REFIRE_COOLDOWN_SECONDS)
    rule.lock_expires_at = None
    rule.triggered_at = None
    rule.intent_id = None
    session.flush()
    _notify(session, rule.user_id, kind, f"Watching again: {p.name}", why, rule=rule, actions={"rule_id": rule.id})


def _follow_up(session: Session, rule: PurchaseRule, *, now: datetime) -> None:
    """A fired rule waiting on the student: did they pay, decline, or let it lapse?"""
    if not rule.intent_id:
        rule.status = RuleStatus.ACTIVE
        return
    intent = session.get(ActionIntent, rule.intent_id)
    if intent is None:
        rule.status = RuleStatus.ACTIVE
        return
    if intent.status is S.LEDGER_UPDATED:
        _complete(session, rule, intent, platform=None, now=now)
        return
    if intent.status in (S.CLOSED, S.DENIED, S.FAILURE):
        _release(session, rule, why="That purchase did not go through (declined or failed). The card is watching the price again.",
                 kind=NotificationKind.RULE_RESUMED)
        return
    exp = _aware(rule.lock_expires_at)
    if exp and now >= exp and intent.status in (S.AWAITING_APPROVAL, S.ALLOWED, S.APPROVED):
        mas.expire_unexecuted(session, intent, reason="card rule approval window lapsed")
        mins = max(1, rule.approval_window_seconds // 60)
        _release(session, rule, why=f"No answer within {mins} min, so the hold was released and nothing was paid. "
                                    "The card keeps watching.", kind=NotificationKind.APPROVAL_EXPIRED)
    # EXECUTING / UNKNOWN / EXCEPTION: checkout or reconciliation is in charge; leave it.


def _trim_ticks(session: Session, *, keep_per_product: int = _HISTORY_POINTS * 4) -> int:
    """Ticks exist to draw a sparkline and to evidence a fired rule, not as a
    permanent record - the ledger and the audit trail are that. At one pass
    every 20 s an all-day demo would otherwise accumulate tens of thousands of
    rows in the SQLite file, so keep a rolling window per product."""
    cutoff_ids: list[str] = []
    for (pid,) in session.execute(select(WatchedProduct.id)).all():
        keep = session.execute(
            select(PriceTick.id).where(PriceTick.product_id == pid)
            .order_by(PriceTick.observed_at.desc(), PriceTick.id.desc()).limit(keep_per_product)
        ).scalars().all()
        if len(keep) < keep_per_product:
            continue
        cutoff_ids += session.execute(
            select(PriceTick.id).where(PriceTick.product_id == pid, PriceTick.id.not_in(keep))
        ).scalars().all()
    for tid in cutoff_ids:
        session.delete(session.get(PriceTick, tid))
    if cutoff_ids:
        session.flush()
    return len(cutoff_ids)


def tick(session: Session, *, now: datetime | None = None, quote: bool = True) -> dict[str, int]:
    """One pass of the monitor. Safe to call as often as you like."""
    now = now or _now()
    quoted = quote_all(session, at=now) if quote else 0
    if quoted:
        _trim_ticks(session)
    fired = 0
    rules = session.execute(
        select(PurchaseRule).where(PurchaseRule.status.in_([RuleStatus.ACTIVE, RuleStatus.AWAITING_APPROVAL]))
        .order_by(PurchaseRule.created_at)
    ).scalars().all()
    for rule in rules:
        if rule.status is RuleStatus.AWAITING_APPROVAL:
            _follow_up(session, rule, now=now)
            if rule.status is RuleStatus.ACTIVE:   # just released: refresh the checks, no re-fire this tick
                rule.snoozed_until = rule.snoozed_until or now + timedelta(seconds=config.CARD_REFIRE_COOLDOWN_SECONDS)
        if rule.status is RuleStatus.ACTIVE:
            before = rule.status
            evaluate(session, rule, now=now)
            if rule.status is not before:
                fired += 1
    session.flush()
    return {"quoted": quoted, "rules_checked": len(rules), "fired": fired}


# ---------------------------------------------------------------------------
# The screen
# ---------------------------------------------------------------------------


def card_view(session: Session, user_id: str) -> dict[str, Any]:
    events = session.execute(
        select(LedgerEvent).where(LedgerEvent.user_id == user_id, LedgerEvent.type == LedgerEventType.PURCHASE)
        .order_by(LedgerEvent.created_at.desc()).limit(10)
    ).scalars().all()
    tx = []
    for e in events:
        intent = session.get(ActionIntent, e.intent_id) if e.intent_id else None
        purpose = intent.purpose if intent else e.source
        label, via = purpose, "card"
        if purpose.startswith("card_rule:"):
            rid = purpose.split(":")[1]; rule = session.get(PurchaseRule, rid)
            prod = session.get(WatchedProduct, rule.product_id) if rule else None
            label, via = (prod.name if prod else "rule purchase"), "rule"
        elif purpose.startswith("offer:"):
            label, via = purpose.split(":", 2)[-1], "offer"
        elif purpose.startswith("seed:"):
            label, via = "Campus purchase (seeded)", "seed"
        tx.append({"event_id": e.id, "label": label, "via": via, "amount_paise": -e.amount_paise,
                   "at": e.created_at.isoformat(), "simulated": "simulated" in (e.source or "")})
    return {
        "card": card_summary(session, user_id),
        "products": list_products(session),
        "rules": list_rules(session, user_id),
        "notifications": list_notifications(session, user_id, limit=20),
        "transactions": tx,
        "platforms": [{"key": k, "label": v} for k, v in PLATFORMS.items()],
        "condition_types": [{"type": k, **v} for k, v in CONDITION_TYPES.items()],
        "poll_interval_seconds": config.PRICE_TICK_SECONDS,
        "approval_window_default_seconds": config.CARD_APPROVAL_WINDOW_SECONDS,
        "demo_notice": "Synthetic catalogue and prices. Manual purchases settle through Razorpay Test Mode; auto mode is simulated.",
    }
