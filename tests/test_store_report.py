from __future__ import annotations

import threading

import pytest

from dbremedy.alerts import AlertmanagerPayload, incident_key, payload_for, summary
from dbremedy.config import Settings, load_runbooks
from dbremedy.observability import Metrics, configure_logging, event
from dbremedy.report import build_report
from dbremedy.store import Store


def exercise(store: Store) -> None:
    a, created = store.open_or_attach("k1", "Alert", "lock-contention", "critical", {"instance": "db1"}, "summary")
    assert created and a["status"] == "open" and a["alerts"] == 1
    b, created = store.open_or_attach("k1", "Alert", "lock-contention", "critical", {"instance": "db1"}, "summary")
    assert not created and b["id"] == a["id"] and b["alerts"] == 2
    store.update(a["id"], status="awaiting_approval", pending=[{"x": 1}])
    assert store.get(a["id"])["pending"] == [{"x": 1}]
    with pytest.raises(ValueError, match="cannot update"):
        store.update(a["id"], key="nope")
    store.update(a["id"])
    record = {
        "id": "act1",
        "incident_id": a["id"],
        "finding_id": "f",
        "action": "terminate_backend",
        "target": {"pid": 1},
        "verdict": "allow",
        "status": "done",
        "reasons": ["r"],
    }
    assert store.record_action(record) and not store.record_action(record)
    assert store.action_recorded("act1")["target"] == {"pid": 1}  # type: ignore[index]
    assert store.action_recorded("nope") is None
    store.close_incident(a["id"], "resolved", "resolved", 1.5)
    c, created = store.open_or_attach("k1", "Alert", "lock-contention", "critical", {}, "again")
    assert created and c["id"] != a["id"]
    kinds = [e["kind"] for e in store.events(a["id"])]
    assert kinds == ["opened", "alert"]
    assert [i["id"] for i in store.incidents(status="resolved")] == [a["id"]]
    assert len(store.incidents()) == 2
    with pytest.raises(KeyError):
        store.get("missing")


def test_sqlite_store(tmp_path: object) -> None:
    exercise(Store.sqlite(":memory:"))
    exercise(Store.sqlite(f"{tmp_path}/s.db"))


def test_postgres_store(target: object) -> None:
    import psycopg

    dsn = target.admin  # type: ignore[attr-defined]
    with psycopg.connect(dsn, autocommit=True) as conn:
        for table in ("dbr_incidents", "dbr_events", "dbr_actions"):
            conn.execute(f"DROP TABLE IF EXISTS {table}")
    store = Store.postgres(dsn)
    try:
        exercise(store)
    finally:
        store.close()


def test_concurrent_deliveries_open_one_incident() -> None:
    store = Store.sqlite(":memory:")
    results: list[bool] = []

    def deliver() -> None:
        results.append(store.open_or_attach("same", "A", None, "warning", {}, "")[1])

    threads = [threading.Thread(target=deliver) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert results.count(True) == 1 and len(store.incidents()) == 1


def test_alert_payloads() -> None:
    payload = AlertmanagerPayload.model_validate(payload_for("PostgresBlockedSessions", {"instance": "db1"}, "s"))
    alert = payload.alerts[0]
    assert alert.name == "PostgresBlockedSessions" and summary(alert) == "s"
    assert (
        incident_key({"alertname": "A", "instance": "db1", "pod": "x", "datname": "shop"})
        == "A|instance=db1|datname=shop"
    )
    alert.annotations.clear()
    assert summary(alert) == "PostgresBlockedSessions"


def test_report_and_metrics() -> None:
    books = load_runbooks(Settings(observer_dsn="x", operator_dsn="y"))
    incident = {
        "id": "inc-1",
        "alertname": "PostgresBlockedSessions",
        "alerts": 2,
        "status": "escalated",
        "outcome": "escalated",
        "opened_at": "2026-11-07T12:00:00+00:00",
        "resolved_at": "x",
        "mttr_s": 3.25,
        "labels": {"instance": "db|1"},
    }
    events = [
        {
            "at": "2026-11-07T12:00:00.000000+00:00",
            "kind": "diagnosis",
            "message": "found",
            "data": {"findings": [{"summary": "pid 1 blocks", "evidence": {"session": {"query": "UPDATE t"}}}]},
        },
    ]
    actions = [
        {
            "action": "terminate_backend",
            "finding_id": "f",
            "target": {"type": "session", "pid": 1},
            "status": "done",
            "detail": "ok",
            "step": "s",
            "approved_by": None,
            "reasons": [],
        },
        {
            "action": "vacuum_analyze",
            "finding_id": "t",
            "target": {"type": "table", "schema": "shop", "name": "e"},
            "status": "failed",
            "detail": "x",
            "step": None,
            "approved_by": "ann",
            "reasons": [],
        },
        {
            "action": "drop_table",
            "finding_id": "f",
            "target": None,
            "status": "denied",
            "detail": "",
            "reasons": ["unknown_action: no"],
        },
    ]
    text = build_report(incident, events, actions, books["lock-contention"])
    assert "2 deliveries" in text and "db\\|1" in text and "pid 1" in text and "shop.e" in text
    assert "unknown_action: 1" in text and "Escalated to **dba-oncall**" in text and "3.2s" in text
    many_findings = [{"summary": f"pid {i}", "attributes": {"application": "leaky"}} for i in range(20)]
    many_actions = [{**actions[0], "target": {"type": "session", "pid": i}} for i in range(20)]
    long = build_report(incident, [{**events[0], "data": {"findings": many_findings}}], many_actions, None)
    assert "... and 12 more (leaky x12)" in long and "... 12 more | | | | terminate_backend done x12" in long
    empty = build_report({**incident, "alerts": 1, "mttr_s": None}, [{**events[0], "data": {"findings": []}}], [], None)
    assert "1 delivery" in empty and "No action was taken" in empty and "Nothing" in empty

    metrics = Metrics()
    configure_logging("WARNING")
    event("ignored")
    for kind, data in [
        ("alert", {"alertname": "A", "created": True}),
        ("alert", {"alertname": "A", "created": False}),
        ("action", {"action": "cancel_backend", "status": "done"}),
        ("denied", {"code": "protected", "action": "terminate_backend"}),
        ("approval", {"approved": True}),
        ("approval", {"approved": False}),
        ("closed", {"runbook": "r", "outcome": "resolved", "mttr_s": 7.0}),
        ("closed", {"runbook": "none", "outcome": "escalated", "mttr_s": None}),
    ]:
        metrics.on_event(kind, data)
    rendered = metrics.render()
    assert 'dbremedy_alerts_total{alertname="A",result="attached"} 1' in rendered
    assert 'dbremedy_time_to_close_seconds_bucket{le="10"} 1' in rendered
    assert "dbremedy_time_to_close_seconds_count 1" in rendered
    assert 'dbremedy_approvals_total{decision="denied"} 1' in rendered
