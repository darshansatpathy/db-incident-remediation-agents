# ADR-0001: Planners propose; a governor decides

**Status:** accepted

## Context

Automating remediation with an agent invites the question every DBA asks first: what stops it
from killing the wrong session? Prompting a model to be careful is not an answer, and neither
is a rule engine nobody can test against bad inputs.

## Decision

Separate deciding what to do from deciding whether it may be done. Planners (a rule planner,
an optional model) return proposals: finding, action, reason. A deterministic governor checks
each proposal against the action catalog, the runbook's steps and their conditions on freshly
diagnosed data, the policy's protections and caps, and the incident's history in the ledger.
It can require approval where the runbook doesn't, never the reverse.

## Consequences

- The safety properties are properties of the governor and can be tested by feeding it the worst
  proposals we can think of: the rogue planner makes 1,191 bad proposals across the scenarios and
  causes no violation.
- A model can be swapped in without changing what the system is allowed to do.
- Runbook conditions are evaluated twice, by the rule planner and by the governor. That is
  deliberate; only the governor's evaluation counts.
