---
name: Bug report
about: A wrong diagnosis, a missed or wrong action, or a scenario that should have passed
labels: bug
---

**What happened** (the incident report from `GET /v1/incidents/{id}/report`, or `dbremedy eval` output)

**What you expected**

**PostgreSQL version and how sessions looked** (`dbremedy diagnose --json`, redacted)

**Version** (`pip show db-incident-remediation-agents`)

Anything that lets the agent act on a session or table the policy protects, or act without an approval it needed, is a security issue: please report it privately, see SECURITY.md.
