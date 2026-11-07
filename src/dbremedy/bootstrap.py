"""Least-privilege roles and the demo schema used by the scenarios.

The agent never connects as a superuser. It uses two roles:

* ``dbr_observer``: member of ``pg_monitor``, read-only by default, short statement timeout.
  Every diagnostic runs as this role.
* ``dbr_operator``: member of ``pg_signal_backend`` (cancel and terminate non-superuser
  sessions), ``pg_read_all_stats`` (to re-check a session's fingerprint before signalling)
  and, on PostgreSQL 17+, ``pg_maintain`` (VACUUM and ANALYZE). On older versions it is
  granted the demo schema's owner role instead, which is broader; see the threat model.

``pg_signal_backend`` cannot signal superuser sessions, so the database itself enforces one
of the governor's rules a second time.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import psycopg
from psycopg import sql

OBSERVER = "dbr_observer"
OPERATOR = "dbr_operator"
APP_OWNER = "dbr_app_owner"
APP = "dbr_app"
AGENT_ROLES = (OBSERVER, OPERATOR)


@dataclass(frozen=True)
class Credentials:
    observer_password: str
    operator_password: str
    app_password: str


def _ensure_role(cur: psycopg.Cursor[Any], name: str, password: str | None, *in_roles: str) -> None:
    cur.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (name,))
    if cur.fetchone() is None:
        cur.execute(sql.SQL("CREATE ROLE {} NOLOGIN").format(sql.Identifier(name)))
    if password is not None:
        cur.execute(sql.SQL("ALTER ROLE {} LOGIN PASSWORD {}").format(sql.Identifier(name), sql.Literal(password)))
    for role in in_roles:
        cur.execute(sql.SQL("GRANT {} TO {}").format(sql.Identifier(role), sql.Identifier(name)))


def server_version(conn: psycopg.Connection[Any]) -> int:
    with conn.cursor() as cur:
        cur.execute("SHOW server_version_num")
        row = cur.fetchone()
    assert row is not None
    return int(str(row[0]))


def install(admin_dsn: str, creds: Credentials, *, demo_schema: bool = True) -> list[str]:
    """Create or update the roles (and the demo schema). Safe to run repeatedly."""
    notes: list[str] = []
    with psycopg.connect(admin_dsn, autocommit=True) as conn, conn.cursor() as cur:
        version = server_version(conn)
        _ensure_role(cur, OBSERVER, creds.observer_password, "pg_monitor")
        cur.execute(sql.SQL("ALTER ROLE {} SET default_transaction_read_only = on").format(sql.Identifier(OBSERVER)))
        cur.execute(sql.SQL("ALTER ROLE {} SET statement_timeout = '5s'").format(sql.Identifier(OBSERVER)))
        _ensure_role(cur, OPERATOR, creds.operator_password, "pg_signal_backend", "pg_read_all_stats")
        cur.execute(sql.SQL("ALTER ROLE {} SET statement_timeout = '120s'").format(sql.Identifier(OPERATOR)))
        cur.execute(sql.SQL("ALTER ROLE {} SET lock_timeout = '5s'").format(sql.Identifier(OPERATOR)))
        _ensure_role(cur, APP_OWNER, None)
        _ensure_role(cur, APP, creds.app_password, APP_OWNER)
        if version >= 170000:
            cur.execute(sql.SQL("GRANT pg_maintain TO {}").format(sql.Identifier(OPERATOR)))
            notes.append("operator: pg_signal_backend + pg_maintain")
        else:
            cur.execute(sql.SQL("GRANT {} TO {}").format(sql.Identifier(APP_OWNER), sql.Identifier(OPERATOR)))
            notes.append("operator: pg_signal_backend + owner of the demo schema (PostgreSQL < 17 has no pg_maintain)")
        if demo_schema:
            create_demo_schema(cur)
            notes.append("demo schema shop.* created")
    return notes


def create_demo_schema(cur: psycopg.Cursor[Any]) -> None:
    owner = sql.Identifier(APP_OWNER)
    cur.execute(sql.SQL("CREATE SCHEMA IF NOT EXISTS shop AUTHORIZATION {}").format(owner))
    statements = [
        """CREATE TABLE IF NOT EXISTS shop.orders (
               id integer PRIMARY KEY, status text NOT NULL, amount numeric(10, 2) NOT NULL,
               updated_at timestamptz NOT NULL DEFAULT now())""",
        """CREATE TABLE IF NOT EXISTS shop.inventory (
               sku text PRIMARY KEY, qty integer NOT NULL)""",
        """CREATE TABLE IF NOT EXISTS shop.events (
               id integer PRIMARY KEY, payload text NOT NULL, seen integer NOT NULL DEFAULT 0)
           WITH (autovacuum_enabled = false)""",
    ]
    for statement in statements:
        cur.execute(statement)  # static DDL
    for table in ("orders", "inventory", "events"):
        cur.execute(sql.SQL("ALTER TABLE shop.{} OWNER TO {}").format(sql.Identifier(table), owner))
    cur.execute(
        "INSERT INTO shop.orders SELECT g, 'open', g * 1.5 FROM generate_series(1, 200) g ON CONFLICT DO NOTHING"
    )
    cur.execute(
        "INSERT INTO shop.inventory SELECT 'SKU-' || g, 100 FROM generate_series(1, 50) g ON CONFLICT DO NOTHING"
    )
    # VACUUM resolves the table by name, so the operator needs USAGE on every schema whose
    # tables it may vacuum; pg_maintain (PostgreSQL 17+) grants the privilege, not the lookup.
    for role in (OBSERVER, OPERATOR):
        cur.execute(sql.SQL("GRANT USAGE ON SCHEMA shop TO {}").format(sql.Identifier(role)))
