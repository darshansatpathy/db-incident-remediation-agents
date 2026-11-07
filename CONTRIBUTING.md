# Contributing

## Set up

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
export DBR_TEST_ADMIN_DSN=postgresql://postgres:...@localhost:5432/scratch   # a scratch database
make check
```

Without `DBR_TEST_ADMIN_DSN` the PostgreSQL tests are skipped; CI runs them all.

## Running the scenarios

```bash
export DBR_ADMIN_DSN=$DBR_TEST_ADMIN_DSN
dbremedy setup
export DBR_APP_DSN=postgresql://dbr_app:dbr-app-local@localhost:5432/scratch
export DBR_TARGET_OBSERVER_DSN=postgresql://dbr_observer:dbr-observer-local@localhost:5432/scratch
export DBR_TARGET_OPERATOR_DSN=postgresql://dbr_operator:dbr-operator-local@localhost:5432/scratch
make eval
```

The scenarios open real sessions and end them. Use a database nothing else depends on.

## Adding an incident type

Follow [docs/runbook-authoring.md](docs/runbook-authoring.md): write the injector and the
scenario first, then the detector and the runbook, then a scenario where the agent must not
act, then the alert rule and its promtool test.

## Adding an action

New actions widen what the agent can do to a database. They need: a single-statement
implementation that re-checks its target, an entry in the governor's catalog, auditor support,
a threat model update, and an ADR.

## Commits

Small commits whose message says what changed and why. Update `CHANGELOG.md` under an
"Unreleased" heading.
