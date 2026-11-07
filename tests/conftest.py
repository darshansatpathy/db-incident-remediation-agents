from __future__ import annotations

import os
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from psycopg.conninfo import make_conninfo

from dbremedy.bootstrap import APP, OBSERVER, OPERATOR, Credentials, install
from dbremedy.config import Settings
from dbremedy.resources import resource
from dbremedy.snapshot import Session, Snapshot, TableStats

NOW = datetime(2026, 11, 7, 12, 0, tzinfo=UTC)


@dataclass(frozen=True)
class Target:
    admin: str
    app: str
    observer: str
    operator: str

    def settings(self, **overrides: Any) -> Settings:
        base = Settings(
            observer_dsn=self.observer,
            operator_dsn=self.operator,
            overrides_path=resource("evals") / "overrides.yaml",
        )
        return replace(base, **overrides)


@pytest.fixture(scope="session")
def target() -> Target:
    admin = os.environ.get("DBR_TEST_ADMIN_DSN")
    if not admin:
        pytest.skip("set DBR_TEST_ADMIN_DSN (a superuser DSN for a scratch database) to run PostgreSQL tests")
    creds = Credentials("test-observer", "test-operator", "test-app")
    install(admin, creds)
    return Target(
        admin=admin,
        app=make_conninfo(admin, user=APP, password=creds.app_password),
        observer=make_conninfo(admin, user=OBSERVER, password=creds.observer_password),
        operator=make_conninfo(admin, user=OPERATOR, password=creds.operator_password),
    )


def session(pid: int, **kw: Any) -> Session:
    started = NOW - timedelta(seconds=kw.pop("age", 60))
    defaults: dict[str, Any] = {
        "pid": pid,
        "usename": "app",
        "application": "checkout",
        "client": "10.0.0.5",
        "state": "active",
        "backend_type": "client backend",
        "datname": "shop",
        "backend_start": started,
        "xact_start": started,
        "query_start": started,
        "query_age_s": 0.0,
        "state_age_s": 0.0,
        "xact_age_s": 0.0,
        "wait_event_type": "",
        "wait_event": "",
        "query": "SELECT 1",
        "blocked_by": (),
        "superuser": False,
    }
    defaults.update(kw)
    return Session(**defaults)


def table(name: str, live: int, dead: int, size_mb: float = 10.0) -> TableStats:
    return TableStats("shop", name, live, dead, size_mb, None, None)


def snapshot(*sessions: Session, tables: tuple[TableStats, ...] = (), max_connections: int = 100) -> Snapshot:
    return Snapshot("shop", "17.0", max_connections, 3, NOW, tuple(sessions), tables)


@pytest.fixture
def runbooks_dir() -> Path:
    return resource("runbooks")
