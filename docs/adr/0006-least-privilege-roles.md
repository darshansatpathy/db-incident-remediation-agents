# ADR-0006: Two least-privilege roles, and the database as a second check

**Status:** accepted

## Context

Automation often runs as a superuser because it is convenient. A superuser agent with a bug can
do anything; an agent that can only read is no use in an incident.

## Decision

`dbremedy setup` creates:

- `dbr_observer`: `pg_monitor`, `default_transaction_read_only = on`, `statement_timeout = 5s`.
  Every diagnostic, and the exporter, uses it.
- `dbr_operator`: `pg_signal_backend`, `pg_read_all_stats`, `statement_timeout = 120s`,
  `lock_timeout = 5s`, and `pg_maintain` on PostgreSQL 17+ (the owner role of the demo schema on
  older versions), plus `USAGE` on each schema whose tables it may vacuum: `pg_maintain` grants
  the right to vacuum, not the right to look the table up by name.

`pg_signal_backend` cannot signal superuser backends, so "never touch superusers" is enforced by
the governor and again by PostgreSQL.

## Consequences

- A bypassed governor still cannot end a superuser session, change data, or run DDL on 17+.
- `pg_signal_backend` is cluster-wide; the protected lists in the policy narrow it further.
- On 16 and older, VACUUM needs ownership, which brings DDL rights on those tables (see the
  threat model's residual risks).
