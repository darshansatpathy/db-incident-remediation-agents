#!/usr/bin/env bash
# End to end: a real lock incident on PostgreSQL is scraped by postgres_exporter, turned into an
# alert by Prometheus, routed by Alertmanager to the agent's webhook, and fixed by the agent.
#
# Needs: prometheus, alertmanager and postgres_exporter on PATH, and
#   DBR_ADMIN_DSN  a superuser DSN for the target database (to create roles and run the injector)
# Run from the repository root after `pip install -e .`.
set -euo pipefail

: "${DBR_ADMIN_DSN:?set DBR_ADMIN_DSN}"
HOST_PORT_DB=${DBR_ADMIN_DSN#*@}
WORK=$(mktemp -d)
LOGS=${E2E_LOGS:-$WORK}
pids=()
cleanup() {
  for pid in "${pids[@]}"; do kill "$pid" 2>/dev/null || true; done
  wait 2>/dev/null || true
}
trap cleanup EXIT

dbremedy setup --admin-dsn "$DBR_ADMIN_DSN" --observer-password e2e-observer --operator-password e2e-operator \
  --app-password e2e-app
export DBR_TARGET_OBSERVER_DSN="postgresql://dbr_observer:e2e-observer@${HOST_PORT_DB}"
export DBR_TARGET_OPERATOR_DSN="postgresql://dbr_operator:e2e-operator@${HOST_PORT_DB}"
export DBR_APP_DSN="postgresql://dbr_app:e2e-app@${HOST_PORT_DB}"
export DBR_RUNBOOK_OVERRIDES="$PWD/evals/overrides.yaml"
export DBR_WEBHOOK_TOKEN="e2e-webhook-token"
printf '%s' "$DBR_WEBHOOK_TOKEN" > "$WORK/webhook-token"
sed "s#WEBHOOK_TOKEN_FILE#$WORK/webhook-token#" deploy/e2e/alertmanager.yml > "$WORK/alertmanager.yml"
cp deploy/e2e/prometheus.yml deploy/e2e/alerts-fast.yml "$WORK/"

DATA_SOURCE_NAME="$DBR_TARGET_OBSERVER_DSN?sslmode=disable" postgres_exporter \
  --extend.query-path=deploy/prometheus/queries.yaml --web.listen-address=127.0.0.1:9187 \
  >"$LOGS/exporter.log" 2>&1 &
pids+=($!)
dbremedy serve --host 127.0.0.1 --port 8080 >"$LOGS/dbremedy.log" 2>&1 &
pids+=($!)
alertmanager --config.file="$WORK/alertmanager.yml" --storage.path="$WORK/am" \
  --web.listen-address=127.0.0.1:9093 --cluster.listen-address= >"$LOGS/alertmanager.log" 2>&1 &
pids+=($!)
prometheus --config.file="$WORK/prometheus.yml" --storage.tsdb.path="$WORK/prom" \
  --web.listen-address=127.0.0.1:9090 >"$LOGS/prometheus.log" 2>&1 &
pids+=($!)

wait_for() {  # url, seconds
  for _ in $(seq 1 "$2"); do curl -fsS "$1" >/dev/null 2>&1 && return 0; sleep 1; done
  echo "timed out waiting for $1" >&2; return 1
}
wait_for http://127.0.0.1:8080/readyz 30
wait_for http://127.0.0.1:9187/metrics 30
wait_for http://127.0.0.1:9093/-/ready 30
wait_for http://127.0.0.1:9090/-/ready 30
curl -fsS http://127.0.0.1:9187/metrics > "$WORK/exporter-metrics.txt"
grep -q '^dbr_sessions_blocked' "$WORK/exporter-metrics.txt" || { echo "exporter has no dbr_ metrics" >&2; exit 1; }
echo "pipeline up: exporter, prometheus, alertmanager, agent"

started=$(date +%s)
dbremedy chaos inject idle_blocker --admin-dsn "$DBR_ADMIN_DSN" --app-dsn "$DBR_APP_DSN" --hold 120 \
  --until-resolved >"$LOGS/chaos.log" 2>&1 &
chaos=$!

viewer=(-H "X-User-Id: e2e" -H "X-User-Roles: viewer")
incident=""
for _ in $(seq 1 120); do
  incident=$(curl -fsS "${viewer[@]}" "http://127.0.0.1:8080/v1/incidents?status=resolved" \
    | python3 -c "import json,sys; r=[i['id'] for i in json.load(sys.stdin)['incidents'] if i['alertname']=='PostgresBlockedSessions']; print(r[0] if r else '')")
  [ -n "$incident" ] && break
  sleep 1
done
if [ -z "$incident" ]; then
  echo "no resolved incident after 120s" >&2
  tail -n 50 "$LOGS"/*.log >&2
  exit 1
fi
wait "$chaos" || true
elapsed=$(( $(date +%s) - started ))

curl -fsS "${viewer[@]}" "http://127.0.0.1:8080/v1/incidents/$incident/report" > "$WORK/report.md"
cat "$WORK/report.md"
grep -q 'terminate_backend' "$WORK/report.md"
grep -q 'resolved' "$WORK/report.md"
grep -q 'Labels | .*instance=e2e-primary' "$WORK/report.md"
curl -fsS http://127.0.0.1:8080/metrics > "$WORK/agent-metrics.txt"
grep -q 'dbremedy_actions_total{action="terminate_backend",status="done"} 1' "$WORK/agent-metrics.txt"
grep -q 'no session is waiting on a lock any more' "$LOGS/chaos.log"

echo
echo "End to end: lock injected, Prometheus alerted, Alertmanager delivered, the agent resolved it in ${elapsed}s."
if [ -n "${GITHUB_STEP_SUMMARY:-}" ]; then
  {
    echo "## End to end: Prometheus → Alertmanager → agent"
    echo
    echo "A real lock incident was resolved ${elapsed}s after injection. The agent's report:"
    echo
    sed 's/^#/###/' "$WORK/report.md"
  } >> "$GITHUB_STEP_SUMMARY"
fi
