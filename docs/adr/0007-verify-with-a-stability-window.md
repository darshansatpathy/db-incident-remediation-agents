# ADR-0007: Verify with a stability window, re-diagnose on change, bound the attempts

**Status:** accepted

## Context

"The detector found nothing" right after an action is weak evidence. Waiters may not have
queued again yet, an application may be about to retry, and a cancel may have changed the
situation into one that needs a different step. Without limits, an agent that keeps fixing a
problem the application keeps re-creating becomes part of the outage.

## Decision

- Verification polls the detector and succeeds only once it has been quiet for the runbook's
  `stable_s` (10 seconds for lock contention in production).
- If, while waiting, a finding appears that another runbook step now applies to (for example,
  the cancelled session rolled back to a savepoint and is now idle in its outer transaction),
  verification ends early and the agent diagnoses again.
- `max_attempts` bounds the cycle; when it is used up the incident escalates with what remains.

## Consequences

- The `blocker-keeps-coming-back` scenario ends the re-appearing blocker three times, then
  escalates, instead of looping.
- Time to resolve includes the stability window. That is the honest number: it is when the fix
  was known to hold.
