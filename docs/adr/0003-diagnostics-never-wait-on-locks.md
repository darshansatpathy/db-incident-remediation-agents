# ADR-0003: Diagnostics never wait on locks

**Status:** accepted

## Context

The first run of the exclusive-lock scenario hung: the snapshot's table statistics query used
`pg_total_relation_size()`, which acquires an `AccessShareLock` on each table. The incident held
an `AccessExclusiveLock` on one of them, so the agent's own diagnostic joined the queue it was
meant to clear, and failed on its statement timeout.

## Decision

- Diagnostics read only views and catalogs that need no relation locks. Table sizes come from
  `pg_class.relpages * block_size`, which is maintained by VACUUM and ANALYZE and is accurate
  enough for a size threshold.
- The observer connection sets `lock_timeout = 1s` in addition to `statement_timeout = 5s`, so a
  future query that does need a lock fails fast instead of piling up.
- The exporter's custom queries follow the same rule.

## Consequences

- The size condition in the table-bloat runbook uses an estimate. That is fine for choosing
  between automatic and approved vacuums; it would not be fine for capacity planning.
- A new detector query must be checked for locking functions in review.
