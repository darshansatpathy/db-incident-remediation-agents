"""The action catalog. These are the only things the agent can do to a database.

Each session action is a single statement that re-checks the target's fingerprint (pid,
backend start, transaction start, statement start) at the moment it signals. If the session
has finished, moved on to a new transaction, or its pid was reused, nothing is signalled and
the result is ``stale``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import psycopg
from psycopg import sql

from dbremedy.detectors import Finding

SIGNAL_SQL = """
SELECT {function}(a.pid) AS signalled
FROM pg_stat_activity a
WHERE a.pid = %(pid)s
  AND a.backend_start = %(backend_start)s::timestamptz
  AND a.xact_start IS NOT DISTINCT FROM %(xact_start)s::timestamptz
  AND a.query_start IS NOT DISTINCT FROM %(query_start)s::timestamptz
"""

DESCRIPTIONS = {
    "cancel_backend": "Cancel the session's current statement (pg_cancel_backend); the session stays connected.",
    "terminate_backend": "End the session (pg_terminate_backend); its open transaction is rolled back.",
    "vacuum_analyze": "Run VACUUM (ANALYZE) on the table; no exclusive lock.",
}


@dataclass(frozen=True)
class ActionResult:
    status: str  # done | stale | failed | dry_run
    detail: str

    def to_dict(self) -> dict[str, Any]:
        return {"status": self.status, "detail": self.detail}


def operator_connect(dsn: str) -> psycopg.Connection[Any]:
    return psycopg.connect(
        dsn, autocommit=True, connect_timeout=5, application_name="dbremedy-operator", options="-c TimeZone=UTC"
    )


def execute(action: str, finding: Finding, operator_dsn: str, *, dry_run: bool = False) -> ActionResult:
    if action not in DESCRIPTIONS:
        return ActionResult("failed", f"unknown action {action!r}")
    if dry_run:
        return ActionResult("dry_run", f"would {action} on {finding.id}")
    try:
        if action in ("cancel_backend", "terminate_backend"):
            return _signal(action, finding, operator_dsn)
        return _vacuum(finding, operator_dsn)
    except psycopg.Error as exc:
        return ActionResult("failed", f"{type(exc).__name__}: {str(exc).strip()[:300]}")


def _signal(action: str, finding: Finding, dsn: str) -> ActionResult:
    target = finding.target
    if target.get("type") != "session":
        return ActionResult("failed", f"{action} needs a session target")
    function = "pg_cancel_backend" if action == "cancel_backend" else "pg_terminate_backend"
    query = sql.SQL(SIGNAL_SQL).format(function=sql.Identifier(function))
    with operator_connect(dsn) as conn, conn.cursor() as cur:
        cur.execute(
            query,
            {
                "pid": target["pid"],
                "backend_start": target["backend_start"],
                "xact_start": target["xact_start"],
                "query_start": target["query_start"],
            },
        )
        row = cur.fetchone()
    if row is None:
        return ActionResult("stale", f"pid {target['pid']} no longer matches the session that was diagnosed")
    if not row[0]:
        return ActionResult("failed", f"{function} returned false for pid {target['pid']}")
    return ActionResult("done", f"{function}({target['pid']})")


def _vacuum(finding: Finding, dsn: str) -> ActionResult:
    target = finding.target
    if target.get("type") != "table":
        return ActionResult("failed", "vacuum_analyze needs a table target")
    with operator_connect(dsn) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM pg_catalog.pg_tables WHERE schemaname = %s AND tablename = %s",
            (target["schema"], target["name"]),
        )
        if cur.fetchone() is None:
            return ActionResult("stale", f"table {target['schema']}.{target['name']} not found")
        cur.execute(
            sql.SQL("VACUUM (ANALYZE) {}.{}").format(sql.Identifier(target["schema"]), sql.Identifier(target["name"]))
        )
    return ActionResult("done", f"VACUUM (ANALYZE) {target['schema']}.{target['name']}")
