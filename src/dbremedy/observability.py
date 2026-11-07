"""JSON logs and a small Prometheus text exposition, with no extra dependencies."""

from __future__ import annotations

import json
import logging
import sys
import threading
from collections import Counter
from typing import Any

LOG = logging.getLogger("dbremedy")


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        data: dict[str, Any] = {"level": record.levelname.lower(), "msg": record.getMessage(), "logger": record.name}
        data.update(getattr(record, "fields", {}))
        return json.dumps(data, default=str)


def configure_logging(level: str = "INFO") -> None:
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(JsonFormatter())
    LOG.handlers[:] = [handler]
    LOG.setLevel(level.upper())
    LOG.propagate = False


def event(msg: str, **fields: Any) -> None:
    LOG.info(msg, extra={"fields": fields})


MTTR_BUCKETS = (5, 10, 30, 60, 120, 300, 600, 1800)


class Metrics:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.alerts: Counter[tuple[str, str]] = Counter()
        self.incidents: Counter[tuple[str, str]] = Counter()
        self.actions: Counter[tuple[str, str]] = Counter()
        self.denied: Counter[tuple[str, str]] = Counter()
        self.approvals: Counter[str] = Counter()
        self.mttr_buckets: Counter[float] = Counter()
        self.mttr_sum = 0.0
        self.mttr_count = 0

    def on_event(self, kind: str, data: dict[str, Any]) -> None:
        with self._lock:
            if kind == "alert":
                self.alerts[(data["alertname"], "new" if data["created"] else "attached")] += 1
            elif kind == "action":
                self.actions[(data["action"], data["status"])] += 1
            elif kind == "denied":
                self.denied[(data["code"], data["action"])] += 1
            elif kind == "approval":
                self.approvals["approved" if data["approved"] else "denied"] += 1
            elif kind == "closed":
                self.incidents[(data["runbook"], data["outcome"])] += 1
                if data.get("mttr_s") is not None:
                    self.mttr_sum += data["mttr_s"]
                    self.mttr_count += 1
                    for bound in MTTR_BUCKETS:
                        if data["mttr_s"] <= bound:
                            self.mttr_buckets[bound] += 1
        event(kind, **data)

    def render(self) -> str:
        with self._lock:
            out = [
                "# HELP dbremedy_alerts_total Alerts received, by whether they opened an incident.",
                "# TYPE dbremedy_alerts_total counter",
            ]
            out += [
                f'dbremedy_alerts_total{{alertname="{a}",result="{r}"}} {n}'
                for (a, r), n in sorted(self.alerts.items())
            ]
            out += ["# HELP dbremedy_incidents_total Closed incidents.", "# TYPE dbremedy_incidents_total counter"]
            out += [
                f'dbremedy_incidents_total{{runbook="{b}",outcome="{o}"}} {n}'
                for (b, o), n in sorted(self.incidents.items())
            ]
            out += ["# HELP dbremedy_actions_total Executed actions.", "# TYPE dbremedy_actions_total counter"]
            out += [
                f'dbremedy_actions_total{{action="{a}",status="{s}"}} {n}' for (a, s), n in sorted(self.actions.items())
            ]
            out += [
                "# HELP dbremedy_denied_total Proposals refused by the governor.",
                "# TYPE dbremedy_denied_total counter",
            ]
            out += [
                f'dbremedy_denied_total{{reason="{c}",action="{a}"}} {n}' for (c, a), n in sorted(self.denied.items())
            ]
            out += ["# HELP dbremedy_approvals_total Approval decisions.", "# TYPE dbremedy_approvals_total counter"]
            out += [f'dbremedy_approvals_total{{decision="{d}"}} {n}' for d, n in sorted(self.approvals.items())]
            out += [
                "# HELP dbremedy_time_to_close_seconds Time from opening an incident to closing it.",
                "# TYPE dbremedy_time_to_close_seconds histogram",
            ]
            out += [f'dbremedy_time_to_close_seconds_bucket{{le="{b}"}} {self.mttr_buckets[b]}' for b in MTTR_BUCKETS]
            out += [
                f'dbremedy_time_to_close_seconds_bucket{{le="+Inf"}} {self.mttr_count}',
                f"dbremedy_time_to_close_seconds_sum {self.mttr_sum:.3f}",
                f"dbremedy_time_to_close_seconds_count {self.mttr_count}",
            ]
        return "\n".join(out) + "\n"
