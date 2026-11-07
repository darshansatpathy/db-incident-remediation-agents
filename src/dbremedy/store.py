"""Incidents, their timeline and the action ledger, in SQLite or PostgreSQL.

An incident is keyed by its alert identity (alert name plus the labels that locate it).
While one is open, repeated deliveries of the same alert attach to it instead of opening
another; a unique ``open_key`` column enforces that even under concurrent deliveries.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any

OPEN_STATUSES = ("open", "running", "awaiting_approval")

SCHEMA = [
    """CREATE TABLE IF NOT EXISTS dbr_incidents (
        id TEXT PRIMARY KEY, key TEXT NOT NULL, open_key TEXT UNIQUE, alertname TEXT NOT NULL,
        runbook TEXT, status TEXT NOT NULL, severity TEXT NOT NULL, labels TEXT NOT NULL, summary TEXT NOT NULL,
        alerts INTEGER NOT NULL, opened_at TEXT NOT NULL, updated_at TEXT NOT NULL, resolved_at TEXT,
        mttr_s REAL, outcome TEXT, report TEXT, pending TEXT)""",
    """CREATE TABLE IF NOT EXISTS dbr_events (
        incident_id TEXT NOT NULL, seq INTEGER NOT NULL, at TEXT NOT NULL, kind TEXT NOT NULL,
        message TEXT NOT NULL, data TEXT NOT NULL, PRIMARY KEY (incident_id, seq))""",
    """CREATE TABLE IF NOT EXISTS dbr_actions (
        id TEXT PRIMARY KEY, incident_id TEXT NOT NULL, finding_id TEXT NOT NULL, action TEXT NOT NULL,
        session_key TEXT, target TEXT NOT NULL, verdict TEXT NOT NULL, reasons TEXT NOT NULL, step TEXT,
        approved_by TEXT, status TEXT NOT NULL, detail TEXT NOT NULL, at TEXT NOT NULL)""",
]

INCIDENT_COLUMNS = (
    "id, key, open_key, alertname, runbook, status, severity, labels, summary, alerts, opened_at, updated_at, "
    "resolved_at, mttr_s, outcome, report, pending"
)
UPDATABLE = {"runbook", "status", "summary", "resolved_at", "mttr_s", "outcome", "report", "pending", "open_key"}


def now_iso() -> str:
    return datetime.now(UTC).isoformat()


class Store:
    """Thread-safe store. ``paramstyle`` is ``?`` for SQLite and ``%s`` for PostgreSQL."""

    def __init__(self, connect: Any, paramstyle: str, *, unique_violation: tuple[type[Exception], ...]) -> None:
        self._connect = connect
        self._paramstyle = paramstyle
        self._unique = unique_violation
        self._lock = threading.RLock()
        self._conn = connect()
        with self._tx() as cur:
            for statement in SCHEMA:
                cur.execute(statement)

    @classmethod
    def sqlite(cls, path: str = ":memory:") -> Store:
        def connect() -> sqlite3.Connection:
            conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
            if path != ":memory:":
                conn.execute("PRAGMA journal_mode=WAL")
            return conn

        return cls(connect, "?", unique_violation=(sqlite3.IntegrityError,))

    @classmethod
    def postgres(cls, dsn: str) -> Store:
        import psycopg

        def connect() -> Any:
            return psycopg.connect(dsn, autocommit=True, application_name="dbremedy-store")

        return cls(connect, "%s", unique_violation=(psycopg.errors.UniqueViolation,))

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    @contextmanager
    def _tx(self) -> Iterator[Any]:
        with self._lock:
            cur = self._conn.cursor()
            cur.execute("BEGIN")
            try:
                yield _Cursor(cur, self._paramstyle)
            except BaseException:
                cur.execute("ROLLBACK")
                raise
            cur.execute("COMMIT")

    # incidents

    def open_or_attach(
        self, key: str, alertname: str, runbook: str | None, severity: str, labels: dict[str, str], summary: str
    ) -> tuple[dict[str, Any], bool]:
        stamp = now_iso()
        incident_id = "inc-" + uuid.uuid4().hex[:12]
        try:
            with self._tx() as cur:
                cur.execute(
                    f"INSERT INTO dbr_incidents ({INCIDENT_COLUMNS}) VALUES "  # noqa: S608 - fixed column list
                    "(?, ?, ?, ?, ?, 'open', ?, ?, ?, 1, ?, ?, NULL, NULL, NULL, NULL, NULL)",
                    (
                        incident_id,
                        key,
                        key,
                        alertname,
                        runbook,
                        severity,
                        json.dumps(labels, sort_keys=True),
                        summary,
                        stamp,
                        stamp,
                    ),
                )
            self.event(incident_id, "opened", f"{alertname} opened an incident", {"labels": labels})
            return self.get(incident_id), True
        except self._unique:
            with self._tx() as cur:
                cur.execute(
                    "UPDATE dbr_incidents SET alerts = alerts + 1, updated_at = ? WHERE open_key = ?", (stamp, key)
                )
                cur.execute("SELECT id FROM dbr_incidents WHERE open_key = ?", (key,))
                row = cur.fetchone()
            if row is None:  # closed between the insert and the update: open a fresh one
                return self.open_or_attach(key, alertname, runbook, severity, labels, summary)
            self.event(row[0], "alert", f"{alertname} delivered again; attached to the open incident", {})
            return self.get(row[0]), False

    def update(self, incident_id: str, **fields: Any) -> None:
        unknown = set(fields) - UPDATABLE
        if unknown:
            raise ValueError(f"cannot update {sorted(unknown)}")
        if not fields:
            return
        assignments = ", ".join(f"{name} = ?" for name in fields)
        values = [json.dumps(v) if name == "pending" and v is not None else v for name, v in fields.items()]
        with self._tx() as cur:
            cur.execute(
                f"UPDATE dbr_incidents SET {assignments}, updated_at = ? WHERE id = ?",  # noqa: S608 - names whitelisted
                (*values, now_iso(), incident_id),
            )

    def close_incident(self, incident_id: str, status: str, outcome: str, mttr_s: float | None) -> None:
        self.update(
            incident_id,
            status=status,
            outcome=outcome,
            resolved_at=now_iso(),
            mttr_s=mttr_s,
            open_key=None,
            pending=None,
        )

    def get(self, incident_id: str) -> dict[str, Any]:
        with self._tx() as cur:
            cur.execute(f"SELECT {INCIDENT_COLUMNS} FROM dbr_incidents WHERE id = ?", (incident_id,))  # noqa: S608
            row = cur.fetchone()
        if row is None:
            raise KeyError(incident_id)
        return self._incident(row)

    def incidents(self, limit: int = 50, status: str | None = None) -> list[dict[str, Any]]:
        query = f"SELECT {INCIDENT_COLUMNS} FROM dbr_incidents"  # noqa: S608
        params: tuple[Any, ...] = ()
        if status:
            query += " WHERE status = ?"
            params = (status,)
        query += " ORDER BY opened_at DESC LIMIT ?"
        with self._tx() as cur:
            cur.execute(query, (*params, limit))
            rows = cur.fetchall()
        return [self._incident(r) for r in rows]

    @staticmethod
    def _incident(row: tuple[Any, ...]) -> dict[str, Any]:
        names = [c.strip() for c in INCIDENT_COLUMNS.split(",")]
        data = dict(zip(names, row, strict=True))
        data["labels"] = json.loads(data["labels"])
        data["pending"] = json.loads(data["pending"]) if data["pending"] else None
        return data

    # timeline

    def event(self, incident_id: str, kind: str, message: str, data: dict[str, Any] | None = None) -> None:
        with self._tx() as cur:
            cur.execute("SELECT coalesce(max(seq), 0) + 1 FROM dbr_events WHERE incident_id = ?", (incident_id,))
            row = cur.fetchone()
            seq = int(row[0]) if row else 1
            cur.execute(
                "INSERT INTO dbr_events (incident_id, seq, at, kind, message, data) VALUES (?, ?, ?, ?, ?, ?)",
                (incident_id, seq, now_iso(), kind, message, json.dumps(data or {}, default=str)),
            )

    def events(self, incident_id: str) -> list[dict[str, Any]]:
        with self._tx() as cur:
            cur.execute(
                "SELECT seq, at, kind, message, data FROM dbr_events WHERE incident_id = ? ORDER BY seq", (incident_id,)
            )
            rows = cur.fetchall()
        return [{"seq": r[0], "at": r[1], "kind": r[2], "message": r[3], "data": json.loads(r[4])} for r in rows]

    # ledger

    def action_recorded(self, action_id: str) -> dict[str, Any] | None:
        rows = [a for a in self._actions("id = ?", (action_id,))]
        return rows[0] if rows else None

    def record_action(self, record: dict[str, Any]) -> bool:
        """Insert one ledger row; False if this action id was already recorded."""
        try:
            with self._tx() as cur:
                cur.execute(
                    "INSERT INTO dbr_actions (id, incident_id, finding_id, action, session_key, target, verdict, "
                    "reasons, step, approved_by, status, detail, at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        record["id"],
                        record["incident_id"],
                        record["finding_id"],
                        record["action"],
                        record.get("session_key"),
                        json.dumps(record.get("target")),
                        record["verdict"],
                        json.dumps(record.get("reasons", [])),
                        record.get("step"),
                        record.get("approved_by"),
                        record["status"],
                        record.get("detail", ""),
                        now_iso(),
                    ),
                )
            return True
        except self._unique:
            return False

    def actions(self, incident_id: str) -> list[dict[str, Any]]:
        return self._actions("incident_id = ?", (incident_id,))

    def _actions(self, where: str, params: tuple[Any, ...]) -> list[dict[str, Any]]:
        with self._tx() as cur:
            cur.execute(
                "SELECT id, incident_id, finding_id, action, session_key, target, verdict, reasons, step, "  # noqa: S608
                f"approved_by, status, detail, at FROM dbr_actions WHERE {where} ORDER BY at, id",
                params,
            )
            rows = cur.fetchall()
        keys = (
            "id",
            "incident_id",
            "finding_id",
            "action",
            "session_key",
            "target",
            "verdict",
            "reasons",
            "step",
            "approved_by",
            "status",
            "detail",
            "at",
        )
        out = []
        for r in rows:
            item = dict(zip(keys, r, strict=True))
            item["target"] = json.loads(item["target"])
            item["reasons"] = json.loads(item["reasons"])
            out.append(item)
        return out


class _Cursor:
    def __init__(self, cur: Any, paramstyle: str) -> None:
        self._cur = cur
        self._paramstyle = paramstyle

    def execute(self, query: str, params: tuple[Any, ...] = ()) -> None:
        if self._paramstyle != "?":
            query = query.replace("?", self._paramstyle)
        self._cur.execute(query, params)

    def fetchone(self) -> Any:
        return self._cur.fetchone()

    def fetchall(self) -> list[Any]:
        return list(self._cur.fetchall())
