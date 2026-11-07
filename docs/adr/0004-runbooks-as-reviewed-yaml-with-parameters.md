# ADR-0004: Runbooks are reviewed YAML with parameters

**Status:** accepted

## Context

Remediation logic tends to live in scripts only their author can read, or in a wiki page that
drifts from what the automation does. Thresholds also differ by environment: ten minutes idle in
transaction is an incident on an OLTP primary and normal on a reporting replica.

## Decision

A runbook is a YAML document validated by strict Pydantic models: alert names, a detector with
parameters, ordered steps (action, conditions, approval), verification, limits, escalation and
guidance. Numbers can reference `parameters` as `${name}`; a deployment overrides parameters
per runbook without touching the steps. Unknown fields, unknown detectors, actions that don't
fit the detector's findings, and two runbooks claiming one alert are load errors.

## Consequences

- What the agent may do for each alert is readable in one file and changes through review.
- The evaluation runs the production runbooks with second-scale parameters
  (`evals/overrides.yaml`), so it tests the steps that ship, not a copy.
- Detectors and actions remain code. Adding one is a reviewed change with a scenario.
