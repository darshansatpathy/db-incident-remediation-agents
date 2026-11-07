"""Alertmanager webhook payloads (version 4)."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

DEFAULT_KEY_LABELS = ("instance", "datname", "cluster")


class Alert(BaseModel):
    model_config = ConfigDict(extra="allow")

    status: Literal["firing", "resolved"]
    labels: dict[str, str]
    annotations: dict[str, str] = {}
    startsAt: str = ""
    fingerprint: str = ""

    @property
    def name(self) -> str:
        return self.labels.get("alertname", "")


class AlertmanagerPayload(BaseModel):
    model_config = ConfigDict(extra="allow")

    version: str = "4"
    status: Literal["firing", "resolved"] = "firing"
    receiver: str = ""
    alerts: list[Alert] = Field(max_length=500)


def incident_key(labels: dict[str, str], key_labels: tuple[str, ...] = DEFAULT_KEY_LABELS) -> str:
    parts = [labels.get("alertname", "")] + [f"{k}={labels[k]}" for k in key_labels if k in labels]
    return "|".join(parts)


def summary(alert: Alert) -> str:
    return alert.annotations.get("summary") or alert.annotations.get("description") or alert.name


def payload_for(alertname: str, labels: dict[str, str] | None = None, summary_text: str = "") -> dict[str, Any]:
    """An Alertmanager-shaped payload, for manual incidents, the watcher and the scenarios."""
    return {
        "version": "4",
        "status": "firing",
        "receiver": "dbremedy",
        "alerts": [
            {
                "status": "firing",
                "labels": {"alertname": alertname, **(labels or {})},
                "annotations": {"summary": summary_text or alertname},
            }
        ],
    }
