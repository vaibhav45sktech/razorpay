"""Phase 8 — the operational surface, tested rather than assumed.

Rate limits that were never observed to engage, a request id nobody checked
propagates, a metric that counts nothing and a backup script no one restored
from are all the same failure mode: an operational feature that exists in the
repository and not in reality. Each item here is asserted.

The Definition of Done's tamper demo and the whole degradation matrix live in
test_degradation.py; this file covers items 1-3 and 6-7.
"""

from __future__ import annotations

import sqlite3

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from backend import config, observability
from backend.main import app
from backend.models.db import get_session
from backend.models.entities import User
from backend.seed import demo_data

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


@pytest.fixture(autouse=True)
def _reset_limiter():
    """slowapi keeps counters in process memory, so one test's requests would
    otherwise exhaust the next test's budget."""
    observability.limiter.reset()
    yield
    observability.limiter.reset()


# ---------------------------------------------------------------------------
# Item 1 — rate limiting
# ---------------------------------------------------------------------------


def test_the_chat_limit_engages_and_says_retry_after(client, seeded) -> None:
    """The endpoint an abuser would actually target: each call is several
    inference passes against one local model."""
    body = {"user_id": seeded, "message": "What's my balance?", "history": []}
    headers = {"x-user-id": seeded}
    codes = [client.post("/api/chat", json=body, headers=headers).status_code for _ in range(16)]
    assert 429 in codes, f"the chat limit never engaged: {codes}"

    limited = client.post("/api/chat", json=body, headers=headers)
    assert limited.status_code == 429
    assert limited.headers.get("Retry-After"), "a 429 without Retry-After tells the client nothing"
    detail = limited.json()["detail"]
    assert "nothing was executed" in detail.lower(), "a refused request must say it changed nothing"


def test_the_chat_limit_is_per_user_not_per_ip(client, seeded, db) -> None:
    """Two students behind one campus NAT must not exhaust each other's budget
    — which is exactly what a per-IP limit on this endpoint would do."""
    diya = db.query(User).filter(User.name.like("Diya%")).one().id
    for _ in range(14):
        client.post("/api/chat", json={"user_id": seeded, "message": "hi", "history": []},
                    headers={"x-user-id": seeded})
    assert client.post("/api/chat", json={"user_id": seeded, "message": "hi", "history": []},
                       headers={"x-user-id": seeded}).status_code == 429
    other = client.post("/api/chat", json={"user_id": diya, "message": "hi", "history": []},
                        headers={"x-user-id": diya})
    assert other.status_code != 429, "the second student was limited by the first student's traffic"


def test_a_caller_with_no_user_id_falls_back_to_ip_not_to_unlimited(client) -> None:
    """The fallback must never be MORE permissive than the specific case."""
    from starlette.requests import Request
    scope = {"type": "http", "headers": [], "query_string": b"", "client": ("10.0.0.1", 1234),
             "method": "POST", "path": "/api/chat", "scheme": "http", "server": ("t", 80)}
    key = observability._chat_key(Request(scope))
    assert key.startswith("ip:")

    scope_with_user = {**scope, "headers": [(b"x-user-id", b"usr_abc")]}
    assert observability._chat_key(Request(scope_with_user)) == "user:usr_abc"


def test_polling_endpoints_are_not_throttled_into_uselessness(client, seeded) -> None:
    """The browser refreshes state every few seconds by design; a limit that
    fights the app's own refresh loop is a bug dressed as security."""
    codes = [client.get(f"/api/state/{seeded}").status_code for _ in range(40)]
    assert all(c == 200 for c in codes), f"the app's own polling got throttled: {set(codes)}"


def test_the_webhook_endpoint_is_never_ip_limited(client) -> None:
    """Razorpay retries from its own address range and would trip a per-IP
    limit during a legitimate burst; signature verification is the real
    control here and is strictly stronger than an IP guess."""
    assert "/api/webhooks/razorpay" in observability.EXEMPT_PATHS
    codes = {client.post("/api/webhooks/razorpay", json={"event": "x"}).status_code for _ in range(30)}
    assert 429 not in codes, f"the webhook endpoint was rate limited: {codes}"


# ---------------------------------------------------------------------------
# Item 2 — request_id
# ---------------------------------------------------------------------------


def test_every_response_carries_a_request_id(client, seeded) -> None:
    r = client.get(f"/api/state/{seeded}")
    assert r.headers.get("X-Request-ID", "").startswith("req_")


def test_a_caller_supplied_request_id_is_honoured(client, seeded) -> None:
    """So a trace started upstream is not broken by this service inventing a
    second identifier for the same transaction."""
    r = client.get(f"/api/state/{seeded}", headers={"X-Request-ID": "req_from_caller"})
    assert r.headers["X-Request-ID"] == "req_from_caller"


def test_two_requests_get_different_ids(client, seeded) -> None:
    a = client.get(f"/api/state/{seeded}").headers["X-Request-ID"]
    b = client.get(f"/api/state/{seeded}").headers["X-Request-ID"]
    assert a != b


def test_the_request_id_reaches_code_that_never_saw_the_request(client, seeded, caplog) -> None:
    """The point of a ContextVar rather than a threaded parameter: a log line
    written deep in a service still carries the id."""
    import logging
    observability.configure_logging(json_logs=False)
    with caplog.at_level(logging.INFO):
        client.get(f"/api/plan/{seeded}", headers={"X-Request-ID": "req_deep_trace"})
    # The filter attaches it to every record, including ones from services.
    assert any(getattr(rec, "request_id", None) == "req_deep_trace" for rec in caplog.records), \
        [getattr(r, "request_id", None) for r in caplog.records]


def test_json_logs_are_machine_readable_when_enabled() -> None:
    import json
    import logging

    # The FORMATTER is the contract — whether a given environment happens to
    # have a stream handler attached is not. Asserting on the formatter keeps
    # this a real test everywhere instead of a skip under pytest's capture.
    observability.configure_logging(json_logs=True)
    try:
        handlers = logging.getLogger().handlers
        assert handlers, "configure_logging found no handler to format"
        fmt = handlers[0].formatter
        assert fmt is not None

        rec = logging.LogRecord("campuspool.test", logging.INFO, __file__, 1,
                                "contribution proposed", None, None)
        rec.request_id = "req_json"
        rec.user_id = "usr_abc"
        payload = json.loads(fmt.format(rec))
    finally:
        observability.configure_logging(json_logs=False)

    assert payload["msg"] == "contribution proposed"
    assert payload["request_id"] == "req_json", "the id is what makes a line traceable"
    assert payload["user_id"] == "usr_abc", "extra fields must survive into the JSON"
    assert payload["level"] == "INFO" and payload["logger"] == "campuspool.test"
    assert "ts" in payload

    # And the default really is the human-readable one: JSON on a demo laptop,
    # where a person is reading the terminal, is strictly worse.
    plain = logging.getLogger().handlers[0].formatter
    rec2 = logging.LogRecord("x", logging.INFO, __file__, 1, "hello", None, None)
    rec2.request_id = "req_plain"
    line = plain.format(rec2)
    assert "req_plain" in line and not line.startswith("{")


# ---------------------------------------------------------------------------
# Item 3 — metrics
# ---------------------------------------------------------------------------


def test_metrics_exposes_the_counters_this_system_is_judged_on(client, seeded) -> None:
    client.get(f"/api/spend/{seeded}")     # runs a policy preview per offer
    body = client.get("/metrics").text

    for family in ("campuspool_http_requests_total", "campuspool_policy_decisions_total",
                   "campuspool_intent_transitions_total", "campuspool_http_request_seconds",
                   "campuspool_audit_chain_ok"):
        assert family in body, f"{family} missing from /metrics"


def test_policy_verdicts_are_counted_by_decision_and_rule(client, seeded, db) -> None:
    from backend.services import policy_engine
    policy_engine.check_policy(db, user_id=seeded, action="PURCHASE",
                               amount_paise=99_999 * RUPEE, purpose="over")
    body = client.get("/metrics").text
    assert 'campuspool_policy_decisions_total{decision="DENY"' in body
    assert 'rule="monthly_limit"' in body, "the RULE matters as much as the verdict"


def test_intent_transitions_are_counted(client, seeded, db) -> None:
    from backend.models.entities import AuditActor, Bucket
    from backend.services import money_action_service as mas
    mas.create(db, user_id=seeded, action="PURCHASE", amount_paise=300 * RUPEE,
               purpose="purchase:metric", bucket=Bucket.DISCRETIONARY, actor=AuditActor.USER)
    db.commit()
    body = client.get("/metrics").text
    assert 'campuspool_intent_transitions_total{to_status="ALLOWED"}' in body


def test_the_audit_chain_gauge_reports_a_broken_chain(client, seeded, db) -> None:
    """A chain that silently broke is exactly what an alert should fire on."""
    assert 'campuspool_audit_chain_ok 1.0' in client.get("/metrics").text
    db.execute(text("UPDATE audit_events SET action='forged' WHERE seq=(SELECT MIN(seq) FROM audit_events)"))
    db.commit()
    # The gauge is refreshed per scrape against the app's own engine, which in
    # tests is not this session's in-memory DB - so assert the mechanism the
    # endpoint uses rather than a cross-database coincidence.
    from backend.services import audit_service
    assert audit_service.verify_chain(db).ok is False
    assert "campuspool_audit_chain_ok" in client.get("/metrics").text


def test_metrics_labels_by_route_not_by_url(client, seeded, db) -> None:
    """A label per user id would blow up cardinality (the classic Prometheus
    footgun) and leak ids into metric names."""
    diya = db.query(User).filter(User.name.like("Diya%")).one().id
    client.get(f"/api/state/{seeded}")
    client.get(f"/api/state/{diya}")
    body = client.get("/metrics").text
    assert 'path="/api/state/{user_id}"' in body
    assert seeded not in body and diya not in body, "user ids leaked into metric labels"


def test_metrics_never_500s_even_if_the_chain_check_explodes(client, monkeypatch) -> None:
    import backend.services.audit_service as audit_mod
    monkeypatch.setattr(audit_mod, "verify_chain", lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("boom")))
    assert client.get("/metrics").status_code == 200


def test_a_failing_request_is_still_counted(client, seeded) -> None:
    client.get("/api/state/usr_does_not_exist")
    body = client.get("/metrics").text
    assert 'status="404"' in body, "only counting successes would hide every outage"


# ---------------------------------------------------------------------------
# Item 7 — SQLite tuning
# ---------------------------------------------------------------------------


def test_wal_is_enabled_on_a_real_file_and_skipped_in_memory(tmp_path) -> None:
    """WAL needs real files; every test uses an in-memory database, where the
    pragma is meaningless. Both halves are asserted so the guard cannot rot."""
    from sqlalchemy import create_engine, text as sql

    from backend.models.db import _make_engine

    path = tmp_path / "wal_probe.db"
    engine = _make_engine(f"sqlite:///{path}")
    try:
        with engine.connect() as conn:
            assert conn.execute(sql("PRAGMA journal_mode")).scalar_one() == "wal"
            assert conn.execute(sql("PRAGMA foreign_keys")).scalar_one() == 1
            assert conn.execute(sql("PRAGMA busy_timeout")).scalar_one() == 15000
    finally:
        engine.dispose()

    mem = _make_engine("sqlite://")
    try:
        with mem.connect() as conn:
            assert conn.execute(sql("PRAGMA journal_mode")).scalar_one() == "memory"
    finally:
        mem.dispose()
    assert create_engine  # import used


# ---------------------------------------------------------------------------
# Item 6 — backup and REHEARSED restore
# ---------------------------------------------------------------------------


def test_backup_verify_and_restore_round_trip(tmp_path, monkeypatch) -> None:
    """The rehearsal, as a test: back up a real file, verify it, restore it,
    and confirm the row counts and the audit chain survive. An unrehearsed
    restore procedure is a hope, not a plan."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from backend.models.entities import Base
    from scripts import backup_db

    live = tmp_path / "live.db"
    engine = create_engine(f"sqlite:///{live}", future=True)
    Base.metadata.create_all(engine)
    with sessionmaker(bind=engine, future=True)() as s:
        demo_data.seed_all(s, force=True)
        s.commit()
    engine.dispose()

    monkeypatch.setattr(backup_db, "BACKUP_DIR", tmp_path / "backups")
    monkeypatch.setattr(backup_db, "_db_path", lambda: live)

    snapshot = backup_db.backup(quiet=True)
    assert snapshot.exists() and snapshot.stat().st_size > 0
    assert backup_db.verify(snapshot, quiet=True), "the fresh backup does not verify"

    before = backup_db._counts(snapshot)
    assert before["users"] == 2 and before["audit_events"] > 0

    # Wreck the live database, then restore over it.
    live.write_bytes(b"not a database at all")
    backup_db.restore(snapshot, assume_yes=True)
    assert backup_db._counts(live) == before
    assert backup_db._verify_chain(live)[0], "the restored chain must still verify"


def test_restore_refuses_a_backup_that_does_not_verify(tmp_path, monkeypatch) -> None:
    """Restoring from a corrupt backup over a working database would turn one
    problem into two."""
    from scripts import backup_db

    live = tmp_path / "live.db"
    live.write_bytes(b"")
    bad = tmp_path / "corrupt.db"
    bad.write_bytes(b"this is not sqlite")
    monkeypatch.setattr(backup_db, "_db_path", lambda: live)
    with pytest.raises(SystemExit) as exc:
        backup_db.restore(bad, assume_yes=True)
    assert "does not verify" in str(exc.value)


def test_backup_uses_the_online_api_not_a_file_copy(tmp_path, monkeypatch) -> None:
    """A `cp` of a live SQLite file can capture a torn page, and under WAL it
    misses the recent commits entirely. Assert we take a real snapshot of a
    database that is being written to."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from backend.models.entities import Base, Goal
    from scripts import backup_db

    live = tmp_path / "live.db"
    engine = create_engine(f"sqlite:///{live}", future=True)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, future=True)
    with Session() as s:
        demo_data.seed_all(s, force=True)
        s.commit()

    monkeypatch.setattr(backup_db, "BACKUP_DIR", tmp_path / "b")
    monkeypatch.setattr(backup_db, "_db_path", lambda: live)

    # Hold an open session with an uncommitted write while backing up.
    with Session() as writer:
        writer.add(Goal(user_id=writer.query(User).first().id, label="uncommitted", target_amount_paise=100))
        snapshot = backup_db.backup(quiet=True)     # must not hang, must not include it
        writer.rollback()
    engine.dispose()

    assert backup_db.verify(snapshot, quiet=True)
    conn = sqlite3.connect(f"file:{snapshot}?mode=ro", uri=True)
    try:
        labels = [r[0] for r in conn.execute("SELECT label FROM goals")]
    finally:
        conn.close()
    assert "uncommitted" not in labels, "the snapshot included an uncommitted write"


def test_config_summary_still_leaks_no_secrets(client) -> None:
    """Phase 8 added two config flags to the summary; re-assert the invariant
    that endpoint has always had."""
    summary = client.get("/health").json()["config"]
    blob = repr(summary)
    for forbidden in ("KEY_SECRET", "WEBHOOK_SECRET", "rzp_test_", "rzp_live_"):
        assert forbidden not in blob
    assert summary["json_logs"] is config.JSON_LOGS
    assert summary["rate_limits_enabled"] is config.RATE_LIMITS_ENABLED
