# DB Incident Remediation Agents

[![CI](https://github.com/darshansatpathy/db-incident-remediation-agents/actions/workflows/ci.yml/badge.svg)](https://github.com/darshansatpathy/db-incident-remediation-agents/actions/workflows/ci.yml)
[![CodeQL](https://github.com/darshansatpathy/db-incident-remediation-agents/actions/workflows/codeql.yml/badge.svg)](https://github.com/darshansatpathy/db-incident-remediation-agents/actions/workflows/codeql.yml)
![Python](https://img.shields.io/badge/python-3.11%20%7C%203.12%20%7C%203.13-blue)
![PostgreSQL](https://img.shields.io/badge/PostgreSQL-16%20%7C%2017-336791)
![Prometheus](https://img.shields.io/badge/Prometheus%20%E2%86%92%20Alertmanager-end--to--end%20in%20CI-e6522c)
![License](https://img.shields.io/badge/license-MIT-green)

**Agents that take a PostgreSQL alert from Prometheus, find the session or table behind it,
fix it from a reviewed runbook, and prove the fix held. Anything a runbook doesn't allow, or
the policy protects, goes to a person instead.**

A lock chain at 3 a.m. is almost always the same story: one abandoned transaction, dozens of
sessions queued behind it, and an on-call engineer who has to find it in `pg_stat_activity`
before checkout times out. This repository automates the safe part of that work and refuses
the rest.

```
PostgreSQL ─▶ postgres_exporter ─▶ Prometheus ─▶ Alertmanager ─▶ webhook ─▶ incident graph
                                                                              │
     diagnose (read-only role) ─▶ plan (rules or a model) ─▶ governor ─▶ approval? ─▶ execute
                                                                              │
                               verify, with a stability window ◀─────────────┘
                               │ fixed: report          │ changed: re-diagnose        │ out of attempts: escalate
```

- **Diagnostics run as a read-only role** (`pg_monitor`, `default_transaction_read_only`), with a
  statement timeout and a one-second lock timeout, so they never queue behind the incident they
  are investigating.
- **A planner only proposes.** The governor allows an action only if a runbook step permits it,
  the target was found by this incident's diagnosis, the step's conditions hold on fresh data,
  the session is not protected, and the incident's caps are not used up.
- **Every signal re-checks the session's fingerprint in the same statement** (pid, backend
  start, transaction start, statement start). A session that finished, started a new
  transaction, or whose pid was reused is never touched.
- **Approvals pause the LangGraph run** with an interrupt; it is checkpointed (memory, SQLite
  or PostgreSQL) and resumes after a restart.
- **The fix has to hold.** Verification polls the detector until it stays quiet for a
  stability window. If the database changed so that another runbook step now applies, the agent
  diagnoses again. Attempts are bounded, so an application that keeps re-creating the problem
  is escalated, not killed in a loop.
- **Every incident ends with a report**: what was found, what was done and by whose approval,
  what was refused and why, a timeline, and follow-ups. [Examples](docs/examples).

> The scenarios run against a real PostgreSQL with real client sessions: abandoned
> transactions, exclusive locks, leaked connections, dead tuples. Nothing is mocked except the
> applications that misbehave.

---

## Try it

```bash
docker compose up -d          # PostgreSQL, postgres_exporter, Prometheus, Alertmanager, the agent
docker compose run --rm dbremedy dbremedy chaos inject idle_blocker --hold 600
curl -s -H 'X-User-Roles: viewer' localhost:8080/v1/incidents | jq '.incidents[0]'
```

The compose stack uses production thresholds, so the lock alert fires after a minute. For
second-scale thresholds set `DBR_RUNBOOK_OVERRIDES=/usr/local/lib/python3.12/site-packages/dbremedy/evals/overrides.yaml`.

Against any PostgreSQL you can reach:

```bash
pip install -e ".[dev]"
export DBR_ADMIN_DSN=postgresql://postgres:...@localhost:5432/scratch   # a scratch database
dbremedy setup                 # least-privilege roles + a demo schema
export DBR_APP_DSN=postgresql://dbr_app:dbr-app-local@localhost:5432/scratch
export DBR_TARGET_OBSERVER_DSN=postgresql://dbr_observer:dbr-observer-local@localhost:5432/scratch
export DBR_TARGET_OPERATOR_DSN=postgresql://dbr_operator:dbr-operator-local@localhost:5432/scratch
dbremedy demo                  # five incidents end to end
dbremedy eval --check          # all twenty scenarios, both planners
dbremedy --fast diagnose       # what the detectors see right now
```

## Results

From `dbremedy eval` over 20 scenarios ([evals/scenarios.yaml](evals/scenarios.yaml)), each a
real incident created on PostgreSQL. Every scenario fixes the outcome, the exact actions, the
sessions that must survive, and that the demo data and server settings are unchanged. An
independent auditor then re-checks the ledger against the raw policy and runbook YAML without
using the governor's code. CI runs it on PostgreSQL 16 and 17.

| | Rule planner | Misbehaving planner |
|---|---|---|
| Scenarios passed | **20 / 20** | **20 / 20** |
| Audit violations | **0** | **0** |
| Actions executed | 77 | 77 (the same ones) |
| Proposals refused by the governor | 9 (protected sessions, caps, a duplicate) | **1,191** |
| Approvals requested | 5 | 5 |
| Time from alert to verified fix, mean / max | 3.5s / 8.1s | 3.5s / 8.1s |

The misbehaving planner proposes the most destructive action for every finding, then actions
that don't exist (`drop_table`, `alter_system`, `kill_all_sessions`), terminations of every
other session on the server including superusers and protected applications, vacuums of
system catalogs and repeats of everything. It ends up doing exactly what the rule planner
does, because none of those decisions are the planner's to make. Refusals by reason: unknown
action 572, not in the runbook 297, unknown target 154, duplicate 130, conditions not met 20,
cap reached 12, protected 6.

**Read the timings carefully.** The evaluation uses second-scale thresholds
([evals/overrides.yaml](evals/overrides.yaml)) so it runs in two minutes; the lock scenarios
include a three-second stability window. With the production runbooks the agent waits for the
thresholds a DBA would (a minute of lock waits, ten minutes idle in transaction) before it acts.

What the scenarios cover:

| Kind | Scenarios |
|---|---|
| Lock contention | abandoned transaction; long statement holding an exclusive lock; a client that rolls back to a savepoint after the cancel and keeps the lock; a job that re-takes the lock every time it is ended (escalated after 3 attempts) |
| Must not act | blocker from a protected application; blocker is a superuser; query text instructing the agent to terminate everything and drop a table; approval denied; dry-run |
| Runaway queries | dashboard query cancelled automatically; ETL query cancelled after approval; query that finishes while the approval is pending (the fingerprint check makes the late cancel a no-op) |
| Connections and transactions | four abandoned transactions; eight, with a cap of five (five ended, then escalated); 55 leaked connections, approval for the batch |
| Maintenance | dead tuples in a table with autovacuum off, `VACUUM (ANALYZE)` |
| Alert handling | five deliveries of one alert open one incident; change freeze turns every action into an approval; false alarm; alert with no runbook |

Full table: [docs/evaluation-report.md](docs/evaluation-report.md).

## What the tests found

Two of the design rules came from scenarios failing against a real database:

- **Diagnostics were blocked by the incident.** The first version measured table sizes with
  `pg_total_relation_size()`, which takes a lock on the table. During an exclusive-lock
  incident the diagnostic queued behind the blocker like every other session. Sizes now come
  from `pg_class.relpages`, and every diagnostic runs with `lock_timeout = 1s`.
  ([ADR-0003](docs/adr/0003-diagnostics-never-wait-on-locks.md))
- **A cancelled transaction does not keep its locks.** A runbook step assumed that cancelling
  a statement leaves its transaction "idle in transaction (aborted)" and still holding locks.
  PostgreSQL releases them when the transaction aborts; the step was removed. The case that does
  keep locks is an application that catches the error and rolls back to a savepoint, which is
  now its own scenario: cancel, then end the session once it sits idle with the outer
  transaction open.

## Runbooks

[runbooks/](runbooks) are reviewed YAML: which alert, which detector with which thresholds,
which actions are allowed under which conditions, and how to verify. Thresholds are parameters,
so a deployment (or the evaluation) can tune them without editing the steps.

| Runbook | Alert | Finds | May do |
|---|---|---|---|
| [lock-contention](runbooks/lock-contention.yaml) | `PostgresBlockedSessions` | root blockers of lock waits | end abandoned transactions; cancel a long statement; terminate a blocker that ignored the cancel (approval) |
| [runaway-query](runbooks/runaway-query.yaml) | `PostgresLongRunningQuery` | long statements not waiting on locks | cancel reporting and ad hoc queries; cancel other applications' queries (approval); terminate after an ignored cancel (approval) |
| [idle-in-transaction](runbooks/idle-in-transaction.yaml) | `PostgresIdleInTransaction` | transactions idle too long | end them, at most five per incident |
| [connection-saturation](runbooks/connection-saturation.yaml) | `PostgresConnectionSaturation` | idle sessions of the applications holding most connections | close them (a batch above ten needs approval) |
| [table-bloat](runbooks/table-bloat.yaml) | `PostgresTableBloat` | tables with many dead tuples | `VACUUM (ANALYZE)`; approval above 2 GB |

The action catalog is three actions: `cancel_backend`, `terminate_backend`, `vacuum_analyze`.
There is no DDL, no configuration change and no `VACUUM FULL`. How to write a runbook:
[docs/runbook-authoring.md](docs/runbook-authoring.md).

## Safety model

| Layer | What it enforces | Where |
|---|---|---|
| Database roles | Observer: `pg_monitor`, read-only, 5s statement timeout, 1s lock timeout. Operator: `pg_signal_backend` (cannot signal superusers), `pg_read_all_stats`, `pg_maintain` on 17+ | [bootstrap.py](src/dbremedy/bootstrap.py) |
| Runbook | Allowed actions and the conditions for each; approval per step; caps and attempts | [runbooks/](runbooks) |
| Policy | Protected applications and users; never superusers or background workers; hard caps; batch approval; change-freeze windows; dry-run | [policies/policy.yaml](policies/policy.yaml) |
| Governor | Re-checks every proposal against all of the above on fresh findings; stricter than the runbook, never looser | [governor.py](src/dbremedy/governor.py) |
| Execution | One statement per signal, guarded by the session fingerprint; action ledger with idempotent ids, so a re-run node never signals twice | [actions.py](src/dbremedy/actions.py), [graph.py](src/dbremedy/graph.py) |
| Verification | Stability window, re-diagnosis on change, bounded attempts, escalation | [graph.py](src/dbremedy/graph.py) |
| Audit | An independent check of the ledger in every scenario | [auditor.py](src/dbremedy/auditor.py) |

Threat model with the OWASP Top 10 for LLM applications: [docs/threat-model.md](docs/threat-model.md).

## Prometheus and Alertmanager

[deploy/prometheus](deploy/prometheus) has the exporter queries (read-only, lock-free, run as
the same observer role), one alert rule per runbook with `promtool` unit tests, and a
Prometheus config. [deploy/alertmanager](deploy/alertmanager) routes the alerts to the webhook
with a bearer token.

CI runs the whole chain on every push: it installs Prometheus, Alertmanager and
postgres_exporter, creates a real lock incident, and checks that the alert fires, Alertmanager
delivers it, the agent ends the blocker, and the report and metrics say so
([scripts/e2e.sh](scripts/e2e.sh)). Repeated deliveries attach to the open incident; a
"resolved" notification is recorded on the timeline but never stops a fix halfway.

Without Prometheus, `dbremedy watch` polls the detectors itself.

## HTTP API

| Endpoint | Role | Purpose |
|---|---|---|
| `POST /v1/alerts/alertmanager` | webhook token | Alertmanager webhook (v4); opens or attaches incidents, runs them in the background |
| `POST /v1/incidents` | operator | Open an incident by hand |
| `GET /v1/incidents`, `GET /v1/incidents/{id}` | viewer | Incidents; one with its timeline and action ledger |
| `GET /v1/incidents/{id}/report` | viewer | The report, as Markdown |
| `POST /v1/incidents/{id}/approval` | approver | Approve or deny the pending actions |
| `GET /v1/runbooks`, `GET /v1/diagnose` | viewer | Loaded runbooks; what every detector sees now |
| `GET /healthz`, `/readyz`, `/metrics` | none | Liveness; target reachable as the observer; Prometheus counters and a time-to-close histogram |

Identities come from the gateway in front of the service (`X-User-Id`, `X-User-Roles`); with
`DBR_SERVICE_TOKEN` set, only callers presenting it can assert them. After a restart, incidents
that were mid-run resume from their last checkpoint; paused ones keep waiting.

## Configuration

| Variable | Purpose |
|---|---|
| `DBR_TARGET_OBSERVER_DSN`, `DBR_TARGET_OPERATOR_DSN` | The two roles created by `dbremedy setup` |
| `DBR_STORE` | `memory`, `sqlite` (`DBR_SQLITE_PATH`) or `postgres` (`DBR_STORE_DSN`) for incidents, ledger and checkpoints |
| `DBR_PLANNER` | `rule`, or `llm` with `DBR_LLM_BASE_URL`, `DBR_LLM_MODEL`, `DBR_LLM_API_KEY` |
| `DBR_WEBHOOK_TOKEN`, `DBR_SERVICE_TOKEN` | Webhook and gateway authentication |
| `DBR_CHANGE_FREEZE`, `DBR_DRY_RUN` | Every action needs approval; record and do nothing |
| `DBR_RUNBOOKS`, `DBR_POLICY`, `DBR_RUNBOOK_OVERRIDES` | Your own runbooks, policy and thresholds |

All of them are in [.env.example](.env.example). A model, when configured, sees the findings
as data and answers through a function whose schema only admits this runbook's actions and
this incident's findings; the governor checks its answer like any other.

## Engineering

- 79 tests, 95% line coverage (gate: 90%), on a real PostgreSQL: detectors, governor, both
  stores and checkpointers, a paused incident surviving a restart, the API and the CLI.
- The evaluation gate runs both planners on PostgreSQL 16 and 17 on every push and uploads
  every scenario's incident report.
- `promtool test rules` for the alerts; `amtool check-config`; the end-to-end pipeline in CI.
- `ruff`, `mypy --strict`, Python 3.11 to 3.13; CodeQL (security-extended), pip-audit,
  Dependabot; a non-root container image published to GitHub Container Registry on tags.

## Layout

```
src/dbremedy/
  snapshot.py      read-only snapshots of pg_stat_activity, locks and table stats
  detectors.py     findings: root blockers, runaway queries, idle transactions, saturation, bloat
  runbooks.py      runbook models, parameters, step conditions
  policy.py        protections, caps, freeze windows
  governor.py      the decision on every proposed action
  actions.py       the catalog: fingerprint-guarded cancel/terminate, VACUUM (ANALYZE)
  graph.py         the LangGraph incident workflow
  engine.py        alerts in, runs, approvals, recovery
  planners/        rule (reference), llm (function calling), rogue (guardrail test)
  chaos.py         real incidents on a real database
  evaluation.py    scenarios, checks and the gate
  auditor.py       independent re-check of the ledger
  store.py, report.py, alerts.py, api.py, cli.py, config.py, bootstrap.py
runbooks/  policies/  evals/
deploy/prometheus/  deploy/alertmanager/  deploy/e2e/
docs/              architecture, threat model, runbook authoring, ADRs, example reports
```

## Known limits

- One target database per deployment. Several instances need one agent each, or a mapping
  from alert labels to DSNs.
- Five incident types. Replication lag, transaction ID wraparound, disk and WAL growth, and
  bad query plans are diagnosed by people today; the detectors and runbooks are the place to
  add them, each with a scenario.
- The agent does not change configuration or schema. Its reports recommend
  `idle_in_transaction_session_timeout`, `lock_timeout`, pool sizes or autovacuum settings;
  applying them stays a reviewed change.
- An approval waits until someone decides. Approval timeouts and paging belong to the alert
  routing in front of the agent.

## License

MIT. See [LICENSE](LICENSE).
