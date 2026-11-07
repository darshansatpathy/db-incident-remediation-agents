"""The governor: every proposed action is checked here, whoever proposed it.

Planners (rules or a model) only propose. A proposal is allowed when the action exists, the
runbook allows it, its target is a finding from this incident's latest diagnosis, a runbook
step's conditions hold for that finding on fresh data, the session is not protected, and the
incident's caps are not exhausted. Approval is required when the step says so, when a change
freeze is on, or when the incident would end more sessions without a person than the policy's
batch threshold. The governor can make an
action stricter than the runbook, never looser.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from dbremedy.actions import DESCRIPTIONS
from dbremedy.bootstrap import AGENT_ROLES
from dbremedy.detectors import Finding
from dbremedy.policy import Policy
from dbremedy.runbooks import SESSION_ACTIONS, Runbook


@dataclass(frozen=True)
class Proposal:
    finding_id: str
    action: str
    reason: str = ""
    source: str = "rule"

    def to_dict(self) -> dict[str, Any]:
        return {"finding_id": self.finding_id, "action": self.action, "reason": self.reason, "source": self.source}


@dataclass
class Decision:
    proposal: Proposal
    verdict: str  # allow | approval | deny
    reasons: list[str] = field(default_factory=list)
    step: str | None = None
    finding: Finding | None = None

    @property
    def code(self) -> str:
        return self.reasons[0].split(":", 1)[0] if self.reasons else self.verdict

    def to_dict(self) -> dict[str, Any]:
        return {
            **self.proposal.to_dict(),
            "verdict": self.verdict,
            "reasons": self.reasons,
            "step": self.step,
            "target": self.finding.target if self.finding else None,
        }


@dataclass(frozen=True)
class Context:
    runbook: Runbook
    policy: Policy
    findings: dict[str, Finding]
    history: set[tuple[str, str]]  # (session_key, action) already executed in this incident
    executed: set[tuple[str, str]]  # (finding_id, action) already executed in this incident
    counts: Counter[str]  # executed actions in this incident, by action
    now: datetime
    freeze: bool = False
    unapproved_terminations: int = 0  # sessions already ended in this incident without a person


def _protection(finding: Finding, policy: Policy) -> str | None:
    a = finding.attributes
    if policy.never_touch.superusers and a.get("superuser"):
        return "protected: superuser session"
    if policy.never_touch.background_workers and a.get("backend_type") not in (None, "client backend"):
        return f"protected: {a.get('backend_type')} is not a client session"
    if a.get("user") in AGENT_ROLES:
        return "protected: the agent's own session"
    if a.get("user") in policy.protected.users:
        return f"protected: user {a.get('user')!r}"
    pattern = policy.protected.application(str(a.get("application", "")))
    if pattern:
        return f"protected: application {a.get('application')!r} matches {pattern!r}"
    return None


def review(proposals: list[Proposal], ctx: Context) -> list[Decision]:
    decisions: list[Decision] = []
    planned: Counter[str] = Counter()
    seen: set[tuple[str, str]] = set()
    for proposal in proposals:
        decision = _review_one(proposal, ctx, planned, seen)
        if decision.verdict != "deny":
            planned[proposal.action] += 1
            seen.add((proposal.finding_id, proposal.action))
        decisions.append(decision)

    auto_terminations = [d for d in decisions if d.verdict == "allow" and d.proposal.action == "terminate_backend"]
    threshold = ctx.policy.limits.batch_approval_threshold
    total = ctx.unapproved_terminations + len(auto_terminations)
    if total > threshold:
        for d in auto_terminations:
            d.verdict = "approval"
            d.reasons.append(
                f"batch: {total} sessions would be ended in this incident without a person (threshold {threshold})"
            )
    if ctx.policy.frozen(ctx.now, ctx.freeze):
        for d in decisions:
            if d.verdict == "allow":
                d.verdict = "approval"
                d.reasons.append("freeze: a change freeze is in effect")
    return decisions


def _review_one(proposal: Proposal, ctx: Context, planned: Counter[str], seen: set[tuple[str, str]]) -> Decision:
    action = proposal.action
    if action not in DESCRIPTIONS:
        return Decision(proposal, "deny", [f"unknown_action: {action!r} is not in the action catalog"])
    if action not in ctx.runbook.actions:
        return Decision(proposal, "deny", [f"not_in_runbook: {ctx.runbook.id} has no step that uses {action}"])
    finding = ctx.findings.get(proposal.finding_id)
    if finding is None:
        return Decision(proposal, "deny", [f"unknown_target: {proposal.finding_id!r} is not a current finding"])
    if (action in SESSION_ACTIONS) != (finding.kind == "session"):
        return Decision(proposal, "deny", [f"wrong_target: {action} cannot act on a {finding.kind}"], finding=finding)
    protection = _protection(finding, ctx.policy) if finding.kind == "session" else None
    if protection:
        return Decision(proposal, "deny", [protection], finding=finding)
    key = (proposal.finding_id, action)
    if key in ctx.executed or key in seen:
        return Decision(
            proposal, "deny", [f"duplicate: {action} already applied to {proposal.finding_id}"], finding=finding
        )
    steps, mismatches = ctx.runbook.matching_steps(finding, action, ctx.history)
    if not steps:
        return Decision(proposal, "deny", ["conditions_not_met: " + " | ".join(mismatches)], finding=finding)
    total = sum(ctx.counts.values()) + sum(planned.values())
    if total >= ctx.runbook.limits.max_actions:
        return Decision(
            proposal, "deny", [f"cap_reached: runbook allows {ctx.runbook.limits.max_actions} actions"], finding=finding
        )
    cap = ctx.policy.limits.cap(action)
    if ctx.counts[action] + planned[action] >= cap:
        return Decision(proposal, "deny", [f"cap_reached: policy allows {cap} {action} per incident"], finding=finding)
    auto = [s for s in steps if s.approval == "auto"]
    step = auto[0] if auto else steps[0]
    if auto:
        return Decision(proposal, "allow", [], step=step.name, finding=finding)
    return Decision(proposal, "approval", [f"step: {step.name!r} requires approval"], step=step.name, finding=finding)
