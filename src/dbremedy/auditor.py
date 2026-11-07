"""An independent check of what was done, written without the governor's code.

It reads the raw policy and runbook YAML and the action ledger, and asks: was every
attempted action in the catalog and in a runbook step for that action, within the caps,
approved where the step, a freeze or a large batch required it, and never aimed at a session
that had to survive?
"""

from __future__ import annotations

import fnmatch
from collections import Counter
from pathlib import Path
from typing import Any

import yaml

CATALOG = {"cancel_backend", "terminate_backend", "vacuum_analyze"}
ATTEMPTED = {"done", "stale", "failed", "dry_run"}
CAP_KEYS = {
    "terminate_backend": "max_terminations_per_incident",
    "cancel_backend": "max_cancellations_per_incident",
    "vacuum_analyze": "max_vacuums_per_incident",
}


def load_raw(runbooks_dir: Path, policy_path: Path) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    books = {}
    for path in sorted(runbooks_dir.glob("*.yaml")):
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        books[data["id"]] = data
    return books, yaml.safe_load(policy_path.read_text(encoding="utf-8")) or {}


def audit(
    ledger: list[dict[str, Any]],
    runbook: dict[str, Any] | None,
    policy: dict[str, Any],
    *,
    must_survive: set[int],
    protected_applications: dict[int, str],
    freeze: bool,
) -> list[str]:
    violations: list[str] = []
    attempted = [row for row in ledger if row["status"] in ATTEMPTED]
    steps = {s["name"]: s for s in (runbook or {}).get("steps", [])}
    counts = Counter(row["action"] for row in attempted if row["status"] in ("done", "dry_run"))
    limits = policy.get("limits", {})
    patterns = policy.get("protected", {}).get("applications", [])

    for row in attempted:
        action, target = row["action"], row.get("target") or {}
        if action not in CATALOG:
            violations.append(f"{action} is not a catalog action")
        step = steps.get(row.get("step") or "")
        if step is None or step.get("action") != action:
            violations.append(f"{action} on {row['finding_id']} does not come from a runbook step for that action")
        elif step.get("approval") == "required" and not row.get("approved_by"):
            violations.append(f"{action} on {row['finding_id']} ran without the approval its step requires")
        if freeze and not row.get("approved_by"):
            violations.append(f"{action} on {row['finding_id']} ran during a freeze without approval")
        pid = target.get("pid")
        if pid is not None and pid in must_survive:
            violations.append(f"{action} was aimed at pid {pid}, which had to survive")
        app = protected_applications.get(int(pid)) if pid is not None else None
        if app is not None and any(fnmatch.fnmatchcase(app, p) for p in patterns):
            violations.append(f"{action} was aimed at protected application {app!r}")

    for action, count in counts.items():
        cap = limits.get(CAP_KEYS.get(action, ""), 0)
        if count > cap:
            violations.append(f"{count} x {action} exceeds the policy cap of {cap}")
    max_actions = (runbook or {}).get("limits", {}).get("max_actions")
    if max_actions is not None and sum(counts.values()) > int(max_actions):
        violations.append(f"{sum(counts.values())} actions exceed the runbook limit of {max_actions}")
    unapproved_terminations = sum(
        1 for row in attempted if row["action"] == "terminate_backend" and not row.get("approved_by")
    )
    threshold = limits.get("batch_approval_threshold")
    if threshold is not None and unapproved_terminations > int(threshold):
        violations.append(f"{unapproved_terminations} sessions ended without approval (threshold {threshold})")
    return violations
