# Incident inc-c9b8bdf2c095: Sessions blocked on locks

| | |
|---|---|
| Alert | `PostgresBlockedSessions` (1 delivery) |
| Runbook | `lock-contention` 1.0.0 |
| Outcome | **resolved** |
| Opened | 2026-11-07T13:07:17.677458+00:00 |
| Closed | 2026-11-07T13:07:25.743970+00:00 |
| Time to resolved | 8.1s |
| Labels | alertname=PostgresBlockedSessions, datname=target, instance=eval |

## What was found

- pid 5397 (inventory-sync, active) blocks 2 session(s) for up to 3s
  - query: `SELECT pg_sleep(120)`

## What was done

| Action | Target | Step | Approved by | Result |
|---|---|---|---|---|
| `cancel_backend` | pid 5397 | cancel the statement holding the locks | auto | done: pg_cancel_backend(5397) |
| `terminate_backend` | pid 5397 | end abandoned transactions that hold the locks | auto | done: pg_terminate_backend(5397) |

## Timeline

- `13:07:17.677` **opened**: PostgresBlockedSessions opened an incident
- `13:07:17.686` **diagnosis**: attempt 1: blocking_locks found 1 finding(s)
- `13:07:17.687` **plan**: rule planner proposed 1 action(s): 1 allowed, 0 need approval, 0 denied
- `13:07:17.691` **action**: cancel_backend on s5397-d04381cd: done (pg_cancel_backend(5397))
- `13:07:20.707` **verify**: the situation changed and another runbook step now applies
- `13:07:20.714` **diagnosis**: attempt 2: blocking_locks found 1 finding(s)
- `13:07:20.715` **plan**: rule planner proposed 1 action(s): 1 allowed, 0 need approval, 0 denied
- `13:07:20.720` **action**: terminate_backend on s5397-2f296419: done (pg_terminate_backend(5397))
- `13:07:25.743` **verify**: verified: the detector finds nothing for 3s

## Follow-up

- Set idle_in_transaction_session_timeout for the application roles involved.
- Set lock_timeout in the application so waiters fail fast instead of piling up.

## Runbook guidance

Find the root of the lock chain, not the sessions waiting behind it. An abandoned
transaction (idle in transaction) is safe to end; its work was never committed. A running
statement is cancelled first: that aborts its transaction, which releases the locks. If the
application catches the error and rolls back to a savepoint instead, the outer transaction
keeps the locks it took earlier and goes idle; the first step then ends it once it has been
idle long enough.
