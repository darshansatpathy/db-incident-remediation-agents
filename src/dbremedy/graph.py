"""The incident workflow as a LangGraph state graph.

    diagnose ─▶ plan ─┬─▶ approve ─┬─▶ execute ─▶ verify ─┬─▶ report   (resolved)
       ▲              │            └─▶ escalate           ├─▶ diagnose (next attempt)
       │              ├─▶ execute                         └─▶ escalate
       │              └─▶ escalate ─▶ report
       └─ no findings at the start ─▶ report (cleared)

The action ledger in the store is the source of truth for what has been done. Every node
that writes checks it first, so a node that LangGraph re-runs (after an approval, or a
restart) never signals a session twice.
"""

from __future__ import annotations

import hashlib
import time
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from dbremedy import actions as catalog
from dbremedy.detectors import DETECTORS, Finding
from dbremedy.governor import Context, review
from dbremedy.planners import Planner
from dbremedy.planners.llm import PlannerError
from dbremedy.policy import Policy
from dbremedy.report import build_report
from dbremedy.runbooks import Runbook
from dbremedy.snapshot import Snapshot, take
from dbremedy.store import Store


class IncidentState(TypedDict, total=False):
    incident_id: str
    runbook: str
    attempt: int
    findings: list[dict[str, Any]]
    allowed: list[dict[str, Any]]
    pending: list[dict[str, Any]]
    verified: bool
    outcome: str
    reason: str


@dataclass
class Services:
    observer_dsn: str
    operator_dsn: str
    runbooks: dict[str, Runbook]
    policy: Policy
    planner: Planner
    store: Store
    freeze: bool = False
    sleep: Callable[[float], None] = time.sleep
    on_event: Callable[[str, dict[str, Any]], None] = lambda kind, data: None
    snapshots: dict[str, Snapshot] = field(default_factory=dict)

    def snapshot(self, incident_id: str) -> Snapshot:
        snap = take(self.observer_dsn)
        self.snapshots[incident_id] = snap
        return snap


def finding_from(data: dict[str, Any]) -> Finding:
    return Finding(**data)


@dataclass(frozen=True)
class LedgerView:
    history: set[tuple[str, str]]  # (session_key, action) applied
    executed: set[tuple[str, str]]  # (finding_id, action) attempted
    counts: Counter[str]  # applied actions by name
    unapproved_terminations: int


def _ledger_view(store: Store, incident_id: str) -> LedgerView:
    history: set[tuple[str, str]] = set()
    executed: set[tuple[str, str]] = set()
    counts: Counter[str] = Counter()
    unapproved = 0
    for row in store.actions(incident_id):
        if row["status"] in ("done", "stale", "dry_run", "failed"):
            executed.add((row["finding_id"], row["action"]))
            if row["action"] == "terminate_backend" and not row["approved_by"]:
                unapproved += 1
        if row["status"] in ("done", "dry_run"):
            counts[row["action"]] += 1
            if row["session_key"]:
                history.add((row["session_key"], row["action"]))
    return LedgerView(history, executed, counts, unapproved)


def _action_id(incident_id: str, *parts: object) -> str:
    raw = "|".join([incident_id, *map(str, parts)])
    return hashlib.sha256(raw.encode()).hexdigest()[:24]


def build_graph(svc: Services, checkpointer: Any) -> Any:
    def runbook_of(state: IncidentState) -> Runbook:
        return svc.runbooks[state["runbook"]]

    def diagnose(state: IncidentState) -> dict[str, Any]:
        incident_id, book = state["incident_id"], runbook_of(state)
        attempt = state.get("attempt", 0)
        svc.store.update(incident_id, status="running")
        snap = svc.snapshot(incident_id)
        findings = DETECTORS[book.detector.name](snap, dict(book.detector.params))
        svc.store.event(
            incident_id,
            "diagnosis",
            f"attempt {attempt + 1}: {book.detector.name} found {len(findings)} finding(s)",
            {"attempt": attempt, "snapshot": snap.summary(), "findings": [f.to_dict() for f in findings]},
        )
        return {"findings": [f.to_dict() for f in findings], "attempt": attempt}

    def after_diagnose(state: IncidentState) -> str:
        if state["findings"]:
            return "plan"
        return "report"

    def plan(state: IncidentState) -> dict[str, Any]:
        incident_id, book = state["incident_id"], runbook_of(state)
        findings = [finding_from(f) for f in state["findings"]]
        ledger = _ledger_view(svc.store, incident_id)
        try:
            proposals = svc.planner.propose(book, findings, ledger.history, svc.snapshots.get(incident_id))
        except PlannerError as exc:
            svc.store.event(incident_id, "planner_error", str(exc), {})
            return {"allowed": [], "pending": [], "reason": f"planner failed: {exc}"}
        decisions = review(
            proposals,
            Context(
                book,
                svc.policy,
                {f.id: f for f in findings},
                ledger.history,
                ledger.executed,
                ledger.counts,
                datetime.now(UTC),
                svc.freeze,
                ledger.unapproved_terminations,
            ),
        )
        attempt = state.get("attempt", 0)
        for index, d in enumerate(decisions):
            if d.verdict == "deny":
                svc.store.record_action(
                    {
                        "id": _action_id(incident_id, "deny", attempt, index, d.proposal.finding_id, d.proposal.action),
                        "incident_id": incident_id,
                        "finding_id": d.proposal.finding_id,
                        "action": d.proposal.action,
                        "session_key": d.finding.attributes.get("session_key") if d.finding else None,
                        "target": d.finding.target if d.finding else None,
                        "verdict": "deny",
                        "reasons": d.reasons,
                        "status": "denied",
                        "detail": d.proposal.reason,
                    }
                )
                svc.on_event("denied", {"code": d.code, "action": d.proposal.action})
        svc.store.event(
            incident_id,
            "plan",
            f"{svc.planner.name} planner proposed {len(proposals)} action(s): "
            f"{sum(d.verdict == 'allow' for d in decisions)} allowed, "
            f"{sum(d.verdict == 'approval' for d in decisions)} need approval, "
            f"{sum(d.verdict == 'deny' for d in decisions)} denied",
            {"decisions": [d.to_dict() for d in decisions]},
        )
        allowed = [_item(d.to_dict(), d.finding) for d in decisions if d.verdict == "allow"]
        pending = [_item(d.to_dict(), d.finding) for d in decisions if d.verdict == "approval"]
        reason = ""
        if not allowed and not pending:
            denied = Counter(d.code for d in decisions)
            reason = (
                "no action is allowed: " + ", ".join(f"{k} x{v}" for k, v in denied.items())
                if decisions
                else f"no runbook step applies to {len(findings)} finding(s)"
            )
        return {"allowed": allowed, "pending": pending, "reason": reason}

    def after_plan(state: IncidentState) -> str:
        if state.get("pending"):
            return "approve"
        if state.get("allowed"):
            return "execute"
        return "escalate"

    def approve(state: IncidentState) -> dict[str, Any]:
        incident_id = state["incident_id"]
        pending = state["pending"]
        summary = [
            {
                "finding_id": p["finding_id"],
                "action": p["action"],
                "step": p["step"],
                "reasons": p["reasons"],
                "target": p["target"],
                "summary": p["finding"]["summary"],
            }
            for p in pending
        ]
        svc.store.update(incident_id, status="awaiting_approval", pending=summary)
        answer = interrupt({"incident_id": incident_id, "actions": summary})
        approved = bool(answer.get("approved")) if isinstance(answer, dict) else False
        approver = str(answer.get("approver", "unknown")) if isinstance(answer, dict) else "unknown"
        svc.store.update(incident_id, status="running", pending=None)
        svc.store.event(
            incident_id,
            "approval",
            f"{approver} {'approved' if approved else 'denied'} {len(pending)} action(s)",
            {"approved": approved, "approver": approver, "actions": summary},
        )
        svc.on_event("approval", {"approved": approved})
        if approved:
            return {
                "allowed": state.get("allowed", []) + [{**p, "approved_by": approver} for p in pending],
                "pending": [],
            }
        for p in pending:
            svc.store.record_action(
                {
                    "id": _action_id(incident_id, "rejected", state.get("attempt", 0), p["finding_id"], p["action"]),
                    "incident_id": incident_id,
                    "finding_id": p["finding_id"],
                    "action": p["action"],
                    "session_key": p["finding"]["attributes"].get("session_key"),
                    "target": p["target"],
                    "verdict": "approval",
                    "reasons": p["reasons"],
                    "step": p["step"],
                    "approved_by": approver,
                    "status": "rejected",
                    "detail": "approval denied",
                }
            )
        reason = "" if state.get("allowed") else f"approval denied by {approver}"
        return {"pending": [], "reason": reason}

    def after_approve(state: IncidentState) -> str:
        return "execute" if state.get("allowed") else "escalate"

    def execute(state: IncidentState) -> dict[str, Any]:
        incident_id = state["incident_id"]
        for item in state.get("allowed", []):
            action_id = _action_id(incident_id, "exec", item["finding_id"], item["action"])
            if svc.store.action_recorded(action_id) is not None:
                continue  # already done before a re-run of this node
            finding = finding_from(item["finding"])
            result = catalog.execute(item["action"], finding, svc.operator_dsn, dry_run=svc.policy.dry_run)
            svc.store.record_action(
                {
                    "id": action_id,
                    "incident_id": incident_id,
                    "finding_id": item["finding_id"],
                    "action": item["action"],
                    "session_key": finding.attributes.get("session_key"),
                    "target": finding.target,
                    "verdict": item["verdict"],
                    "reasons": item["reasons"],
                    "step": item["step"],
                    "approved_by": item.get("approved_by"),
                    "status": result.status,
                    "detail": result.detail,
                }
            )
            svc.store.event(
                incident_id,
                "action",
                f"{item['action']} on {item['finding_id']}: {result.status} ({result.detail})",
                {"finding_id": item["finding_id"], "action": item["action"], **result.to_dict()},
            )
            svc.on_event("action", {"action": item["action"], "status": result.status})
        return {"allowed": []}

    def verify(state: IncidentState) -> dict[str, Any]:
        incident_id, book = state["incident_id"], runbook_of(state)
        ledger = _ledger_view(svc.store, incident_id)
        svc.sleep(book.verify.settle_s)
        # In dry-run nothing changed, so there is nothing to wait for: look once.
        deadline = time.monotonic() + (0.0 if svc.policy.dry_run else book.verify.timeout_s)
        findings: list[Finding] = []
        quiet_since: float | None = None
        while True:
            snap = svc.snapshot(incident_id)
            findings = DETECTORS[book.detector.name](snap, dict(book.detector.params))
            if not findings:
                quiet_since = time.monotonic() if quiet_since is None else quiet_since
                if time.monotonic() - quiet_since >= book.verify.stable_s:
                    stable = f" for {book.verify.stable_s:.0f}s" if book.verify.stable_s else ""
                    svc.store.event(incident_id, "verify", f"verified: the detector finds nothing{stable}", {})
                    return {"verified": True, "findings": []}
                svc.sleep(min(book.verify.interval_s, book.verify.stable_s))
                continue
            quiet_since = None
            if any(_new_step_applies(book, f, ledger.history, ledger.executed) for f in findings):
                svc.store.event(
                    incident_id,
                    "verify",
                    "the situation changed and another runbook step now applies",
                    {"findings": [f.to_dict() for f in findings]},
                )
                break
            if time.monotonic() >= deadline:
                svc.store.event(
                    incident_id,
                    "verify",
                    f"{len(findings)} finding(s) remain after {book.verify.timeout_s:.0f}s",
                    {"findings": [f.to_dict() for f in findings]},
                )
                break
            svc.sleep(book.verify.interval_s)
        return {"verified": False, "findings": [f.to_dict() for f in findings]}

    def after_verify(state: IncidentState) -> str:
        if state.get("verified"):
            return "report"
        if state.get("attempt", 0) + 1 < runbook_of(state).limits.max_attempts:
            return "next_attempt"
        return "escalate"

    def next_attempt(state: IncidentState) -> dict[str, Any]:
        return {"attempt": state.get("attempt", 0) + 1}

    def escalate(state: IncidentState) -> dict[str, Any]:
        incident_id, book = state["incident_id"], runbook_of(state)
        reason = state.get("reason") or (
            f"{len(state.get('findings', []))} finding(s) remain after {state.get('attempt', 0) + 1} attempt(s)"
        )
        svc.store.event(
            incident_id,
            "escalated",
            f"escalated to {book.escalation.team}: {reason}",
            {"team": book.escalation.team, "channel": book.escalation.channel},
        )
        return {"outcome": "escalated", "reason": reason}

    def report(state: IncidentState) -> dict[str, Any]:
        incident_id = state["incident_id"]
        outcome = state.get("outcome") or ("resolved" if state.get("verified") else "cleared")
        incident = svc.store.get(incident_id)
        opened = datetime.fromisoformat(incident["opened_at"])
        mttr = (datetime.now(UTC) - opened).total_seconds()
        svc.store.close_incident(incident_id, "escalated" if outcome == "escalated" else "resolved", outcome, mttr)
        text = build_report(
            svc.store.get(incident_id), svc.store.events(incident_id), svc.store.actions(incident_id), runbook_of(state)
        )
        svc.store.update(incident_id, report=text)
        svc.on_event("closed", {"runbook": state["runbook"], "outcome": outcome, "mttr_s": mttr})
        svc.snapshots.pop(incident_id, None)
        return {"outcome": outcome}

    graph = StateGraph(IncidentState)
    for name, fn in (
        ("diagnose", diagnose),
        ("plan", plan),
        ("approve", approve),
        ("execute", execute),
        ("verify", verify),
        ("next_attempt", next_attempt),
        ("escalate", escalate),
        ("report", report),
    ):
        graph.add_node(name, fn)
    graph.add_edge(START, "diagnose")
    graph.add_conditional_edges("diagnose", after_diagnose, ["plan", "report"])
    graph.add_conditional_edges("plan", after_plan, ["approve", "execute", "escalate"])
    graph.add_conditional_edges("approve", after_approve, ["execute", "escalate"])
    graph.add_edge("execute", "verify")
    graph.add_conditional_edges("verify", after_verify, ["report", "next_attempt", "escalate"])
    graph.add_edge("next_attempt", "diagnose")
    graph.add_edge("escalate", "report")
    graph.add_edge("report", END)
    return graph.compile(checkpointer=checkpointer)


def _item(decision: dict[str, Any], finding: Finding | None) -> dict[str, Any]:
    assert finding is not None
    return {**decision, "finding": finding.to_dict()}


def _new_step_applies(
    book: Runbook, finding: Finding, history: set[tuple[str, str]], executed: set[tuple[str, str]]
) -> bool:
    for step in book.steps:
        if (finding.id, step.action) in executed:
            continue
        if not step.when.mismatches(finding, history):
            return True
    return False
