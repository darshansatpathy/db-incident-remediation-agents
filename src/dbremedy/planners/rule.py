"""The reference planner: for each finding, the first runbook step whose conditions hold."""

from __future__ import annotations

from dbremedy.detectors import Finding
from dbremedy.governor import Proposal
from dbremedy.runbooks import Runbook
from dbremedy.snapshot import Snapshot


class RulePlanner:
    name = "rule"

    def propose(
        self, runbook: Runbook, findings: list[Finding], history: set[tuple[str, str]], snapshot: Snapshot | None
    ) -> list[Proposal]:
        proposals = []
        for finding in findings:
            for step in runbook.steps:
                if not step.when.mismatches(finding, history):
                    proposals.append(Proposal(finding.id, step.action, f"runbook step: {step.name}", self.name))
                    break
        return proposals
