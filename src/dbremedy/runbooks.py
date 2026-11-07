"""Runbooks: which detector to run for an alert, and which actions are allowed on what.

A runbook is the reviewed, versioned description of how an incident type is handled. The
planner (rules or a model) may only propose actions a runbook step allows, and the governor
re-checks every proposal against the step's conditions on fresh data.

Numbers in ``detector.params`` and step conditions can reference ``parameters`` as
``${name}``. Parameters can be overridden per deployment, which is how the evaluation runs
the same runbooks with second-scale thresholds.
"""

from __future__ import annotations

import fnmatch
import re
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from dbremedy.detectors import DETECTORS, Finding

ActionName = Literal["cancel_backend", "terminate_backend", "vacuum_analyze"]
SESSION_ACTIONS = {"cancel_backend", "terminate_backend"}
TABLE_ACTIONS = {"vacuum_analyze"}
DETECTOR_KIND = {
    "blocking_locks": "session",
    "long_running_queries": "session",
    "idle_in_transaction": "session",
    "connection_saturation": "session",
    "table_bloat": "table",
}
_REF = re.compile(r"^\$\{([a-z_][a-z0-9_]*)\}$")


class RunbookError(ValueError):
    pass


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Conditions(Strict):
    state_in: list[str] | None = None
    application_matches: list[str] | None = None
    application_not_matches: list[str] | None = None
    min_query_age_s: float | None = None
    min_state_age_s: float | None = None
    min_xact_age_s: float | None = None
    min_blocked: int | None = None
    min_dead_ratio: float | None = None
    min_dead_tuples: int | None = None
    max_size_mb: float | None = None
    after: ActionName | None = Field(
        default=None, description="Only once this action has already been applied to the same session."
    )

    def mismatches(self, finding: Finding, history: set[tuple[str, str]]) -> list[str]:
        """Reasons the finding does not satisfy these conditions (empty when it does)."""
        a = finding.attributes
        reasons: list[str] = []
        if self.state_in is not None and a.get("state") not in self.state_in:
            reasons.append(f"state {a.get('state')!r} not in {self.state_in}")
        app = str(a.get("application", ""))
        if self.application_matches is not None and not any(
            fnmatch.fnmatchcase(app, p) for p in self.application_matches
        ):
            reasons.append(f"application {app!r} does not match {self.application_matches}")
        if self.application_not_matches is not None and any(
            fnmatch.fnmatchcase(app, p) for p in self.application_not_matches
        ):
            reasons.append(f"application {app!r} matches an excluded pattern")
        for field_name, attribute in (
            ("min_query_age_s", "query_age_s"),
            ("min_state_age_s", "state_age_s"),
            ("min_xact_age_s", "xact_age_s"),
            ("min_blocked", "blocked_count"),
            ("min_dead_ratio", "dead_ratio"),
            ("min_dead_tuples", "dead_tuples"),
        ):
            limit = getattr(self, field_name)
            if limit is not None and float(a.get(attribute, 0)) < limit:
                reasons.append(f"{attribute} {a.get(attribute, 0)} below {limit}")
        if self.max_size_mb is not None and float(a.get("size_mb", 0)) > self.max_size_mb:
            reasons.append(f"size_mb {a.get('size_mb')} above {self.max_size_mb}")
        if self.after is not None and (str(a.get("session_key")), self.after) not in history:
            reasons.append(f"{self.after} has not been tried on this session yet")
        return reasons


class Step(Strict):
    name: str
    action: ActionName
    when: Conditions = Conditions()
    approval: Literal["auto", "required"] = "auto"


class DetectorRef(Strict):
    name: str
    params: dict[str, float] = {}

    @field_validator("name")
    @classmethod
    def known(cls, value: str) -> str:
        if value not in DETECTORS:
            raise ValueError(f"unknown detector {value!r}; known: {sorted(DETECTORS)}")
        return value


class Verify(Strict):
    settle_s: float = 1.0
    interval_s: float = 2.0
    timeout_s: float = 30.0
    stable_s: float = Field(default=0.0, ge=0, description="How long the detector must stay quiet to count as fixed.")


class Limits(Strict):
    max_actions: int = Field(default=5, ge=0)
    max_attempts: int = Field(default=3, ge=1)


class Escalation(Strict):
    team: str
    channel: str = ""


class Runbook(Strict):
    id: str = Field(pattern=r"^[a-z][a-z0-9-]{1,48}$")
    version: str = Field(pattern=r"^\d+\.\d+\.\d+$")
    title: str
    severity: Literal["critical", "warning", "info"] = "warning"
    alerts: list[str] = Field(min_length=1)
    detector: DetectorRef
    parameters: dict[str, float] = {}
    steps: list[Step] = []
    verify: Verify = Verify()
    limits: Limits = Limits()
    escalation: Escalation
    guidance: str = ""
    recommendations: list[str] = []

    @model_validator(mode="after")
    def actions_fit_detector(self) -> Runbook:
        kind = DETECTOR_KIND[self.detector.name]
        allowed = SESSION_ACTIONS if kind == "session" else TABLE_ACTIONS
        for step in self.steps:
            if step.action not in allowed:
                raise ValueError(f"step {step.name!r}: {step.action} cannot act on {kind} findings")
        return self

    @property
    def actions(self) -> set[str]:
        return {s.action for s in self.steps}

    def matching_steps(
        self, finding: Finding, action: str, history: set[tuple[str, str]]
    ) -> tuple[list[Step], list[str]]:
        """Steps allowing ``action`` on ``finding``, and why the other steps for that action don't."""
        matched: list[Step] = []
        reasons: list[str] = []
        for step in self.steps:
            if step.action != action:
                continue
            problems = step.when.mismatches(finding, history)
            if problems:
                reasons.append(f"{step.name}: " + "; ".join(problems))
            else:
                matched.append(step)
        return matched, reasons


def _resolve(value: Any, params: dict[str, float], where: str) -> Any:
    if isinstance(value, str):
        match = _REF.match(value)
        if match:
            name = match.group(1)
            if name not in params:
                raise RunbookError(f"{where}: unknown parameter ${{{name}}}")
            return params[name]
        return value
    if isinstance(value, dict):
        return {k: _resolve(v, params, f"{where}.{k}") for k, v in value.items()}
    if isinstance(value, list):
        return [_resolve(v, params, where) for v in value]
    return value


def parse(data: dict[str, Any], overrides: dict[str, float] | None = None, source: str = "runbook") -> Runbook:
    if not isinstance(data, dict):
        raise RunbookError(f"{source}: expected a mapping")
    params = {**{k: float(v) for k, v in (data.get("parameters") or {}).items()}}
    for key, value in (overrides or {}).items():
        if key not in params:
            raise RunbookError(f"{source}: override for unknown parameter {key!r}")
        params[key] = float(value)
    resolved = {k: v for k, v in data.items() if k != "parameters"}
    resolved = _resolve(resolved, params, source)
    resolved["parameters"] = params
    try:
        return Runbook.model_validate(resolved)
    except ValueError as exc:
        raise RunbookError(f"{source}: {exc}") from exc


def load_folder(folder: Path, overrides: dict[str, dict[str, float]] | None = None) -> dict[str, Runbook]:
    overrides = overrides or {}
    books: dict[str, Runbook] = {}
    for path in sorted(folder.glob("*.yaml")):
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        book = parse(data, overrides.get(str(data.get("id", ""))), source=path.name)
        if book.id in books:
            raise RunbookError(f"{path.name}: duplicate runbook id {book.id!r}")
        books[book.id] = book
    unknown = set(overrides) - set(books)
    if unknown:
        raise RunbookError(f"overrides for unknown runbooks: {sorted(unknown)}")
    alerts: dict[str, str] = {}
    for book in books.values():
        for alert in book.alerts:
            if alert in alerts:
                raise RunbookError(f"alert {alert!r} is handled by both {alerts[alert]} and {book.id}")
            alerts[alert] = book.id
    return books


def for_alert(books: dict[str, Runbook], alertname: str) -> Runbook | None:
    return next((b for b in books.values() if alertname in b.alerts), None)
