# ADR-0002: Signal sessions in one statement guarded by a fingerprint

**Status:** accepted

## Context

Between diagnosing a session and signalling it, time passes: seconds normally, minutes or hours
while an approval is pending. In that time the statement can finish, the session can start a new
transaction, or it can disconnect and its pid be given to a new backend. `SELECT
pg_terminate_backend(12345)` would then end something nobody diagnosed.

## Decision

A finding's target is a fingerprint: pid, `backend_start`, `xact_start` and `query_start`. Every
signal is one statement over `pg_stat_activity` that matches all four (null-safe) and calls the
signal function only for the matching row. No row means the result is `stale` and nothing
happens. The operator role has `pg_read_all_stats` so it can see these columns for other users'
sessions.

## Consequences

- The `target-finishes-during-approval` scenario approves a cancel after the query has finished;
  the cancel is a no-op and the incident resolves.
- A long-running transaction that issues a new statement while an approval is pending also
  becomes stale. The next attempt re-diagnoses it, so the approver sees the current statement.
