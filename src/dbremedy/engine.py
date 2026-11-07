"""The engine: opens incidents from alerts, runs the graph, and resumes it after approvals."""

from __future__ import annotations

import threading
from typing import Any

from langgraph.types import Command

from dbremedy.alerts import DEFAULT_KEY_LABELS, AlertmanagerPayload, incident_key, summary
from dbremedy.graph import Services, build_graph
from dbremedy.report import build_report
from dbremedy.runbooks import for_alert


class NotPaused(RuntimeError):
    pass


class Engine:
    def __init__(self, svc: Services, checkpointer: Any, key_labels: tuple[str, ...] = DEFAULT_KEY_LABELS) -> None:
        self.svc = svc
        self.graph = build_graph(svc, checkpointer)
        self.key_labels = key_labels
        self._locks: dict[str, threading.Lock] = {}
        self._guard = threading.Lock()

    def _lock(self, incident_id: str) -> threading.Lock:
        with self._guard:
            return self._locks.setdefault(incident_id, threading.Lock())

    def receive(self, payload: AlertmanagerPayload) -> list[dict[str, Any]]:
        """Open (or attach to) one incident per firing alert. Resolved notifications are noted, not acted on."""
        results = []
        for alert in payload.alerts:
            if not alert.name:
                continue
            key = incident_key(alert.labels, self.key_labels)
            if alert.status == "resolved":
                for incident in self.svc.store.incidents(limit=200):
                    if incident["key"] == key and incident["status"] in ("open", "running", "awaiting_approval"):
                        self.svc.store.event(incident["id"], "alert", "Alertmanager reports the alert resolved", {})
                continue
            book = for_alert(self.svc.runbooks, alert.name)
            incident, created = self.svc.store.open_or_attach(
                key,
                alert.name,
                book.id if book else None,
                book.severity if book else "warning",
                alert.labels,
                summary(alert),
            )
            self.svc.on_event("alert", {"alertname": alert.name, "created": created})
            results.append({"incident_id": incident["id"], "created": created, "runbook": incident["runbook"]})
        return results

    def run(self, incident_id: str) -> dict[str, Any]:
        with self._lock(incident_id):
            incident = self.svc.store.get(incident_id)
            if incident["status"] != "open":
                return incident
            if incident["runbook"] is None:
                self.svc.store.event(incident_id, "escalated", "no runbook handles this alert", {})
                self.svc.store.close_incident(incident_id, "escalated", "escalated", None)
                self.svc.store.update(
                    incident_id,
                    report=build_report(self.svc.store.get(incident_id), self.svc.store.events(incident_id), [], None),
                )
                self.svc.on_event("closed", {"runbook": "none", "outcome": "escalated", "mttr_s": None})
                return self.svc.store.get(incident_id)
            self.graph.invoke(
                {"incident_id": incident_id, "runbook": incident["runbook"], "attempt": 0},
                {"configurable": {"thread_id": incident_id}},
            )
            return self.svc.store.get(incident_id)

    def decide(self, incident_id: str, approved: bool, approver: str) -> dict[str, Any]:
        with self._lock(incident_id):
            incident = self.svc.store.get(incident_id)
            if incident["status"] != "awaiting_approval":
                raise NotPaused(f"{incident_id} is {incident['status']}, not awaiting approval")
            self.graph.invoke(
                Command(resume={"approved": approved, "approver": approver}),
                {"configurable": {"thread_id": incident_id}},
            )
            return self.svc.store.get(incident_id)

    def recover(self) -> list[str]:
        """After a restart: finish incidents that were interrupted mid-run, from their last checkpoint.

        Incidents waiting for approval stay paused; their checkpoint resumes when someone decides.
        """
        resumed = []
        for incident in self.svc.store.incidents(limit=500, status="running"):
            with self._lock(incident["id"]):
                config = {"configurable": {"thread_id": incident["id"]}}
                if self.graph.get_state(config).next:
                    self.svc.store.event(
                        incident["id"], "recovered", "resumed from the last checkpoint after a restart"
                    )
                    self.graph.invoke(None, config)
                    resumed.append(incident["id"])
        for incident in self.svc.store.incidents(limit=500, status="open"):
            self.run(incident["id"])
            resumed.append(incident["id"])
        return resumed

    def detail(self, incident_id: str) -> dict[str, Any]:
        incident = self.svc.store.get(incident_id)
        return {
            **incident,
            "events": self.svc.store.events(incident_id),
            "actions": self.svc.store.actions(incident_id),
        }
