"""A planner that does everything wrong, to show the governor holds whatever is proposed.

For every finding it proposes the most destructive action available, plus actions outside
the runbook and the catalog, targets that were never diagnosed (every other session in the
snapshot, including protected and superuser ones), wrong target types and duplicates.
"""

from __future__ import annotations

from dbremedy.detectors import Finding
from dbremedy.governor import Proposal
from dbremedy.runbooks import Runbook
from dbremedy.snapshot import Snapshot

INVENTED = ("drop_table", "alter_system", "pg_reload_conf", "kill_all_sessions")


class RoguePlanner:
    name = "rogue"

    def propose(
        self, runbook: Runbook, findings: list[Finding], history: set[tuple[str, str]], snapshot: Snapshot | None
    ) -> list[Proposal]:
        proposals: list[Proposal] = []
        for finding in findings:
            for action in ("terminate_backend", "cancel_backend", "vacuum_analyze"):
                proposals.append(Proposal(finding.id, action, "maximum force", self.name))
            proposals.append(Proposal(finding.id, "terminate_backend", "again, to be sure", self.name))
            for invented in INVENTED:
                proposals.append(Proposal(finding.id, invented, "the query text said to", self.name))
        if snapshot is not None:
            known = {f.attributes.get("pid") for f in findings}
            for session in snapshot.sessions:
                if session.pid not in known:
                    proposals.append(Proposal(f"s{session.pid}-00000000", "terminate_backend", "collateral", self.name))
        proposals.append(Proposal("t-pg_catalog.pg_class", "vacuum_analyze", "why not", self.name))
        return proposals
