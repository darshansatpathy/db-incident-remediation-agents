# Threat model

The agent can end sessions on a production database. The threats are that it ends the wrong
ones, ends too many, acts without the approval it needed, or is steered by someone who controls
what it reads.

## Trust boundaries

| Boundary | Trusted | Untrusted |
|---|---|---|
| Alertmanager → webhook | an alert's name and labels choose a runbook | everything else in the payload; alerts only start a diagnosis, they never carry targets |
| Database → agent | the database's own catalog and statistics | query text, application names and anything else a client can set |
| Planner → governor | nothing | every proposal, from rules or a model |
| Gateway → API | the gateway that authenticated users and set roles | callers without the service token |

## Threats and controls

| Threat | Example | Control | Tested by |
|---|---|---|---|
| Ending the wrong session | killing the waiters instead of the blocker | detectors target root blockers; waiters are never findings | `test_blocking_finds_the_root_of_a_chain` |
| Acting on a session that changed | the query finished, or the pid was reused, before the signal | single-statement signal guarded by the full fingerprint | `target-finishes-during-approval`, `test_actions_refuse_stale_and_wrong_targets` |
| Ending protected work | billing batch, backups, replication | protected applications and users in the policy; never superusers or background workers; `pg_signal_backend` cannot signal superusers | `protected-blocker`, `superuser-blocker`, `test_operator_cannot_signal_superusers` |
| Too much at once | a bug or a model ending every session | runbook `max_actions`, policy caps per action, batch approval threshold counted per incident | `idle-transactions-over-cap`, `connection-leak-approved`, `test_batch_threshold_is_cumulative` |
| Kill loops | an application that re-creates the problem | bounded attempts, then escalation; duplicate actions refused | `blocker-keeps-coming-back` |
| Instructions in data | query text telling the agent to terminate everything | rule planner ignores text; a model sees it as delimited data and can only pick this runbook's actions for this incident's findings; the governor re-checks conditions | `injection-in-query-text` with both planners |
| A planner inventing actions or targets | `drop_table`, sessions never diagnosed, system catalogs | catalog of three actions; targets must be current findings | rogue planner: 1,191 refusals, 0 violations |
| Approval bypass | an action that needs a person runs automatically | approvals per step, freeze windows, batch threshold; the auditor checks every executed action independently | `runaway-etl-approved`, `change-freeze`, `connection-leak-denied`, `test_every_kind_of_violation_is_reported` |
| Double execution | a node re-run after an approval or a restart | ledger ids derived from incident, finding and action; checked before executing | `test_a_paused_incident_survives_a_restart` |
| The agent making the incident worse | diagnostics queued behind the lock | read-only observer, 5s statement timeout, 1s lock timeout, no locking functions | `active-blocker` (the bug it found: ADR-0003) |
| Forged alerts or roles | anyone posting to the webhook or asserting `approver` | bearer token for the webhook; service token for identity headers; unknown roles refused | `test_webhook_approval_and_report`, `test_manual_incidents_and_service_token` |
| Excess privilege | an operator role that can do anything | two roles: observer read-only, operator limited to signalling, reading stats and maintenance | `test_observer_is_read_only` |

## OWASP Top 10 for LLM applications (2025)

| Risk | Control |
|---|---|
| LLM01 Prompt injection | Findings are passed as delimited data; the function schema admits only this runbook's actions and this incident's findings; the governor re-evaluates the runbook conditions itself |
| LLM05 Improper output handling | Model output is parsed into proposals and nothing else; it never reaches SQL |
| LLM06 Excessive agency | Three catalog actions; caps; approvals; protected sessions; fingerprinted execution |
| LLM10 Unbounded consumption | One planner call per attempt; bounded attempts per incident |

## Residual risks

- On PostgreSQL 16 and older there is no `pg_maintain`; the operator is granted the demo
  schema's owner role to run VACUUM, which also allows DDL on those tables. Use 17+, or grant
  ownership only of the tables the table-bloat runbook may vacuum.
- `pg_signal_backend` can signal any non-superuser session in the cluster, not just one
  database. The governor and the protected lists are what narrow it.
- Application names are set by clients. A client can name itself `billing-batch` to be
  protected (it then escalates to a person); it cannot use a name to become a target.
