# Changelog

All notable changes are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/).

## [0.1.0] - 2026-11-07

### Added

- Read-only snapshots and five detectors: lock contention (root blockers), runaway queries,
  idle transactions, connection saturation, table bloat.
- Runbooks as validated YAML with parameters and per-deployment overrides; five runbooks.
- Operating policy: protected applications and users, caps, batch approval, change freezes,
  dry-run.
- Governor reviewing every proposal; rule, model (OpenAI-compatible) and rogue planners.
- Fingerprint-guarded cancel and terminate, and `VACUUM (ANALYZE)`, as least-privilege roles.
- LangGraph incident workflow with approvals as interrupts, verification with a stability
  window, bounded attempts, escalation and a Markdown report per incident.
- Store and checkpoints in memory, SQLite or PostgreSQL; recovery after a restart.
- Alertmanager webhook, HTTP API with roles, Prometheus metrics, `dbremedy` CLI with `setup`,
  `diagnose`, `chaos`, `eval`, `demo`, `watch` and `serve`.
- postgres_exporter queries, alert rules with promtool tests, Alertmanager routing, and an
  end-to-end pipeline test.
- Twenty scenarios on a real PostgreSQL with an independent auditor, run in CI on
  PostgreSQL 16 and 17 for both the rule and the rogue planner.
