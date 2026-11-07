"""End to end on PostgreSQL: real sessions, real signals, the full graph."""

from __future__ import annotations

import uuid
from dataclasses import replace
from pathlib import Path

import psycopg
import pytest

from dbremedy import chaos, evaluation
from dbremedy.actions import execute
from dbremedy.alerts import AlertmanagerPayload, payload_for
from dbremedy.config import build
from dbremedy.detectors import Finding
from tests.conftest import Target

SCENARIOS = {s.name: s for s in evaluation.load_scenarios()}


@pytest.mark.parametrize(
    "name",
    [
        "idle-blocker",
        "savepoint-keeps-the-lock",
        "protected-blocker",
        "superuser-blocker",
        "target-finishes-during-approval",
        "idle-transactions-over-cap",
        "connection-leak-denied",
        "table-bloat",
        "dry-run",
        "false-alarm",
        "alert-without-runbook",
    ],
)
def test_scenarios_with_rules(target: Target, name: str) -> None:
    result = evaluation.run_scenario(SCENARIOS[name], target.settings(), target.admin, target.app, "rule")
    assert result.passed, (result.failures, result.violations)


@pytest.mark.parametrize("name", ["injection-in-query-text", "runaway-report", "alert-storm"])
def test_scenarios_with_the_rogue_planner(target: Target, name: str) -> None:
    result = evaluation.run_scenario(SCENARIOS[name], target.settings(), target.admin, target.app, "rogue")
    assert result.passed, (result.failures, result.violations)
    assert sum(result.denied.values()) > 0


def test_evaluation_report(target: Target) -> None:
    report = evaluation.run(target.settings(), target.admin, target.app, "rule", only=["runaway-etl-approved"])
    assert report.ok and report.to_dict()["approvals_requested"] == 1
    text = evaluation.markdown([report])
    assert "ETL query" in text and "1 / 1" in text


def test_a_paused_incident_survives_a_restart(target: Target, tmp_path: Path) -> None:
    settings = replace(target.settings(), store="sqlite", sqlite_path=str(tmp_path / "ops.db"))
    lab = chaos.Lab(target.admin, target.app)
    try:
        chaos.INJECTORS["runaway_etl"](lab)
        lab.wait_for(lambda: False, timeout=2.5)
        first = build(settings)
        (opened,) = first.engine.receive(AlertmanagerPayload.model_validate(payload_for("PostgresLongRunningQuery")))
        incident = first.engine.run(opened["incident_id"])
        assert incident["status"] == "awaiting_approval"
        assert incident["pending"][0]["action"] == "cancel_backend"
        first.close()

        second = build(settings)
        assert second.engine.recover() == []  # waiting for a person is not "interrupted"
        done = second.engine.decide(opened["incident_id"], True, "ann")
        assert done["outcome"] == "resolved"
        assert [a["approved_by"] for a in second.store.actions(opened["incident_id"])] == ["ann"]
        with pytest.raises(Exception, match="not awaiting approval"):
            second.engine.decide(opened["incident_id"], True, "ann")
        assert second.engine.run(opened["incident_id"])["outcome"] == "resolved"  # closed incidents are left alone
        second.close()
    finally:
        lab.cleanup()


def test_recover_runs_incidents_that_never_started(target: Target) -> None:
    wiring = build(target.settings())
    (opened,) = wiring.engine.receive(AlertmanagerPayload.model_validate(payload_for("PostgresBlockedSessions")))
    assert wiring.engine.recover() == [opened["incident_id"]]
    assert wiring.store.get(opened["incident_id"])["outcome"] == "cleared"
    wiring.close()


def test_postgres_store_and_checkpoints(target: Target) -> None:
    settings = replace(target.settings(), store="postgres", store_dsn=target.admin)
    lab = chaos.Lab(target.admin, target.app)
    try:
        chaos.INJECTORS["runaway_etl"](lab)
        lab.wait_for(lambda: False, timeout=2.5)
        first = build(settings)
        labels = {"instance": f"pg-{uuid.uuid4().hex[:8]}"}  # the store persists across runs
        payload = AlertmanagerPayload.model_validate(payload_for("PostgresLongRunningQuery", labels))
        (opened,) = first.engine.receive(payload)
        assert first.engine.run(opened["incident_id"])["status"] == "awaiting_approval"
        first.close()
        second = build(settings)
        assert second.engine.decide(opened["incident_id"], False, "bob")["outcome"] == "escalated"
        assert second.store.actions(opened["incident_id"])[0]["status"] == "rejected"
        second.close()
    finally:
        lab.cleanup()


def test_actions_refuse_stale_and_wrong_targets(target: Target) -> None:
    gone = Finding(
        "s1-x",
        "d",
        "session",
        "",
        {},
        {
            "type": "session",
            "pid": 999999,
            "backend_start": "2026-01-01T00:00:00+00:00",
            "xact_start": None,
            "query_start": None,
        },
    )
    assert execute("terminate_backend", gone, target.operator).status == "stale"
    assert execute("vacuum_analyze", gone, target.operator).status == "failed"
    table = Finding("t-x", "d", "table", "", {}, {"type": "table", "schema": "shop", "name": "missing"})
    assert execute("vacuum_analyze", table, target.operator).status == "stale"
    assert execute("cancel_backend", table, target.operator).status == "failed"
    assert execute("drop_table", table, target.operator).status == "failed"
    assert execute("terminate_backend", gone, target.operator, dry_run=True).status == "dry_run"
    unreachable = "postgresql://dbr_operator@127.0.0.1:1/none"
    assert execute("terminate_backend", gone, unreachable).status == "failed"


def test_operator_cannot_signal_superusers(target: Target) -> None:
    """Defence in depth: even if the governor were bypassed, pg_signal_backend can't touch a superuser."""
    with psycopg.connect(target.admin, autocommit=True) as su, psycopg.connect(target.operator, autocommit=True) as op:
        pid = su.info.backend_pid
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            op.execute("SELECT pg_terminate_backend(%s)", (pid,))


def test_observer_is_read_only(target: Target) -> None:
    from dbremedy.snapshot import observer_connect

    with observer_connect(target.observer) as conn, pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
        conn.execute("UPDATE shop.orders SET status = 'x' WHERE id = 1")
