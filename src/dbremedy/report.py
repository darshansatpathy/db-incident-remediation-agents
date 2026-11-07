"""The incident report: a blameless postmortem draft written from the timeline and the ledger."""

from __future__ import annotations

from collections import Counter
from typing import Any

from dbremedy.runbooks import Runbook

SHOWN = 8  # longer lists are summarised, the full detail stays in the API


def _plural(count: int, word: str) -> str:
    if count == 1:
        return word
    return word[:-1] + "ies" if word.endswith("y") else word + "s"


def _cell(value: Any) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ")


def build_report(
    incident: dict[str, Any], events: list[dict[str, Any]], actions: list[dict[str, Any]], runbook: Runbook | None
) -> str:
    lines = [
        f"# Incident {incident['id']}: {runbook.title if runbook else incident['alertname']}",
        "",
        "| | |",
        "|---|---|",
        f"| Alert | `{incident['alertname']}` ({incident['alerts']} {_plural(incident['alerts'], 'delivery')}) |",
        f"| Runbook | {f'`{runbook.id}` {runbook.version}' if runbook else 'none'} |",
        f"| Outcome | **{incident.get('outcome') or incident['status']}** |",
        f"| Opened | {incident['opened_at']} |",
        f"| Closed | {incident.get('resolved_at') or 'open'} |",
    ]
    if incident.get("mttr_s") is not None:
        lines.append(f"| Time to {incident.get('outcome') or 'close'} | {incident['mttr_s']:.1f}s |")
    labels = ", ".join(f"{k}={v}" for k, v in sorted(incident["labels"].items()))
    lines += [f"| Labels | {_cell(labels)} |", ""]

    first = next((e for e in events if e["kind"] == "diagnosis"), None)
    if first:
        lines += ["## What was found", ""]
        findings = first["data"].get("findings", [])
        if not findings:
            lines.append("Nothing: the condition had cleared by the time the agent looked.")
        for f in findings[:SHOWN]:
            lines.append(f"- {_cell(f['summary'])}")
            query = f.get("evidence", {}).get("session", {}).get("query")
            if query:
                lines.append(f"  - query: `{_cell(query[:200])}`")
        if len(findings) > SHOWN:
            apps = Counter(str(f.get("attributes", {}).get("application", "?")) for f in findings[SHOWN:])
            grouped = ", ".join(f"{app} x{n}" for app, n in apps.most_common())
            lines.append(f"- ... and {len(findings) - SHOWN} more ({_cell(grouped)})")
        lines.append("")

    executed = [a for a in actions if a["status"] in ("done", "stale", "failed", "dry_run")]
    lines += ["## What was done", ""]
    if executed:
        lines += ["| Action | Target | Step | Approved by | Result |", "|---|---|---|---|---|"]
        for a in executed[:SHOWN]:
            target = a["target"] or {}
            where = (
                f"pid {target.get('pid')}"
                if target.get("type") == "session"
                else (f"{target.get('schema')}.{target.get('name')}" if target else a["finding_id"])
            )
            lines.append(
                f"| `{a['action']}` | {_cell(where)} | {_cell(a.get('step') or '')} | "
                f"{_cell(a.get('approved_by') or 'auto')} | {a['status']}: {_cell(a['detail'])} |"
            )
        if len(executed) > SHOWN:
            rest = Counter(f"{a['action']} {a['status']}" for a in executed[SHOWN:])
            tally = ", ".join(f"{k} x{n}" for k, n in rest.items())
            lines.append(f"| ... {len(executed) - SHOWN} more | | | | {tally} |")
    else:
        lines.append("No action was taken.")
    lines.append("")

    refused = [a for a in actions if a["status"] in ("denied", "rejected")]
    if refused:
        codes = Counter((a["reasons"][0].split(":", 1)[0] if a["reasons"] else a["status"]) for a in refused)
        lines += ["## What was refused", ""]
        lines += [f"- {code}: {count}" for code, count in codes.most_common()]
        for a in refused[:10]:
            lines.append(f"  - `{a['action']}` on {a['finding_id']}: {_cell('; '.join(a['reasons']) or a['detail'])}")
        lines.append("")

    lines += ["## Timeline", ""]
    lines += [f"- `{e['at'][11:23]}` **{e['kind']}**: {_cell(e['message'])}" for e in events]
    lines.append("")

    if runbook:
        lines += ["## Follow-up", ""]
        if incident.get("outcome") == "escalated":
            lines.append(f"- Escalated to **{runbook.escalation.team}** {runbook.escalation.channel}.".rstrip())
        lines += [f"- {r}" for r in runbook.recommendations]
        if runbook.guidance:
            lines += ["", "## Runbook guidance", "", runbook.guidance.strip()]
    return "\n".join(lines) + "\n"
