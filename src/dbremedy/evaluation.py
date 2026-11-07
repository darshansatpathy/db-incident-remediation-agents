"""Run the scenarios against a real PostgreSQL and check what happened.

For each scenario: reset the demo data, inject the incident with real sessions, deliver the
alert the way Alertmanager would, run the engine, answer approvals as the scenario says, then
check the outcome, the actions in the ledger, the sessions that had to survive, the demo
data, and the independent audit. Every scenario gets a fresh engine and store.
"""

from __future__ import annotations

import statistics
import time
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict

from dbremedy import chaos
from dbremedy.alerts import AlertmanagerPayload, payload_for
from dbremedy.auditor import audit, load_raw
from dbremedy.config import Settings, build
from dbremedy.observability import Metrics
from dbremedy.planners import build as build_planner
from dbremedy.resources import resource


class Expect(BaseModel):
    model_config = ConfigDict(extra="forbid")

    outcome: str
    done: dict[str, int] | None = None
    done_range: dict[str, tuple[int, int]] | None = None
    stale: dict[str, int] | None = None
    dry_run: dict[str, int] | None = None
    approvals_requested: int | None = None
    counters: dict[str, int] = {}
    incidents: int | None = None


class Scenario(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    title: str
    inject: str
    alert: str
    wait_s: float = 2.5
    deliveries: int = 1
    approvals: list[bool] = []
    approve_after: str | None = None
    freeze: bool = False
    dry_run: bool = False
    expect: Expect


def load_scenarios(path: Path | None = None) -> list[Scenario]:
    data = yaml.safe_load((path or resource("evals") / "scenarios.yaml").read_text(encoding="utf-8"))
    scenarios = [Scenario.model_validate(s) for s in data["scenarios"]]
    unknown = [s.inject for s in scenarios if s.inject not in chaos.INJECTORS]
    if unknown:
        raise ValueError(f"unknown injectors: {unknown}")
    return scenarios


@dataclass
class ScenarioResult:
    scenario: str
    title: str
    planner: str
    passed: bool
    outcome: str
    failures: list[str]
    violations: list[str]
    done: dict[str, int]
    denied: dict[str, int]
    approvals_requested: int
    seconds: float
    incident_id: str
    survivors: dict[str, bool] = field(default_factory=dict)
    report: str = ""


@dataclass
class Report:
    planner: str
    results: list[ScenarioResult]

    @property
    def passed(self) -> int:
        return sum(r.passed for r in self.results)

    @property
    def violations(self) -> int:
        return sum(len(r.violations) for r in self.results)

    @property
    def ok(self) -> bool:
        return self.passed == len(self.results) and self.violations == 0

    def denied(self) -> Counter[str]:
        total: Counter[str] = Counter()
        for r in self.results:
            total.update(r.denied)
        return total

    def to_dict(self) -> dict[str, Any]:
        resolved = [r.seconds for r in self.results if r.outcome == "resolved"]
        return {
            "planner": self.planner,
            "scenarios": len(self.results),
            "passed": self.passed,
            "violations": self.violations,
            "actions_done": sum(sum(r.done.values()) for r in self.results),
            "denied": dict(self.denied()),
            "approvals_requested": sum(r.approvals_requested for r in self.results),
            "time_to_resolve_s": {
                "mean": round(statistics.mean(resolved), 1) if resolved else None,
                "max": round(max(resolved), 1) if resolved else None,
            },
            "results": [{k: v for k, v in r.__dict__.items() if k != "report"} for r in self.results],
        }


def run_scenario(
    scenario: Scenario, settings: Settings, admin_dsn: str, app_dsn: str, planner_name: str
) -> ScenarioResult:
    chaos.reset_demo_data(admin_dsn)
    lab = chaos.Lab(admin_dsn, app_dsn)
    settings = replace(settings, change_freeze=scenario.freeze, dry_run=scenario.dry_run, store="memory")
    wiring = build(settings, planner=build_planner(planner_name), metrics=Metrics())
    engine = wiring.engine
    failures: list[str] = []
    try:
        chaos.INJECTORS[scenario.inject](lab)
        time.sleep(scenario.wait_s)
        payload = AlertmanagerPayload.model_validate(
            payload_for(scenario.alert, {"instance": "eval", "datname": "target"})
        )
        opened = [engine.receive(payload) for _ in range(scenario.deliveries)]
        incident_ids = sorted({r["incident_id"] for batch in opened for r in batch})
        incident_id = incident_ids[0]
        alert_at = time.monotonic()
        incident = engine.run(incident_id)
        approvals = list(scenario.approvals)
        requested = 0
        while incident["status"] == "awaiting_approval":
            requested += 1
            if scenario.approve_after:
                counter = scenario.approve_after

                def reached(counter: str = counter) -> bool:
                    return lab.counters.get(counter, 0) > 0

                lab.wait_for(reached, timeout=20)
            decision = approvals.pop(0) if approvals else False
            incident = engine.decide(incident_id, decision, "eval-approver")
        seconds = time.monotonic() - alert_at
        survivors = lab.survivors()
        data_ok = chaos.demo_data_intact(admin_dsn)
        ledger = wiring.store.actions(incident_id)
        apps = {t.pid: t.application for t in lab.tracked}
        must_survive = lab.protected_pids()
    finally:
        lab.cleanup()
        wiring.close()

    def tally(status: str) -> dict[str, int]:
        return dict(Counter(r["action"] for r in ledger if r["status"] == status))

    done, stale, dry = tally("done"), tally("stale"), tally("dry_run")
    denied = Counter(
        (r["reasons"][0].split(":", 1)[0] if r["reasons"] else "denied") for r in ledger if r["status"] == "denied"
    )
    e = scenario.expect
    outcome = incident.get("outcome") or incident["status"]
    if outcome != e.outcome:
        failures.append(f"outcome {outcome}, expected {e.outcome}")
    if e.done is not None and done != e.done:
        failures.append(f"done {done}, expected {e.done}")
    for action, (low, high) in (e.done_range or {}).items():
        if not low <= done.get(action, 0) <= high:
            failures.append(f"{action} done {done.get(action, 0)} times, expected {low} to {high}")
    if e.stale is not None and stale != e.stale:
        failures.append(f"stale {stale}, expected {e.stale}")
    if e.dry_run is not None and dry != e.dry_run:
        failures.append(f"dry_run {dry}, expected {e.dry_run}")
    if e.approvals_requested is not None and requested != e.approvals_requested:
        failures.append(f"{requested} approval request(s), expected {e.approvals_requested}")
    if e.incidents is not None and len(incident_ids) != e.incidents:
        failures.append(f"{len(incident_ids)} incidents opened, expected {e.incidents}")
    for counter, minimum in e.counters.items():
        if lab.counters.get(counter, 0) < minimum:
            failures.append(f"{counter} = {lab.counters.get(counter, 0)}, expected at least {minimum}")
    dead = [k for k, alive in survivors.items() if not alive]
    if dead:
        failures.append(f"sessions that had to survive were ended: {dead}")
    if not data_ok:
        failures.append("demo data or server settings were changed")
    if lab.errors:
        failures.append(f"injector errors: {lab.errors[:3]}")

    raw_books, raw_policy = load_raw(settings.runbooks_dir, settings.policy_path)
    runbook_id = incident.get("runbook")
    violations = audit(
        ledger,
        raw_books.get(runbook_id) if runbook_id else None,
        raw_policy,
        must_survive=must_survive,
        protected_applications=apps,
        freeze=scenario.freeze,
    )
    return ScenarioResult(
        scenario=scenario.name,
        title=scenario.title,
        planner=planner_name,
        passed=not failures and not violations,
        outcome=outcome,
        failures=failures,
        violations=violations,
        done=done,
        denied=dict(denied),
        approvals_requested=requested,
        seconds=round(seconds, 1),
        incident_id=incident_id,
        survivors=survivors,
        report=str(incident.get("report") or ""),
    )


def run(
    settings: Settings,
    admin_dsn: str,
    app_dsn: str,
    planner: str = "rule",
    only: list[str] | None = None,
    progress: Any = None,
) -> Report:
    results = []
    for scenario in load_scenarios():
        if only and scenario.name not in only:
            continue
        result = run_scenario(scenario, settings, admin_dsn, app_dsn, planner)
        if progress:
            progress(result)
        results.append(result)
    return Report(planner, results)


def markdown(reports: list[Report]) -> str:
    lines = ["## Incident scenarios on PostgreSQL", ""]
    header = "| Scenario | " + " | ".join(f"{r.planner} planner" for r in reports) + " |"
    lines += [header, "|---|" + "---|" * len(reports)]
    by_name = {r.planner: {x.scenario: x for x in r.results} for r in reports}
    for result in reports[0].results:
        cells = []
        for report in reports:
            x = by_name[report.planner].get(result.scenario)
            if x is None:
                cells.append("")
                continue
            mark = "pass" if x.passed else "**FAIL**"
            acted = ", ".join(f"{k} x{v}" for k, v in sorted(x.done.items())) or "no action"
            cells.append(f"{mark}: {x.outcome}, {acted}, {x.seconds}s")
        lines.append(f"| {result.title} | " + " | ".join(cells) + " |")
    lines.append("")
    lines += ["| | " + " | ".join(r.planner for r in reports) + " |", "|---|" + "---|" * len(reports)]
    summaries = [r.to_dict() for r in reports]
    rows: list[tuple[str, Callable[[dict[str, Any]], str]]] = [
        ("Scenarios passed", lambda s: f"{s['passed']} / {s['scenarios']}"),
        ("Audit violations", lambda s: str(s["violations"])),
        ("Actions executed", lambda s: str(s["actions_done"])),
        ("Proposals refused by the governor", lambda s: str(sum(s["denied"].values()))),
        ("Approvals requested", lambda s: str(s["approvals_requested"])),
        ("Mean time to resolve (s)", lambda s: str(s["time_to_resolve_s"]["mean"])),
    ]
    for label, fn in rows:
        lines.append(f"| {label} | " + " | ".join(fn(s) for s in summaries) + " |")
    for report in reports:
        for x in report.results:
            for problem in x.failures + x.violations:
                lines.append(f"\n- {report.planner} / {x.scenario}: {problem}")
    return "\n".join(lines) + "\n"
