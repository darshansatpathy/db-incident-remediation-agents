"""Planners propose remediation actions. They have no authority: the governor decides."""

from __future__ import annotations

from typing import Protocol

from dbremedy.detectors import Finding
from dbremedy.governor import Proposal
from dbremedy.runbooks import Runbook
from dbremedy.snapshot import Snapshot


class Planner(Protocol):
    name: str

    def propose(
        self, runbook: Runbook, findings: list[Finding], history: set[tuple[str, str]], snapshot: Snapshot | None
    ) -> list[Proposal]: ...


def build(name: str) -> Planner:
    if name == "rule":
        from dbremedy.planners.rule import RulePlanner

        return RulePlanner()
    if name == "rogue":
        from dbremedy.planners.rogue import RoguePlanner

        return RoguePlanner()
    if name == "llm":
        from dbremedy.planners.llm import LLMPlanner

        return LLMPlanner.from_env()
    raise ValueError(f"unknown planner {name!r}; use rule, llm or rogue")
