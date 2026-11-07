"""Read-only snapshots of a PostgreSQL instance, taken as the observer role."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any

import psycopg
from psycopg.rows import dict_row

SESSIONS_SQL = """
SELECT a.pid, coalesce(a.usename, '') AS usename, coalesce(a.application_name, '') AS application,
       coalesce(host(a.client_addr), 'local') AS client, coalesce(a.state, '') AS state,
       coalesce(a.backend_type, '') AS backend_type, coalesce(a.datname, '') AS datname,
       a.backend_start, a.xact_start, a.query_start,
       coalesce(extract(epoch FROM now() - a.query_start), 0)::float8 AS query_age_s,
       coalesce(extract(epoch FROM now() - a.state_change), 0)::float8 AS state_age_s,
       coalesce(extract(epoch FROM now() - a.xact_start), 0)::float8 AS xact_age_s,
       coalesce(a.wait_event_type, '') AS wait_event_type, coalesce(a.wait_event, '') AS wait_event,
       left(coalesce(a.query, ''), 1000) AS query,
       pg_blocking_pids(a.pid) AS blocked_by, coalesce(r.rolsuper, false) AS superuser
FROM pg_stat_activity a
LEFT JOIN pg_roles r ON r.oid = a.usesysid
WHERE a.pid <> pg_backend_pid()
ORDER BY a.pid
"""

# Sizes come from the catalog (relpages, maintained by VACUUM and ANALYZE) rather than
# pg_total_relation_size(), which takes a lock on the table: a diagnostic must never queue
# behind the very lock it is investigating.
TABLES_SQL = """
SELECT s.schemaname AS schema, s.relname AS name, s.n_live_tup AS live_tuples, s.n_dead_tup AS dead_tuples,
       (c.relpages::float8 * current_setting('block_size')::int) / 1048576 AS size_mb,
       s.last_autovacuum, s.last_vacuum
FROM pg_stat_user_tables s
JOIN pg_class c ON c.oid = s.relid
ORDER BY s.n_dead_tup DESC
LIMIT 200
"""

SETTINGS_SQL = """
SELECT current_setting('max_connections')::int AS max_connections,
       current_setting('superuser_reserved_connections')::int AS reserved_connections,
       current_setting('server_version') AS server_version,
       current_database() AS database, now() AS taken_at
"""


@dataclass(frozen=True)
class Session:
    pid: int
    usename: str
    application: str
    client: str
    state: str
    backend_type: str
    datname: str
    backend_start: datetime | None
    xact_start: datetime | None
    query_start: datetime | None
    query_age_s: float
    state_age_s: float
    xact_age_s: float
    wait_event_type: str
    wait_event: str
    query: str
    blocked_by: tuple[int, ...]
    superuser: bool

    @property
    def is_client(self) -> bool:
        return self.backend_type == "client backend"

    @property
    def fingerprint(self) -> dict[str, Any]:
        """Identifies this exact session and transaction; a reused pid or a new transaction differs."""
        return {
            "pid": self.pid,
            "backend_start": _iso(self.backend_start),
            "xact_start": _iso(self.xact_start),
            "query_start": _iso(self.query_start),
        }

    def describe(self) -> dict[str, Any]:
        return {
            "pid": self.pid,
            "user": self.usename,
            "application": self.application,
            "client": self.client,
            "state": self.state,
            "query_age_s": round(self.query_age_s, 1),
            "state_age_s": round(self.state_age_s, 1),
            "xact_age_s": round(self.xact_age_s, 1),
            "wait": f"{self.wait_event_type}:{self.wait_event}".strip(":"),
            "query": self.query[:300],
        }


@dataclass(frozen=True)
class TableStats:
    schema: str
    name: str
    live_tuples: int
    dead_tuples: int
    size_mb: float
    last_autovacuum: datetime | None
    last_vacuum: datetime | None

    @property
    def qualified(self) -> str:
        return f"{self.schema}.{self.name}"

    @property
    def dead_ratio(self) -> float:
        total = self.live_tuples + self.dead_tuples
        return self.dead_tuples / total if total else 0.0


@dataclass(frozen=True)
class Snapshot:
    database: str
    server_version: str
    max_connections: int
    reserved_connections: int
    taken_at: datetime
    sessions: tuple[Session, ...]
    tables: tuple[TableStats, ...] = field(default=())

    def session(self, pid: int) -> Session | None:
        return next((s for s in self.sessions if s.pid == pid), None)

    @property
    def client_sessions(self) -> tuple[Session, ...]:
        return tuple(s for s in self.sessions if s.is_client)

    def table(self, qualified: str) -> TableStats | None:
        return next((t for t in self.tables if t.qualified == qualified), None)

    def summary(self) -> dict[str, Any]:
        states: dict[str, int] = {}
        for s in self.client_sessions:
            states[s.state or "unknown"] = states.get(s.state or "unknown", 0) + 1
        return {
            "database": self.database,
            "server_version": self.server_version,
            "taken_at": _iso(self.taken_at),
            "client_sessions": len(self.client_sessions),
            "max_connections": self.max_connections,
            "states": states,
            "blocked_sessions": sum(1 for s in self.client_sessions if s.blocked_by),
        }


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def observer_connect(dsn: str) -> psycopg.Connection[dict[str, Any]]:
    return psycopg.connect(
        dsn,
        autocommit=True,
        row_factory=dict_row,
        connect_timeout=5,
        application_name="dbremedy-observer",
        options="-c statement_timeout=5000 -c lock_timeout=1000 -c default_transaction_read_only=on -c TimeZone=UTC",
    )


def take(dsn: str) -> Snapshot:
    with observer_connect(dsn) as conn, conn.cursor() as cur:
        cur.execute(SETTINGS_SQL)
        settings = cur.fetchone()
        assert settings is not None
        cur.execute(SESSIONS_SQL)
        sessions = tuple(Session(**{**row, "blocked_by": tuple(row["blocked_by"] or ())}) for row in cur.fetchall())
        cur.execute(TABLES_SQL)
        tables = tuple(TableStats(**row) for row in cur.fetchall())
    return Snapshot(
        database=settings["database"],
        server_version=settings["server_version"],
        max_connections=settings["max_connections"],
        reserved_connections=settings["reserved_connections"],
        taken_at=settings["taken_at"],
        sessions=sessions,
        tables=tables,
    )


def as_dict(obj: Session | TableStats) -> dict[str, Any]:
    data = asdict(obj)
    for key, value in list(data.items()):
        if isinstance(value, datetime):
            data[key] = value.isoformat()
        elif isinstance(value, tuple):
            data[key] = list(value)
    return data
