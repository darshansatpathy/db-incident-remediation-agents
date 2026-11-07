"""Detectors turn a snapshot into findings: the specific sessions or tables behind an incident.

Detectors never decide what to do. They report what they see, including sessions the agent
must not touch (superusers, background workers, protected applications), so that those
incidents end in an escalation rather than silence.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from typing import Any

from dbremedy.bootstrap import AGENT_ROLES
from dbremedy.snapshot import Session, Snapshot, TableStats

IDLE_IN_TRANSACTION = ("idle in transaction", "idle in transaction (aborted)")


@dataclass(frozen=True)
class Finding:
    id: str
    detector: str
    kind: str  # "session" or "table"
    summary: str
    attributes: dict[str, Any]
    target: dict[str, Any]
    evidence: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "detector": self.detector,
            "kind": self.kind,
            "summary": self.summary,
            "attributes": self.attributes,
            "target": self.target,
            "evidence": self.evidence,
        }


def session_key(session: Session) -> str:
    """Stable across a session's statements and transactions; changes if the pid is reused."""
    raw = f"{session.pid}|{session.backend_start.isoformat() if session.backend_start else ''}"
    return hashlib.sha256(raw.encode()).hexdigest()[:12]


def _session_finding(detector: str, session: Session, summary: str, **extra: Any) -> Finding:
    fingerprint = session.fingerprint
    digest = hashlib.sha256(json.dumps(fingerprint, sort_keys=True).encode()).hexdigest()[:8]
    attributes = {
        "pid": session.pid,
        "state": session.state,
        "application": session.application,
        "user": session.usename,
        "superuser": session.superuser,
        "backend_type": session.backend_type,
        "query_age_s": round(session.query_age_s, 1),
        "state_age_s": round(session.state_age_s, 1),
        "xact_age_s": round(session.xact_age_s, 1),
        "session_key": session_key(session),
        **extra,
    }
    return Finding(
        id=f"s{session.pid}-{digest}",
        detector=detector,
        kind="session",
        summary=summary,
        attributes=attributes,
        target={"type": "session", **fingerprint},
        evidence={"session": session.describe()},
    )


def _ours(session: Session) -> bool:
    return session.usename in AGENT_ROLES


def blocking_locks(snapshot: Snapshot, params: dict[str, Any]) -> list[Finding]:
    """Root blockers of lock waits that have lasted at least ``min_wait_s``."""
    min_wait = float(params.get("min_wait_s", 30))
    by_pid = {s.pid: s for s in snapshot.sessions}
    waiters = [s for s in snapshot.sessions if s.blocked_by and s.wait_event_type == "Lock"]
    blocked_by_root: dict[int, list[Session]] = {}
    for waiter in waiters:
        if waiter.state_age_s < min_wait:
            continue
        for root_pid in _roots(waiter.pid, by_pid):
            blocked_by_root.setdefault(root_pid, []).append(waiter)
    findings = []
    for root_pid, victims in sorted(blocked_by_root.items()):
        root = by_pid.get(root_pid)
        if root is None or _ours(root):
            continue
        apps = Counter(v.application or "unknown" for v in victims)
        finding = _session_finding(
            "blocking_locks",
            root,
            f"pid {root.pid} ({root.application or 'no application'}, {root.state}) blocks "
            f"{len(victims)} session(s) for up to {max(v.state_age_s for v in victims):.0f}s",
            blocked_count=len(victims),
            max_wait_s=round(max(v.state_age_s for v in victims), 1),
        )
        evidence = {
            "blocked": [v.describe() for v in sorted(victims, key=lambda v: -v.state_age_s)[:10]],
            "blocked_applications": dict(apps),
        }
        findings.append(replace(finding, evidence={**finding.evidence, **evidence}))
    return findings


def _roots(pid: int, by_pid: dict[int, Session]) -> set[int]:
    """Follow blocked_by edges to sessions that are not themselves waiting on a lock."""
    roots: set[int] = set()
    seen: set[int] = set()
    stack = [pid]
    while stack:
        current = stack.pop()
        if current in seen:
            continue
        seen.add(current)
        session = by_pid.get(current)
        parents = session.blocked_by if session else ()
        if current != pid and (not parents or session is None or session.wait_event_type != "Lock"):
            roots.add(current)
            continue
        stack.extend(parents)
    return roots


def long_running_queries(snapshot: Snapshot, params: dict[str, Any]) -> list[Finding]:
    """Active statements running longer than ``min_query_age_s`` that are not waiting on a lock."""
    threshold = float(params.get("min_query_age_s", 300))
    findings = []
    for s in snapshot.client_sessions:
        if _ours(s) or s.state != "active" or s.wait_event_type == "Lock" or s.query_age_s < threshold:
            continue
        findings.append(
            _session_finding(
                "long_running_queries",
                s,
                f"pid {s.pid} ({s.application or 'no application'}) has run one statement for {s.query_age_s:.0f}s",
            )
        )
    return findings


def idle_in_transaction(snapshot: Snapshot, params: dict[str, Any]) -> list[Finding]:
    """Sessions sitting idle inside an open transaction for at least ``min_state_age_s``."""
    threshold = float(params.get("min_state_age_s", 300))
    findings = []
    for s in snapshot.client_sessions:
        if _ours(s) or s.state not in IDLE_IN_TRANSACTION or s.state_age_s < threshold:
            continue
        findings.append(
            _session_finding(
                "idle_in_transaction",
                s,
                f"pid {s.pid} ({s.application or 'no application'}) idle in an open transaction "
                f"for {s.state_age_s:.0f}s",
            )
        )
    return findings


def connection_saturation(snapshot: Snapshot, params: dict[str, Any]) -> list[Finding]:
    """When connection usage crosses ``max_ratio``: the idle sessions of the applications hogging them."""
    max_ratio = float(params.get("max_ratio", 0.8))
    min_idle = float(params.get("min_idle_s", 60))
    min_share = float(params.get("min_application_share", 0.3))
    clients = snapshot.client_sessions
    usable = max(1, snapshot.max_connections - snapshot.reserved_connections)
    ratio = len(clients) / usable
    if ratio < max_ratio:
        return []
    by_app = Counter(s.application or "unknown" for s in clients)
    hogs = {app for app, count in by_app.items() if count / len(clients) >= min_share}
    findings = []
    for s in clients:
        if _ours(s) or (s.application or "unknown") not in hogs:
            continue
        if s.state != "idle" or s.state_age_s < min_idle:
            continue
        findings.append(
            _session_finding(
                "connection_saturation",
                s,
                f"pid {s.pid} ({s.application or 'no application'}) idle for {s.state_age_s:.0f}s while "
                f"connections are at {ratio:.0%}",
                usage_ratio=round(ratio, 3),
                application_sessions=by_app[s.application or "unknown"],
            )
        )
    return findings


def table_bloat(snapshot: Snapshot, params: dict[str, Any]) -> list[Finding]:
    """Tables whose dead tuples exceed both ``min_dead_ratio`` and ``min_dead_tuples``."""
    min_ratio = float(params.get("min_dead_ratio", 0.2))
    min_dead = int(params.get("min_dead_tuples", 10000))
    findings = []
    for t in snapshot.tables:
        if t.dead_ratio < min_ratio or t.dead_tuples < min_dead:
            continue
        findings.append(_table_finding(t))
    return findings


def _table_finding(t: TableStats) -> Finding:
    return Finding(
        id=f"t-{t.qualified}",
        detector="table_bloat",
        kind="table",
        summary=f"{t.qualified} has {t.dead_tuples} dead tuples ({t.dead_ratio:.0%}), last autovacuum "
        f"{t.last_autovacuum.isoformat() if t.last_autovacuum else 'never'}",
        attributes={
            "table": t.qualified,
            "dead_tuples": t.dead_tuples,
            "dead_ratio": round(t.dead_ratio, 3),
            "size_mb": round(t.size_mb, 2),
        },
        target={"type": "table", "schema": t.schema, "name": t.name},
    )


Detector = Callable[[Snapshot, dict[str, Any]], list[Finding]]

DETECTORS: dict[str, Detector] = {
    "blocking_locks": blocking_locks,
    "long_running_queries": long_running_queries,
    "idle_in_transaction": idle_in_transaction,
    "connection_saturation": connection_saturation,
    "table_bloat": table_bloat,
}
