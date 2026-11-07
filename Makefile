.PHONY: install check lint test eval e2e rules serve demo

install:
	pip install -e ".[dev]"

lint:
	ruff check .
	ruff format --check .
	mypy

test:
	pytest --cov --cov-report=term-missing --cov-fail-under=90

check: lint test
	dbremedy runbooks

# Needs DBR_ADMIN_DSN, DBR_APP_DSN, DBR_TARGET_OBSERVER_DSN and DBR_TARGET_OPERATOR_DSN.
eval:
	dbremedy eval --planner rule,rogue --check --report eval-report.md

rules:
	promtool check rules deploy/prometheus/alerts.yml deploy/e2e/alerts-fast.yml
	cd deploy/prometheus && promtool test rules alerts.test.yml

# Needs prometheus, alertmanager and postgres_exporter on PATH, and DBR_ADMIN_DSN.
e2e:
	./scripts/e2e.sh

demo:
	dbremedy demo

serve:
	dbremedy serve --port 8080
