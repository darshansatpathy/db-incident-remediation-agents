# Writing a runbook

A runbook says what an alert means, how to find what is behind it, what may be done about it and
under which conditions, and how to tell it worked. It is reviewed like code.

```yaml
id: idle-in-transaction          # lowercase, dashes
version: 1.0.0
title: Transactions left open and idle
severity: warning                # critical | warning | info
alerts: [PostgresIdleInTransaction]   # Prometheus alert names; each alert maps to one runbook
detector:
  name: idle_in_transaction      # see the table below
  params: {min_state_age_s: "${max_idle_s}"}
parameters:
  max_idle_s: 600                # override per deployment with DBR_RUNBOOK_OVERRIDES
steps:
  - name: end transactions abandoned by their client
    action: terminate_backend    # cancel_backend | terminate_backend | vacuum_analyze
    when:
      state_in: [idle in transaction, idle in transaction (aborted)]
    approval: auto               # auto | required
verify: {settle_s: 1, interval_s: 2, timeout_s: 20, stable_s: 0}
limits: {max_actions: 5, max_attempts: 2}
escalation: {team: dba-oncall, channel: "#db-incidents"}
guidance: |
  Shown in the report: what a person should know.
recommendations:
  - Shown in the report as follow-ups.
```

## Detectors

| Detector | Parameters | Finding per |
|---|---|---|
| `blocking_locks` | `min_wait_s` | root blocker of sessions waiting at least that long |
| `long_running_queries` | `min_query_age_s` | active statement older than that, not waiting on a lock |
| `idle_in_transaction` | `min_state_age_s` | session idle inside a transaction that long |
| `connection_saturation` | `max_ratio`, `min_idle_s`, `min_application_share` | idle session of an application holding at least that share, when usage is over the ratio |
| `table_bloat` | `min_dead_ratio`, `min_dead_tuples` | table over both |

## Step conditions

All are optional and all must hold: `state_in`, `application_matches`,
`application_not_matches` (glob patterns), `min_query_age_s`, `min_state_age_s`,
`min_xact_age_s`, `min_blocked`, `min_dead_ratio`, `min_dead_tuples`, `max_size_mb`, and
`after: <action>` (only once that action has already been applied to the same session in this
incident). Steps are tried in order by the rule planner; the governor accepts a proposal if any
step for that action matches, preferring an `auto` one.

## Checklist

- Write the scenario first: an injector in `chaos.py` that creates the incident with real
  sessions, and an entry in `evals/scenarios.yaml` with the exact expected actions.
- Add a scenario where the agent must not act (protected, approval denied, or conditions not
  met) and check the outcome is an escalation.
- Add the alert to `deploy/prometheus/alerts.yml` and a `promtool` test for it.
- For `vacuum_analyze` on a new schema, grant the operator `USAGE` on it.
- Run `dbremedy eval --planner rule,rogue --check` on every PostgreSQL version you support.
