from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from dbremedy import chaos
from dbremedy.alerts import payload_for
from dbremedy.api import create_app
from dbremedy.cli import main
from dbremedy.config import build
from tests.conftest import Target

APPROVER = {"X-User-Id": "ann", "X-User-Roles": "approver"}
VIEWER = {"X-User-Id": "vic", "X-User-Roles": "viewer"}


def wait_status(client: TestClient, incident_id: str, *statuses: str, timeout: float = 30) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        data = client.get(f"/v1/incidents/{incident_id}", headers=VIEWER).json()
        if data["status"] in statuses:
            return data  # type: ignore[no-any-return]
        time.sleep(0.2)
    raise AssertionError(f"{incident_id} never reached {statuses}")


def test_webhook_approval_and_report(target: Target) -> None:
    from dataclasses import replace

    settings = replace(target.settings(), webhook_token="hook-secret")
    wiring = build(settings)
    lab = chaos.Lab(target.admin, target.app)
    try:
        with TestClient(create_app(wiring, settings)) as client:
            assert client.get("/healthz").json() == {"status": "ok"}
            assert client.get("/readyz").json()["status"] == "ready"
            body = payload_for("PostgresLongRunningQuery", {"instance": "db1"})
            assert client.post("/v1/alerts/alertmanager", json=body).status_code == 401
            chaos.INJECTORS["runaway_etl"](lab)
            time.sleep(2.5)
            hook = {"Authorization": "Bearer hook-secret"}
            response = client.post("/v1/alerts/alertmanager", json=body, headers=hook)
            assert response.status_code == 202
            incident_id = response.json()["incidents"][0]["incident_id"]
            again = client.post("/v1/alerts/alertmanager", json=body, headers=hook).json()
            assert again["incidents"][0] == {"incident_id": incident_id, "created": False, "runbook": "runaway-query"}

            paused = wait_status(client, incident_id, "awaiting_approval")
            assert paused["pending"][0]["action"] == "cancel_backend"
            assert client.get(f"/v1/incidents/{incident_id}/report", headers=VIEWER).status_code == 409
            decision = {"approved": True, "comment": "go"}
            assert (
                client.post(f"/v1/incidents/{incident_id}/approval", json=decision, headers=VIEWER).status_code == 403
            )
            assert (
                client.post(f"/v1/incidents/{incident_id}/approval", json=decision, headers=APPROVER).status_code == 202
            )
            done = wait_status(client, incident_id, "resolved", "escalated")
            assert done["outcome"] == "resolved"
            assert (
                client.post(f"/v1/incidents/{incident_id}/approval", json=decision, headers=APPROVER).status_code == 409
            )
            report = client.get(f"/v1/incidents/{incident_id}/report", headers=VIEWER).text
            assert "cancel_backend" in report and "ann" in report

            resolved = {**body, "status": "resolved", "alerts": [{**body["alerts"][0], "status": "resolved"}]}
            assert client.post("/v1/alerts/alertmanager", json=resolved, headers=hook).json() == {"incidents": []}
            assert client.get("/v1/incidents", headers=VIEWER).json()["incidents"][0]["id"] == incident_id
            assert client.get("/v1/incidents?status=resolved", headers=VIEWER).json()["incidents"]
            assert client.get("/v1/incidents/nope", headers=VIEWER).status_code == 404
            assert client.get("/v1/incidents/nope/report", headers=VIEWER).status_code == 404
            assert client.post("/v1/incidents/nope/approval", json=decision, headers=APPROVER).status_code == 404
            assert client.get("/v1/incidents").status_code == 403
            assert client.get("/v1/incidents", headers={"X-User-Roles": "root"}).status_code == 400
            assert len(client.get("/v1/runbooks", headers=VIEWER).json()["runbooks"]) == 5
            assert set(client.get("/v1/diagnose", headers=VIEWER).json()["findings"]) >= {"lock-contention"}
            metrics = client.get("/metrics").text
            assert 'dbremedy_actions_total{action="cancel_backend",status="done"} 1' in metrics
    finally:
        lab.cleanup()
        wiring.close()


def test_manual_incidents_and_service_token(target: Target) -> None:
    from dataclasses import replace

    settings = replace(target.settings(), service_token="svc")
    wiring = build(settings)
    try:
        with TestClient(create_app(wiring, settings)) as client:
            operator = {"X-User-Id": "ops", "X-User-Roles": "operator", "X-Service-Token": "svc"}
            body = {"alertname": "PostgresIdleInTransaction", "labels": {"instance": "db1"}}
            assert (
                client.post("/v1/incidents", json=body, headers={**operator, "X-Service-Token": "x"}).status_code == 401
            )
            assert (
                client.post("/v1/incidents", json=body, headers={**VIEWER, "X-Service-Token": "svc"}).status_code == 403
            )
            response = client.post("/v1/incidents", json=body, headers=operator)
            assert response.status_code == 202
            incident_id = response.json()["incidents"][0]["incident_id"]
            viewer = {**VIEWER, "X-Service-Token": "svc"}
            deadline = time.monotonic() + 20
            while client.get(f"/v1/incidents/{incident_id}", headers=viewer).json()["status"] not in ("resolved",):
                assert time.monotonic() < deadline
                time.sleep(0.2)
            detail = client.get(f"/v1/incidents/{incident_id}", headers=viewer).json()
            assert detail["outcome"] == "cleared" and detail["labels"]["opened_by"] == "ops"
    finally:
        wiring.close()


def test_readyz_reports_an_unreachable_target(target: Target) -> None:
    from dataclasses import replace

    settings = replace(target.settings(), observer_dsn="postgresql://nobody@127.0.0.1:1/none")
    wiring = build(settings)
    try:
        with TestClient(create_app(wiring, settings)) as client:
            assert client.get("/readyz").status_code == 503
    finally:
        wiring.close()


def env(monkeypatch: pytest.MonkeyPatch, target: Target) -> None:
    monkeypatch.setenv("DBR_TARGET_OBSERVER_DSN", target.observer)
    monkeypatch.setenv("DBR_TARGET_OPERATOR_DSN", target.operator)
    monkeypatch.setenv("DBR_ADMIN_DSN", target.admin)
    monkeypatch.setenv("DBR_APP_DSN", target.app)


def test_cli(
    target: Target, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    env(monkeypatch, target)
    assert main(["runbooks"]) == 0
    assert "5 runbooks valid" in capsys.readouterr().out
    assert main(["chaos", "list"]) == 0
    assert "idle_blocker" in capsys.readouterr().out
    assert (
        main(
            [
                "setup",
                "--admin-dsn",
                target.admin,
                "--observer-password",
                "test-observer",
                "--operator-password",
                "test-operator",
                "--app-password",
                "test-app",
            ]
        )
        == 0
    )
    assert "operator:" in capsys.readouterr().out
    assert main(["--fast", "diagnose"]) == 0
    assert "client sessions" in capsys.readouterr().out
    assert main(["diagnose", "--json"]) == 0
    assert "findings" in json.loads(capsys.readouterr().out)
    summary = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    code = main(
        [
            "eval",
            "--planner",
            "rule",
            "--scenario",
            "runaway-report",
            "--check",
            "--report",
            str(tmp_path / "r.md"),
            "--json",
            str(tmp_path / "r.json"),
            "--reports-dir",
            str(tmp_path / "reports"),
        ]
    )
    assert code == 0
    assert "pass" in capsys.readouterr().out
    assert summary.read_text() == (tmp_path / "r.md").read_text()
    assert json.loads((tmp_path / "r.json").read_text())[0]["passed"] == 1
    assert "cancel_backend" in (tmp_path / "reports" / "rule" / "runaway-report.md").read_text()
    assert main(["--fast", "watch", "--rounds", "1", "--interval", "0"]) == 0
    assert main(["chaos", "inject", "idle_blocker", "--hold", "6", "--until-resolved"]) == 0
    assert "injected idle_blocker" in capsys.readouterr().out
    with pytest.raises(SystemExit):
        main(["chaos", "inject", "nope"])


def test_cli_watch_resolves_what_it_finds(
    target: Target, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    env(monkeypatch, target)
    lab = chaos.Lab(target.admin, target.app)
    try:
        chaos.INJECTORS["idle_transactions_4"](lab)
        time.sleep(2.5)
        assert main(["--fast", "watch", "--rounds", "1", "--interval", "0"]) == 0
        assert "idle-in-transaction: resolved" in capsys.readouterr().out
    finally:
        lab.cleanup()


def test_cli_demo(target: Target, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    env(monkeypatch, target)
    monkeypatch.setattr("dbremedy.cli.DEMO", ["idle-blocker", "protected-blocker"])
    assert main(["demo"]) == 0
    out = capsys.readouterr().out
    assert "outcome: resolved" in out and "outcome: escalated" in out and "all alive" in out


def test_cli_needs_dsns(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("DBR_ADMIN_DSN", "DBR_APP_DSN"):
        monkeypatch.delenv(name, raising=False)
    for argv in (["eval"], ["demo"], ["chaos", "inject", "idle_blocker"]):
        with pytest.raises(SystemExit):
            main(argv)
