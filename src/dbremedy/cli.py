"""The ``dbremedy`` command line."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import replace
from pathlib import Path
from typing import Any

from dbremedy import __version__


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


def _settings(args: argparse.Namespace) -> Any:
    from dbremedy.config import Settings

    settings = Settings.from_env()
    if getattr(args, "fast", False):
        from dbremedy.resources import resource

        settings = replace(settings, overrides_path=resource("evals") / "overrides.yaml")
    return settings


def cmd_setup(args: argparse.Namespace) -> int:
    from dbremedy.bootstrap import Credentials, install

    creds = Credentials(args.observer_password, args.operator_password, args.app_password)
    for note in install(args.admin_dsn, creds, demo_schema=not args.no_demo):
        print(note)
    return 0


def cmd_diagnose(args: argparse.Namespace) -> int:
    from dbremedy.config import load_runbooks
    from dbremedy.detectors import DETECTORS
    from dbremedy.snapshot import take

    settings = _settings(args)
    snap = take(settings.observer_dsn)
    books = load_runbooks(settings)
    result = {
        book.id: [f.to_dict() for f in DETECTORS[book.detector.name](snap, dict(book.detector.params))]
        for book in books.values()
    }
    if args.json:
        print(json.dumps({"snapshot": snap.summary(), "findings": result}, indent=2, default=str))
        return 0
    summary = snap.summary()
    print(
        f"{summary['database']} on PostgreSQL {summary['server_version']}: {summary['client_sessions']} client "
        f"sessions of {summary['max_connections']}, {summary['blocked_sessions']} blocked"
    )
    for book_id, findings in result.items():
        print(f"\n{book_id}: {len(findings)} finding(s)")
        for f in findings:
            print(f"  - {f['summary']}")
    return 0


def cmd_runbooks(args: argparse.Namespace) -> int:
    from dbremedy.config import load_runbooks

    books = load_runbooks(_settings(args))
    for book in books.values():
        print(f"{book.id} {book.version}  [{book.severity}]  alerts: {', '.join(book.alerts)}")
        for step in book.steps:
            print(f"    {step.action:<18} {step.approval:<8} {step.name}")
    print(f"{len(books)} runbooks valid")
    return 0


def _lab(args: argparse.Namespace) -> Any:
    from dbremedy.chaos import Lab

    admin = args.admin_dsn or _env("DBR_ADMIN_DSN")
    app = args.app_dsn or _env("DBR_APP_DSN")
    if not admin or not app:
        raise SystemExit("set --admin-dsn/--app-dsn or DBR_ADMIN_DSN/DBR_APP_DSN")
    return Lab(admin, app)


def cmd_chaos(args: argparse.Namespace) -> int:
    from dbremedy.chaos import INJECTORS

    if args.action == "list":
        for name in INJECTORS:
            print(name)
        return 0
    if args.name not in INJECTORS:
        raise SystemExit(f"unknown injector {args.name!r}; see 'dbremedy chaos list'")
    lab = _lab(args)
    INJECTORS[args.name](lab)
    print(f"injected {args.name}: {len(lab.tracked)} session(s); holding for up to {args.hold:.0f}s", flush=True)
    try:
        started = time.monotonic()
        while time.monotonic() - started < args.hold:
            time.sleep(1)
            if args.until_resolved and time.monotonic() - started > 5 and lab.waiting_on_locks() == 0:
                print("no session is waiting on a lock any more", flush=True)
                break
    except KeyboardInterrupt:
        pass
    finally:
        lab.cleanup()
    return 0


def cmd_eval(args: argparse.Namespace) -> int:
    from dbremedy import evaluation
    from dbremedy.resources import resource

    settings = replace(_settings(args), overrides_path=resource("evals") / "overrides.yaml")
    admin = args.admin_dsn or _env("DBR_ADMIN_DSN")
    app = args.app_dsn or _env("DBR_APP_DSN")
    if not admin or not app:
        raise SystemExit("set --admin-dsn/--app-dsn or DBR_ADMIN_DSN/DBR_APP_DSN")
    reports = []
    for planner in args.planner.split(","):

        def progress(r: Any, planner: str = planner) -> None:
            mark = "pass" if r.passed else "FAIL"
            acted = ", ".join(f"{k} x{v}" for k, v in sorted(r.done.items())) or "no action"
            print(f"[{planner}] {mark:4} {r.scenario:34} {r.outcome:9} {acted} ({r.seconds}s)", flush=True)
            for problem in r.failures + r.violations:
                print(f"         {problem}", flush=True)

        report = evaluation.run(settings, admin, app, planner, args.scenario or None, progress)
        reports.append(report)
        if args.reports_dir:
            folder = Path(args.reports_dir) / planner
            folder.mkdir(parents=True, exist_ok=True)
            for result in report.results:
                (folder / f"{result.scenario}.md").write_text(result.report, encoding="utf-8")
    text = evaluation.markdown(reports)
    print("\n" + text)
    if args.report:
        Path(args.report).write_text(text, encoding="utf-8")
    if args.json:
        Path(args.json).write_text(json.dumps([r.to_dict() for r in reports], indent=2, default=str), encoding="utf-8")
    summary = _env("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as fh:
            fh.write(text)
    if args.check and not all(r.ok for r in reports):
        print("evaluation gate failed", file=sys.stderr)
        return 1
    return 0


DEMO = [
    "idle-blocker",
    "savepoint-keeps-the-lock",
    "injection-in-query-text",
    "protected-blocker",
    "connection-leak-approved",
]


def cmd_demo(args: argparse.Namespace) -> int:
    from dbremedy import evaluation
    from dbremedy.resources import resource

    settings = replace(_settings(args), overrides_path=resource("evals") / "overrides.yaml")
    admin = args.admin_dsn or _env("DBR_ADMIN_DSN")
    app = args.app_dsn or _env("DBR_APP_DSN")
    if not admin or not app:
        raise SystemExit("set --admin-dsn/--app-dsn or DBR_ADMIN_DSN/DBR_APP_DSN")
    scenarios = {s.name: s for s in evaluation.load_scenarios()}
    for name in DEMO:
        scenario = scenarios[name]
        print(f"\n### {scenario.title}", flush=True)
        result = evaluation.run_scenario(scenario, settings, admin, app, "rule")
        acted = ", ".join(f"{k} x{v}" for k, v in sorted(result.done.items())) or "nothing"
        print(f"  outcome: {result.outcome} in {result.seconds}s; did: {acted}")
        if result.denied:
            print(f"  refused: {', '.join(f'{k} x{v}' for k, v in result.denied.items())}")
        if result.survivors:
            print(f"  sessions that had to survive: {'all alive' if all(result.survivors.values()) else 'NOT ALL'}")
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn

    from dbremedy.api import create_app
    from dbremedy.config import build
    from dbremedy.observability import configure_logging

    settings = _settings(args)
    configure_logging(settings.log_level)
    wiring = build(settings)
    try:
        uvicorn.run(create_app(wiring, settings), host=args.host, port=args.port, log_level="warning")
    finally:
        wiring.close()
    return 0


def cmd_watch(args: argparse.Namespace) -> int:
    """Poll the detectors directly and open incidents, for setups without Prometheus."""
    from dbremedy.alerts import AlertmanagerPayload, payload_for
    from dbremedy.config import build
    from dbremedy.detectors import DETECTORS
    from dbremedy.observability import configure_logging, event
    from dbremedy.snapshot import take

    settings = _settings(args)
    configure_logging(settings.log_level)
    wiring = build(settings)
    engine = wiring.engine
    rounds = 0
    try:
        while args.rounds == 0 or rounds < args.rounds:
            rounds += 1
            snap = take(settings.observer_dsn)
            for book in engine.svc.runbooks.values():
                if DETECTORS[book.detector.name](snap, dict(book.detector.params)):
                    payload = AlertmanagerPayload.model_validate(
                        payload_for(book.alerts[0], {"instance": snap.database, "source": "watch"})
                    )
                    for r in engine.receive(payload):
                        if r["created"]:
                            incident = engine.run(r["incident_id"])
                            event("watch_incident", incident_id=r["incident_id"], status=incident["status"])
                            print(
                                f"{r['incident_id']} {book.id}: {incident.get('outcome') or incident['status']}",
                                flush=True,
                            )
            time.sleep(args.interval)
    except KeyboardInterrupt:
        pass
    finally:
        wiring.close()
    return 0


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="dbremedy", description=__doc__)
    p.add_argument("--version", action="version", version=__version__)
    p.add_argument("--fast", action="store_true", help="use the evaluation's second-scale thresholds")
    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser("setup", help="create the agent's roles (and the demo schema) on the target")
    s.add_argument("--admin-dsn", default=_env("DBR_ADMIN_DSN"), required=not _env("DBR_ADMIN_DSN"))
    s.add_argument("--observer-password", default=_env("DBR_OBSERVER_PASSWORD", "dbr-observer-local"))
    s.add_argument("--operator-password", default=_env("DBR_OPERATOR_PASSWORD", "dbr-operator-local"))
    s.add_argument("--app-password", default=_env("DBR_APP_PASSWORD", "dbr-app-local"))
    s.add_argument("--no-demo", action="store_true", help="roles only, no demo schema")
    s.set_defaults(fn=cmd_setup)

    s = sub.add_parser("diagnose", help="run every detector once and print the findings")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_diagnose)

    s = sub.add_parser("runbooks", help="validate and list the runbooks")
    s.set_defaults(fn=cmd_runbooks)

    s = sub.add_parser("chaos", help="inject a real incident into the target")
    s.add_argument("action", choices=["list", "inject"])
    s.add_argument("name", nargs="?", default="")
    s.add_argument("--admin-dsn", default="")
    s.add_argument("--app-dsn", default="")
    s.add_argument("--hold", type=float, default=300.0, help="seconds to keep the incident going")
    s.add_argument("--until-resolved", action="store_true", help="stop once no session waits on a lock")
    s.set_defaults(fn=cmd_chaos)

    s = sub.add_parser("eval", help="run the incident scenarios against PostgreSQL")
    s.add_argument("--admin-dsn", default="")
    s.add_argument("--app-dsn", default="")
    s.add_argument("--planner", default="rule,rogue", help="comma-separated: rule, rogue, llm")
    s.add_argument("--scenario", action="append", help="only this scenario (repeatable)")
    s.add_argument("--report", help="write the markdown report here")
    s.add_argument("--json", help="write the full results here")
    s.add_argument("--reports-dir", help="write each scenario's incident report here, per planner")
    s.add_argument("--check", action="store_true", help="exit non-zero unless every scenario passes")
    s.set_defaults(fn=cmd_eval)

    s = sub.add_parser("demo", help="walk through five incidents end to end")
    s.add_argument("--admin-dsn", default="")
    s.add_argument("--app-dsn", default="")
    s.set_defaults(fn=cmd_demo)

    s = sub.add_parser("serve", help="run the HTTP API")
    s.add_argument("--host", default="0.0.0.0")  # noqa: S104 - a container listens on all interfaces
    s.add_argument("--port", type=int, default=8080)
    s.set_defaults(fn=cmd_serve)

    s = sub.add_parser("watch", help="poll the detectors and handle incidents without Prometheus")
    s.add_argument("--interval", type=float, default=15.0)
    s.add_argument("--rounds", type=int, default=0, help="stop after this many rounds (0 = forever)")
    s.set_defaults(fn=cmd_watch)
    return p


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    return int(args.fn(args))


if __name__ == "__main__":
    raise SystemExit(main())
