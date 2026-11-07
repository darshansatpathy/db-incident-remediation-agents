# Incident inc-bb04d7556e6c: Sessions blocked on locks

| | |
|---|---|
| Alert | `PostgresBlockedSessions` (1 delivery) |
| Runbook | `lock-contention` 1.0.0 |
| Outcome | **escalated** |
| Opened | 2026-11-07T13:07:28.290407+00:00 |
| Closed | 2026-11-07T13:07:28.301437+00:00 |
| Time to escalated | 0.0s |
| Labels | alertname=PostgresBlockedSessions, datname=target, instance=eval |

## What was found

- pid 5423 (billing-batch, idle in transaction) blocks 2 session(s) for up to 3s
  - query: `UPDATE shop.orders SET status = 'held', updated_at = now() WHERE id = $1 `

## What was done

No action was taken.

## What was refused

- protected: 1
  - `terminate_backend` on s5423-8572a14f: protected: application 'billing-batch' matches 'billing-*'

## Timeline

- `13:07:28.290` **opened**: PostgresBlockedSessions opened an incident
- `13:07:28.298` **diagnosis**: attempt 1: blocking_locks found 1 finding(s)
- `13:07:28.299` **plan**: rule planner proposed 1 action(s): 0 allowed, 0 need approval, 1 denied
- `13:07:28.300` **escalated**: escalated to dba-oncall: no action is allowed: protected x1

## Follow-up

- Escalated to **dba-oncall** #db-incidents.
- Set idle_in_transaction_session_timeout for the application roles involved.
- Set lock_timeout in the application so waiters fail fast instead of piling up.

## Runbook guidance

Find the root of the lock chain, not the sessions waiting behind it. An abandoned
transaction (idle in transaction) is safe to end; its work was never committed. A running
statement is cancelled first: that aborts its transaction, which releases the locks. If the
application catches the error and rolls back to a savepoint instead, the outer transaction
keeps the locks it took earlier and goes idle; the first step then ends it once it has been
idle long enough.
