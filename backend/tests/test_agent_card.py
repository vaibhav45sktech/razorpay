"""Phase 6b — the Agentic Card: rules, the price monitor, notifications, limits.

What these tests pin down:
  * the card's limits ARE the spend policy - editing them on the card changes
    what the policy engine allows everywhere, and is audited before/after;
  * rule creation is default-deny on unknown conditions and refuses a target
    above MRP; the first evaluation happens immediately;
  * evaluation is deterministic and explains itself: price check + every
    compound condition with met/not-met and a detail string;
  * when everything holds the rule fires THROUGH policy_engine: ALLOW ->
    notify + wait; REQUIRE_APPROVAL -> notify + approval window; DENY ->
    BLOCKED with the engine's reason, no intent left open;
  * YES approves and hands the browser to checkout; NO releases; a lapsed
    window expires the intent (releasing its reserved limit) and the rule
    keeps watching under a cooldown, so it does not nag on the next tick;
  * auto mode settles only when policy said ALLOW and only with DEBUG on,
    through settle_success with evidence stamped simulated; above the
    approval line, auto mode still asks;
  * the model's tools: get_agent_card is read-only; create_purchase_rule is
    gated by amount provenance, write provenance and the taint lock.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from backend import config
from backend.main import app
from backend.models.db import get_session
from backend.models.entities import (
    ActionIntent, ApprovalStatus, Approval, IntentStatus as S, PurchaseRule, RuleStatus, User, WatchedProduct,
)
from backend.seed import demo_data
from backend.services import agent_card_service as card
from backend.services import audit_service, money_action_service as mas, policy_engine

RUPEE = 100


@pytest.fixture()
def client(db):
    def _override():
        yield db
    app.dependency_overrides[get_session] = _override
    try:
        with TestClient(app) as c:
            yield c
    finally:
        app.dependency_overrides.clear()


@pytest.fixture()
def seeded(client, db) -> str:
    demo_data.seed_all(db)
    db.commit()
    return db.query(User).filter_by(name=demo_data.PRIMARY_DEMO_USER).one().id


def _product(db, name_part: str) -> WatchedProduct:
    return db.query(WatchedProduct).filter(WatchedProduct.name.like(f"%{name_part}%")).one()


def _pin(db, product: WatchedProduct, rupees: int, *, platform: str = "shopkart", stock: int = 5) -> None:
    card.set_price(db, product_id=product.id, platform=platform, price_paise=rupees * RUPEE, stock=stock)


def _rule(db, user_id: str, product: WatchedProduct, target_rupees: int, **kw) -> PurchaseRule:
    r = card.create_rule(db, user_id, product_id=product.id, target_price_paise=target_rupees * RUPEE, **kw)
    return db.get(PurchaseRule, r["rule_id"])


# ---------------------------------------------------------------------------
# The card and its limits
# ---------------------------------------------------------------------------


def test_card_view_is_issued_on_first_look_and_limits_are_the_policy(client, seeded, db) -> None:
    v = client.get(f"/api/card/{seeded}").json()
    c = v["card"]
    pol = db.get(User, seeded).spend_policy
    assert len(c["last4"]) == 4 and c["is_synthetic"] is True
    assert c["monthly_limit_paise"] == pol.monthly_limit_paise
    assert c["approval_threshold_paise"] == pol.approval_threshold_paise
    assert c["per_tx_limit_paise"] == pol.per_tx_limit_paise
    assert c["frozen"] is pol.paused
    assert c["spent_this_month_paise"] == 240 * RUPEE and c["headroom_paise"] == 760 * RUPEE
    assert len(v["products"]) == 6 and all(p["is_synthetic"] for p in v["products"])
    assert all(p["best"] is not None for p in v["products"]), "every product starts in stock somewhere"
    assert {ct["type"] for ct in v["condition_types"]} == set(card.CONDITION_TYPES)
    assert len(v["rules"]) == 1, "the seed leaves one live rule so the screen is never empty"


def test_changing_card_limits_changes_what_the_policy_engine_allows_everywhere(client, seeded, db) -> None:
    # ₹900 purchase: over the ₹760 headroom -> DENY today.
    before = policy_engine.check_policy(db, user_id=seeded, action="PURCHASE", amount_paise=900 * RUPEE, purpose="x")
    assert before.decision.value == "DENY" and before.rule == "monthly_limit"

    r = client.patch(f"/api/card/{seeded}/limits", json={"monthly_limit_rupees": 3000, "per_tx_limit_rupees": 1000})
    assert r.status_code == 200, r.text
    assert r.json()["monthly_limit_paise"] == 3000 * RUPEE and r.json()["per_tx_limit_paise"] == 1000 * RUPEE

    after = policy_engine.check_policy(db, user_id=seeded, action="PURCHASE", amount_paise=900 * RUPEE, purpose="x")
    assert after.decision.value == "REQUIRE_APPROVAL", "now inside the cap, above the ₹500 line"
    big = policy_engine.check_policy(db, user_id=seeded, action="PURCHASE", amount_paise=1200 * RUPEE, purpose="x")
    assert big.decision.value == "DENY" and big.rule == "per_tx_limit", "the new per-purchase cap bites"

    from backend.models.entities import AuditEvent
    changed = db.query(AuditEvent).filter_by(action="card_limits_changed").all()
    assert changed, "limit changes are on the record"
    notes = client.get(f"/api/card/{seeded}/notifications").json()
    assert any(n["kind"] == "card_limits_changed" for n in notes["items"])


def test_freezing_the_card_pauses_all_purchases(client, seeded, db) -> None:
    client.patch(f"/api/card/{seeded}/limits", json={"frozen": True})
    res = policy_engine.check_policy(db, user_id=seeded, action="PURCHASE", amount_paise=100 * RUPEE, purpose="x")
    assert res.decision.value == "DENY" and res.rule == "paused"
    assert client.get(f"/api/card/{seeded}").json()["card"]["frozen"] is True


def test_limit_validation(client, seeded) -> None:
    assert client.patch(f"/api/card/{seeded}/limits", json={"monthly_limit_rupees": -1}).status_code == 422
    assert client.patch(f"/api/card/{seeded}/limits", json={"monthly_limit_rupees": 99_999}).status_code == 400
    assert client.patch(f"/api/card/{seeded}/limits", json={"per_tx_limit_rupees": 0}).status_code == 422


# ---------------------------------------------------------------------------
# Rules: creation, validation, evaluation
# ---------------------------------------------------------------------------


def test_create_rule_validates_and_evaluates_immediately(client, seeded, db) -> None:
    fan = _product(db, "table fan")
    r = client.post(f"/api/card/{seeded}/rules", json={
        "product_id": fan.id, "target_price_rupees": 1000,
        "conditions": [{"type": "date_after", "value": "2026-01-01"}, {"type": "budget_remaining_gte", "value": 500}],
    })
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["status"] == "active" and body["approval_mode"] == "manual"
    assert body["approval_window_seconds"] == config.CARD_APPROVAL_WINDOW_SECONDS
    assert body["last_eval"] is not None, "checked on creation, never 'never checked'"
    assert body["last_eval"]["price"]["detail"].startswith(("ShopKart", "Bazaario"))
    checks = {c["type"]: c for c in body["last_eval"]["checks"]}
    assert checks["date_after"]["met"] is True
    assert checks["budget_remaining_gte"]["met"] is True and "₹760" in checks["budget_remaining_gte"]["detail"]
    assert checks["budget_remaining_gte"]["value"] == 500 * RUPEE, "rupees in, paise stored"


def test_create_rule_refuses_unknown_conditions_and_target_above_mrp(client, seeded, db) -> None:
    fan = _product(db, "table fan")
    bad = client.post(f"/api/card/{seeded}/rules", json={"product_id": fan.id, "target_price_rupees": 1000,
                                                          "conditions": [{"type": "price_history_ok", "value": 1}]})
    assert bad.status_code == 400 and "unsupported condition" in bad.json()["detail"]
    high = client.post(f"/api/card/{seeded}/rules", json={"product_id": fan.id, "target_price_rupees": 5000})
    assert high.status_code == 400 and "MRP" in high.json()["detail"]
    assert client.post(f"/api/card/{seeded}/rules", json={"product_id": "prd_nope", "target_price_rupees": 10}).status_code == 404
    assert client.post(f"/api/card/{seeded}/rules", json={"product_id": fan.id, "target_price_rupees": 100,
                                                           "approval_window_seconds": 5}).status_code == 422


def test_evaluation_explains_why_a_rule_has_not_fired(db, seeded) -> None:
    fan = _product(db, "table fan")
    _pin(db, fan, 1200, platform="shopkart"); _pin(db, fan, 1250, platform="bazaario")
    rule = _rule(db, seeded, fan, 1000, conditions=[{"type": "min_discount_pct", "value": 30}])
    ev = card.evaluate(db, rule)
    assert ev["all_met"] is False and rule.status is RuleStatus.ACTIVE
    assert ev["price"]["met"] is False and ev["price"]["platform"] == "shopkart", "cheapest in-stock platform is used"
    assert "₹200 above target" in ev["price"]["detail"]
    assert ev["checks"][0]["met"] is False and "11.1% off MRP, need 30%" in ev["checks"][0]["detail"]


def test_out_of_stock_everywhere_never_fires(db, seeded) -> None:
    fan = _product(db, "table fan")
    _pin(db, fan, 500, platform="shopkart", stock=0); _pin(db, fan, 500, platform="bazaario", stock=0)
    rule = _rule(db, seeded, fan, 1000)
    ev = card.evaluate(db, rule)
    assert ev["price"]["met"] is False and "Out of stock" in ev["price"]["detail"]
    assert rule.status is RuleStatus.ACTIVE and rule.intent_id is None


def test_platform_restriction_is_honoured(db, seeded) -> None:
    fan = _product(db, "table fan")
    _pin(db, fan, 500, platform="shopkart"); _pin(db, fan, 1300, platform="bazaario")
    rule = _rule(db, seeded, fan, 1000, platforms=["bazaario"])
    ev = card.evaluate(db, rule)
    assert ev["price"]["platform"] == "bazaario" and ev["price"]["met"] is False


# ---------------------------------------------------------------------------
# Firing THROUGH the policy engine
# ---------------------------------------------------------------------------


def test_fire_within_limits_creates_allowed_intent_and_a_yes_no_notification(db, seeded) -> None:
    meal = _product(db, "meal-card")
    _pin(db, meal, 440, stock=2)
    rule = _rule(db, seeded, meal, 450)
    assert rule.status is RuleStatus.AWAITING_APPROVAL, "fired on the immediate first evaluation"
    intent = db.get(ActionIntent, rule.intent_id)
    assert intent.status is S.ALLOWED and intent.amount_paise == 440 * RUPEE
    assert intent.purpose == f"card_rule:{rule.id}:shopkart"
    assert rule.lock_expires_at is not None
    notes = card.list_notifications(db, seeded)
    top = notes["items"][0]
    assert top["kind"] == "rule_triggered" and top["actionable"] is True and top["actions"]["yes_no"] is True
    assert "Only 1 left" in top["body"], "one unit is soft-held while the student decides"
    assert card.card_view(db, seeded)["products"][0]["quotes"][0]["available_stock"] == 1


def test_fire_above_approval_line_requires_approval_with_a_window(db, seeded) -> None:
    fan = _product(db, "table fan")
    _pin(db, fan, 700)
    rule = _rule(db, seeded, fan, 750, approval_window_seconds=120)
    intent = db.get(ActionIntent, rule.intent_id)
    assert intent.status is S.AWAITING_APPROVAL
    apr = db.query(Approval).filter_by(intent_id=intent.id).one()
    assert apr.expires_at is not None
    assert 100 <= (apr.expires_at.replace(tzinfo=timezone.utc) - datetime.now(timezone.utc)).total_seconds() <= 120
    top = card.list_notifications(db, seeded)["items"][0]
    assert top["kind"] == "rule_needs_approval" and "approval threshold" in top["body"]


def test_fire_over_the_cap_is_blocked_with_the_engines_reason_and_no_open_intent(db, seeded) -> None:
    phone = _product(db, "smartphone")
    _pin(db, phone, 7999)
    rule = _rule(db, seeded, phone, 8000)
    assert rule.status is RuleStatus.BLOCKED
    assert rule.last_eval["blocked_rule"] == "monthly_limit" and "₹1,000" in rule.last_eval["blocked_reason"]
    intent = db.get(ActionIntent, rule.intent_id)
    assert intent.status is S.CLOSED, "a denied intent is closed - it reserves nothing"
    assert mas.committed_pending_paise(db, seeded) == 0
    top = card.list_notifications(db, seeded)["items"][0]
    assert top["kind"] == "rule_blocked" and top["actions"].get("resume") is True
    # Blocked rules are not re-evaluated on ticks (no notification storm).
    n_before = len(card.list_notifications(db, seeded)["items"])
    card.tick(db, quote=False)
    assert len(card.list_notifications(db, seeded)["items"]) == n_before


def test_raise_cap_then_resume_lets_the_same_rule_fire(client, seeded, db) -> None:
    phone = _product(db, "smartphone")
    _pin(db, phone, 7999)
    rule = _rule(db, seeded, phone, 8000)
    assert rule.status is RuleStatus.BLOCKED
    assert client.post(f"/api/card/{seeded}/rules/{rule.id}/resume").status_code == 200
    db.refresh(rule)
    assert rule.status is RuleStatus.BLOCKED, "resuming without changing anything just blocks again"
    client.patch(f"/api/card/{seeded}/limits", json={"monthly_limit_rupees": 20000})
    r = client.post(f"/api/card/{seeded}/rules/{rule.id}/resume")
    assert r.json()["status"] == "awaiting_approval" and r.json()["intent"]["status"] == "AWAITING_APPROVAL"


def test_frozen_card_blocks_a_rule_at_fire_time(db, seeded) -> None:
    meal = _product(db, "meal-card")
    card.update_limits(db, seeded, frozen=True)
    _pin(db, meal, 400)
    rule = _rule(db, seeded, meal, 450)
    assert rule.status is RuleStatus.BLOCKED and rule.last_eval["blocked_rule"] == "paused"


# ---------------------------------------------------------------------------
# YES / NO / lapse
# ---------------------------------------------------------------------------


def test_yes_on_an_allowed_rule_hands_the_browser_to_checkout(client, seeded, db) -> None:
    meal = _product(db, "meal-card"); _pin(db, meal, 440)
    rule = _rule(db, seeded, meal, 450); db.commit()
    nid = card.list_notifications(db, seeded)["items"][0]["notification_id"]
    r = client.post(f"/api/card/{seeded}/notifications/{nid}/respond", json={"answer": "yes"})
    assert r.status_code == 200, r.text
    assert r.json()["next"] == "pay" and r.json()["intent_id"] == rule.intent_id and r.json()["status"] == "ALLOWED"
    # Settle as checkout-verify would; the next tick marks the rule DONE.
    intent = mas.get(db, rule.intent_id)
    mas.begin_execution(db, intent, evidence={"debug": True})
    mas.settle_success(db, intent, provider_evidence={"debug": True}, source="debug:test")
    card.tick(db, quote=False); db.refresh(rule)
    assert rule.status is RuleStatus.DONE
    assert rule.result["price_paise"] == 440 * RUPEE and rule.result["order_id"].startswith("SIM-")
    assert card.list_notifications(db, seeded)["items"][0]["kind"] == "purchase_done"
    v = card.card_view(db, seeded)
    assert v["card"]["spent_this_month_paise"] == (240 + 440) * RUPEE, "it is on the ledger and counts toward the cap"
    assert v["transactions"][0]["via"] == "rule" and v["transactions"][0]["amount_paise"] == 440 * RUPEE
    assert v["products"][0]["quotes"][0]["stock"] == 4, "one unit consumed from the seeded 5"


def test_yes_on_a_requires_approval_rule_approves_first(client, seeded, db) -> None:
    fan = _product(db, "table fan"); _pin(db, fan, 700)
    rule = _rule(db, seeded, fan, 750); db.commit()
    nid = card.list_notifications(db, seeded)["items"][0]["notification_id"]
    r = client.post(f"/api/card/{seeded}/notifications/{nid}/respond", json={"answer": "yes"}).json()
    assert r["next"] == "pay" and r["status"] == "APPROVED"
    assert db.query(Approval).filter_by(intent_id=rule.intent_id).one().status is ApprovalStatus.GRANTED


def test_no_releases_the_hold_and_the_rule_keeps_watching_under_cooldown(client, seeded, db) -> None:
    meal = _product(db, "meal-card"); _pin(db, meal, 440, stock=3)
    rule = _rule(db, seeded, meal, 450); db.commit()
    nid = card.list_notifications(db, seeded)["items"][0]["notification_id"]
    r = client.post(f"/api/card/{seeded}/notifications/{nid}/respond", json={"answer": "no"})
    assert r.status_code == 200
    db.refresh(rule)
    assert rule.status is RuleStatus.ACTIVE and rule.intent_id is None and rule.lock_expires_at is None
    assert mas.get(db, r.json()["intent_id"]).status is S.CLOSED
    assert mas.committed_pending_paise(db, seeded) == 0, "reserved limit released"
    assert card.card_view(db, seeded)["products"][0]["quotes"][0]["available_stock"] == 3
    # Price is still below target, but the cooldown stops an immediate re-fire.
    card.tick(db, quote=False); db.refresh(rule)
    assert rule.status is RuleStatus.ACTIVE and rule.last_eval["all_met"] is True and rule.last_eval["snoozed_until"]
    # Cooldown over -> fires again.
    rule.snoozed_until = datetime.now(timezone.utc) - timedelta(seconds=1); db.flush()
    card.tick(db, quote=False); db.refresh(rule)
    assert rule.status is RuleStatus.AWAITING_APPROVAL


def test_lapsed_window_expires_the_intent_and_releases_everything(db, seeded) -> None:
    fan = _product(db, "table fan"); _pin(db, fan, 700)
    rule = _rule(db, seeded, fan, 750)
    intent = db.get(ActionIntent, rule.intent_id)
    assert intent.status is S.AWAITING_APPROVAL and mas.committed_pending_paise(db, seeded) == 700 * RUPEE
    rule.lock_expires_at = datetime.now(timezone.utc) - timedelta(seconds=1); db.flush()
    card.tick(db, quote=False); db.refresh(rule)
    assert intent.status is S.CLOSED
    assert db.query(Approval).filter_by(intent_id=intent.id).one().status is ApprovalStatus.EXPIRED
    assert rule.status is RuleStatus.ACTIVE and rule.intent_id is None
    assert mas.committed_pending_paise(db, seeded) == 0
    assert card.list_notifications(db, seeded)["items"][0]["kind"] == "approval_expired"
    assert audit_service.verify_chain(db).ok


def test_allowed_intent_can_expire_but_executing_cannot(db, seeded) -> None:
    """The new ALLOWED -> CLOSED edge exists for lapsed windows only; once
    checkout has begun, a timer must never close it - reconciliation decides."""
    meal = _product(db, "meal-card"); _pin(db, meal, 440)
    rule = _rule(db, seeded, meal, 450)
    intent = db.get(ActionIntent, rule.intent_id)
    mas.begin_execution(db, intent, evidence={"debug": True})
    rule.lock_expires_at = datetime.now(timezone.utc) - timedelta(seconds=1); db.flush()
    card.tick(db, quote=False); db.refresh(rule)
    assert intent.status is S.EXECUTING and rule.status is RuleStatus.AWAITING_APPROVAL
    with pytest.raises(mas.IllegalTransition):
        mas.expire_unexecuted(db, intent, reason="test")


def test_cancel_rule_closes_its_open_intent(client, seeded, db) -> None:
    fan = _product(db, "table fan"); _pin(db, fan, 700)
    rule = _rule(db, seeded, fan, 750); db.commit()
    r = client.delete(f"/api/card/{seeded}/rules/{rule.id}")
    assert r.status_code == 200 and r.json()["status"] == "cancelled"
    assert mas.get(db, rule.intent_id).status is S.CLOSED
    assert all(x["rule_id"] != rule.id for x in client.get(f"/api/card/{seeded}").json()["rules"])


def test_a_user_cannot_touch_another_users_rule_or_notification(client, seeded, db) -> None:
    diya = db.query(User).filter(User.name.like("Diya%")).one().id
    meal = _product(db, "meal-card"); _pin(db, meal, 440)
    rule = _rule(db, seeded, meal, 450); db.commit()
    nid = card.list_notifications(db, seeded)["items"][0]["notification_id"]
    assert client.post(f"/api/card/{diya}/notifications/{nid}/respond", json={"answer": "yes"}).status_code == 404
    assert client.delete(f"/api/card/{diya}/rules/{rule.id}").status_code == 404


# ---------------------------------------------------------------------------
# Auto mode
# ---------------------------------------------------------------------------


def test_auto_mode_settles_an_allowed_purchase_simulated_with_debug_on(db, seeded, monkeypatch) -> None:
    monkeypatch.setattr(config, "DEBUG", True)
    meal = _product(db, "meal-card"); _pin(db, meal, 440)
    rule = _rule(db, seeded, meal, 450, approval_mode="auto")
    assert rule.status is RuleStatus.DONE and rule.result["auto"] is True and rule.result["simulated"] is True
    intent = db.get(ActionIntent, rule.intent_id)
    assert intent.status is S.LEDGER_UPDATED
    from backend.models.entities import LedgerEvent
    ev = db.query(LedgerEvent).filter_by(intent_id=intent.id).one()
    assert ev.source.startswith("simulated:card_rule:") and ev.amount_paise == -440 * RUPEE
    assert "auto-executed" in card.list_notifications(db, seeded)["items"][0]["body"]
    assert audit_service.verify_chain(db).ok


def test_auto_mode_cannot_skip_the_approval_line(db, seeded, monkeypatch) -> None:
    monkeypatch.setattr(config, "DEBUG", True)
    fan = _product(db, "table fan"); _pin(db, fan, 700)
    rule = _rule(db, seeded, fan, 750, approval_mode="auto")
    assert rule.status is RuleStatus.AWAITING_APPROVAL
    assert db.get(ActionIntent, rule.intent_id).status is S.AWAITING_APPROVAL
    top = card.list_notifications(db, seeded)["items"][0]
    assert top["kind"] == "rule_needs_approval" and "Auto mode cannot skip" in top["body"]


def test_auto_mode_falls_back_to_asking_with_debug_off(db, seeded, monkeypatch) -> None:
    monkeypatch.setattr(config, "DEBUG", False)
    meal = _product(db, "meal-card"); _pin(db, meal, 440)
    rule = _rule(db, seeded, meal, 450, approval_mode="auto")
    assert rule.status is RuleStatus.AWAITING_APPROVAL
    assert db.get(ActionIntent, rule.intent_id).status is S.ALLOWED, "nothing settled without a provider"
    assert "DEBUG" in card.list_notifications(db, seeded)["items"][0]["body"]


# ---------------------------------------------------------------------------
# The monitor and the debug levers
# ---------------------------------------------------------------------------


def test_quote_all_is_deterministic_and_bounded(db, seeded) -> None:
    at = datetime(2026, 9, 5, 12, 0, tzinfo=timezone.utc)
    before = {p.id: dict(p.prices) for p in db.query(WatchedProduct).all()}
    card.quote_all(db, at=at)
    after1 = {p.id: {k: v["price_paise"] for k, v in p.prices.items()} for p in db.query(WatchedProduct).all()}
    for p in db.query(WatchedProduct).all():       # rewind and replay the same instant
        p.prices = before[p.id]
    db.flush()
    card.quote_all(db, at=at)
    after2 = {p.id: {k: v["price_paise"] for k, v in p.prices.items()} for p in db.query(WatchedProduct).all()}
    assert after1 == after2, "same inputs, same quotes"
    for p in db.query(WatchedProduct).all():
        for q in p.prices.values():
            assert int(p.list_price_paise * 0.6) <= q["price_paise"] <= p.list_price_paise
            assert q["price_paise"] % 100 == 0, "whole rupees"


def test_pinned_price_survives_ticks_and_tick_endpoint_runs_the_monitor(client, seeded, db) -> None:
    meal = _product(db, "meal-card")
    r = client.post(f"/debug/card/price/{meal.id}", json={"platform": "shopkart", "price_rupees": 431, "stock": 4})
    assert r.status_code == 200 and r.json()["debug"] is True
    card.quote_all(db)
    assert db.get(WatchedProduct, meal.id).prices["shopkart"]["price_paise"] == 431 * RUPEE
    t = client.post(f"/api/card/{seeded}/tick").json()
    assert set(t) == {"quoted", "rules_checked", "fired"} and t["quoted"] >= 11  # the pinned quote is skipped


def test_debug_levers_404_with_debug_off(client, seeded, db, monkeypatch) -> None:
    monkeypatch.setattr(config, "DEBUG", False)
    meal = _product(db, "meal-card")
    assert client.post(f"/debug/card/price/{meal.id}", json={"price_rupees": 1}).status_code == 404


def test_expire_now_lever_demonstrates_the_release_path(client, seeded, db) -> None:
    fan = _product(db, "table fan"); _pin(db, fan, 700)
    rule = _rule(db, seeded, fan, 750); db.commit()
    intent_id = rule.intent_id
    r = client.post(f"/debug/card/rules/{rule.id}/expire-now")
    assert r.status_code == 200 and r.json()["rule"]["status"] == "active"
    assert mas.get(db, intent_id).status is S.CLOSED


def test_notifications_mark_read(client, seeded, db) -> None:
    meal = _product(db, "meal-card"); _pin(db, meal, 440)
    _rule(db, seeded, meal, 450); db.commit()
    assert client.get(f"/api/card/{seeded}/notifications").json()["unread"] >= 1
    assert client.post(f"/api/card/{seeded}/notifications/read", json={}).json()["marked"] >= 1
    assert client.get(f"/api/card/{seeded}/notifications").json()["unread"] == 0


# ---------------------------------------------------------------------------
# The model's tools
# ---------------------------------------------------------------------------


def test_get_agent_card_tool_is_read_only_and_explains_a_waiting_rule(db, seeded) -> None:
    from backend.agent import tool_registry
    from backend.models.schemas import NoArgs

    fan = _product(db, "table fan"); _pin(db, fan, 1200)
    rule = _rule(db, seeded, fan, 1000, conditions=[{"type": "min_discount_pct", "value": 30}])
    tool = tool_registry.get("get_agent_card")
    assert tool.caller is tool_registry.Caller.LLM and tool.args_json_schema().get("properties", {}) == {}
    n_intents = db.query(ActionIntent).count()
    out = tool.handler(db, seeded, NoArgs()).model_dump()
    assert db.query(ActionIntent).count() == n_intents
    assert out["card"]["monthly_cap_paise"] == 1000 * RUPEE and out["card"]["headroom_paise"] == 760 * RUPEE
    mine = next(r for r in out["rules"] if r["rule_id"] == rule.id)
    assert mine["status"] == "active" and mine["last_check"]["price_met"] is False
    assert "above target" in mine["last_check"]["why_not_yet"] and "need 30%" in mine["last_check"]["why_not_yet"]
    assert any(p["product_id"] == fan.id and p["best_price_paise"] == 1200 * RUPEE for p in out["products"])


def test_create_purchase_rule_tool_is_gated_like_a_money_tool(db, seeded) -> None:
    from backend.agent import orchestrator, tool_registry

    fan = _product(db, "table fan")
    tool = tool_registry.get("create_purchase_rule")
    assert tool.caller is tool_registry.Caller.LLM
    args = {"product_id": fan.id, "target_price_rupees": 1000}

    # 1. Amount the user never typed -> blocked as invented.
    r = orchestrator.execute_tool(db, seeded, tool, dict(args), stated_amounts=frozenset({500 * RUPEE}),
                                  user_said="watch the fan and buy it when it drops to ₹500")
    assert r.get("blocked") and "never stated an amount" in r["reason"]

    # 2. Right amount, but the user never asked to watch anything -> blocked.
    r = orchestrator.execute_tool(db, seeded, tool, dict(args), stated_amounts=frozenset({1000 * RUPEE}),
                                  user_said="what's my balance? i paid ₹1000 for books")
    assert r.get("blocked") and r["reason"].startswith("The user did not ask to watch")

    # 3. Injected text earlier in the turn -> taint lock, like a money tool.
    r = orchestrator.execute_tool(db, seeded, tool, dict(args), stated_amounts=frozenset({1000 * RUPEE}),
                                  user_said="watch the fan, buy when it drops to ₹1000", money_locked_reason="planted offer title")
    assert r.get("blocked") and r["decision"] == "BLOCKED"
    assert db.query(PurchaseRule).filter_by(product_id=fan.id).count() == 0

    # 4. All three satisfied -> a rule, and only a rule.
    n_intents = db.query(ActionIntent).count()
    r = orchestrator.execute_tool(db, seeded, tool, {**args, "only_after_date": "2026-01-01"},
                                  stated_amounts=frozenset({1000 * RUPEE}),
                                  user_said="watch the fan, buy when it drops to ₹1000")
    assert not r.get("blocked") and not r.get("error"), r
    assert r["status"] == "active" and r["conditions"] == [{"rule": "Only on or after a date", "value": "2026-01-01"}]
    assert "nothing was bought" in r["what_happens_next"]
    assert db.query(ActionIntent).count() == n_intents, "a rule is not an intent"
    assert db.query(PurchaseRule).filter_by(product_id=fan.id).count() == 1


def test_seed_is_idempotent_for_card_tables(db, seeded) -> None:
    from backend.models.entities import VirtualCard
    counts = (db.query(WatchedProduct).count(), db.query(PurchaseRule).count(), db.query(VirtualCard).count())
    assert demo_data.seed_all(db) is False
    assert counts == (db.query(WatchedProduct).count(), db.query(PurchaseRule).count(), db.query(VirtualCard).count())
    demo_data.seed_all(db, force=True)
    assert db.query(WatchedProduct).count() == 6 and db.query(VirtualCard).count() == 2
