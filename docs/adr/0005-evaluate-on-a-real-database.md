# ADR-0005: Evaluate on a real PostgreSQL, with an independent auditor

**Status:** accepted

## Context

Mocking `pg_stat_activity` would test that the code does what we think PostgreSQL does. Two
of our beliefs about PostgreSQL were wrong (ADR-0003, and that an aborted transaction keeps its
locks), and only a real database showed it.

## Decision

Every scenario creates its incident with real client sessions on a real PostgreSQL, delivers an
Alertmanager-shaped alert, runs the full engine, and checks the outcome, the exact actions,
the sessions that had to survive, and that the demo data and `fsync` are untouched. An auditor,
written without the governor's code, re-checks the ledger against the raw policy and runbook
YAML. CI runs this on PostgreSQL 16 and 17, for the rule planner and the rogue planner, and
runs the Prometheus and Alertmanager chain end to end.

## Consequences

- The evaluation needs a database and takes about two minutes per planner.
- Timing matters: thresholds are seconds, so scenarios wait a few seconds before alerting, and
  verification uses a short stability window. Scenarios were run repeatedly to check they are
  stable.
- The auditor's own checks are tested with a ledger that breaks every rule.
