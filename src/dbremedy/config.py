"""Settings from the environment, and wiring them into an engine."""

from __future__ import annotations

import os
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import psycopg
import yaml
from psycopg.conninfo import conninfo_to_dict

from dbremedy.engine import Engine
from dbremedy.graph import Services
from dbremedy.observability import Metrics
from dbremedy.planners import build as build_planner
from dbremedy.policy import Policy, load_policy
from dbremedy.resources import resource
from dbremedy.runbooks import Runbook, load_folder
from dbremedy.store import Store


class ConfigError(ValueError):
    pass


def _flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class Settings:
    observer_dsn: str = ""
    operator_dsn: str = ""
    store: str = "memory"
    sqlite_path: str = "dbremedy.db"
    store_dsn: str = ""
    planner: str = "rule"
    runbooks_dir: Path = field(default_factory=lambda: resource("runbooks"))
    policy_path: Path = field(default_factory=lambda: resource("policies") / "policy.yaml")
    overrides_path: Path | None = None
    webhook_token: str = ""
    service_token: str = ""
    change_freeze: bool = False
    dry_run: bool = False
    log_level: str = "INFO"

    @classmethod
    def from_env(cls) -> Settings:
        env = os.environ.get
        overrides = env("DBR_RUNBOOK_OVERRIDES")
        return cls(
            observer_dsn=env("DBR_TARGET_OBSERVER_DSN", ""),
            operator_dsn=env("DBR_TARGET_OPERATOR_DSN", ""),
            store=env("DBR_STORE", "memory"),
            sqlite_path=env("DBR_SQLITE_PATH", "dbremedy.db"),
            store_dsn=env("DBR_STORE_DSN", ""),
            planner=env("DBR_PLANNER", "rule"),
            runbooks_dir=Path(env("DBR_RUNBOOKS", str(resource("runbooks")))),
            policy_path=Path(env("DBR_POLICY", str(resource("policies") / "policy.yaml"))),
            overrides_path=Path(overrides) if overrides else None,
            webhook_token=env("DBR_WEBHOOK_TOKEN", ""),
            service_token=env("DBR_SERVICE_TOKEN", ""),
            change_freeze=_flag("DBR_CHANGE_FREEZE"),
            dry_run=_flag("DBR_DRY_RUN"),
            log_level=env("DBR_LOG_LEVEL", "INFO"),
        )

    def check(self) -> None:
        missing = [
            n
            for n, v in (("DBR_TARGET_OBSERVER_DSN", self.observer_dsn), ("DBR_TARGET_OPERATOR_DSN", self.operator_dsn))
            if not v
        ]
        if missing:
            raise ConfigError(f"set {', '.join(missing)}")
        if self.store not in ("memory", "sqlite", "postgres"):
            raise ConfigError("DBR_STORE must be memory, sqlite or postgres")
        if self.store == "postgres" and not self.store_dsn:
            raise ConfigError("DBR_STORE=postgres needs DBR_STORE_DSN")
        if self.store == "postgres" and same_database(self.store_dsn, self.observer_dsn):
            raise ConfigError(
                "DBR_STORE_DSN points at the target database. Keep the agent's store elsewhere: its "
                "first start runs CREATE INDEX CONCURRENTLY, which waits for every open transaction, "
                "including the incident's."
            )


def same_database(a: str, b: str) -> bool:
    """Whether two DSNs name the same database on the same server (by host, port and name)."""
    try:
        left, right = conninfo_to_dict(a), conninfo_to_dict(b)
    except psycopg.ProgrammingError:
        return False

    def where(info: dict[str, Any]) -> tuple[str, str, str]:
        host = str(info.get("host") or "localhost")
        host = "localhost" if host in ("127.0.0.1", "::1") or host.startswith("/") else host
        return host, str(info.get("port") or "5432"), str(info.get("dbname") or info.get("user") or "")

    return where(left) == where(right)


def load_overrides(path: Path | None) -> dict[str, dict[str, float]]:
    if path is None:
        return {}
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise ConfigError(f"{path}: expected runbook id -> parameter overrides")
    return {str(k): {str(p): float(v) for p, v in (params or {}).items()} for k, params in data.items()}


def load_runbooks(settings: Settings) -> dict[str, Runbook]:
    return load_folder(settings.runbooks_dir, load_overrides(settings.overrides_path))


def load_effective_policy(settings: Settings) -> Policy:
    policy = load_policy(settings.policy_path)
    if settings.dry_run:
        policy = policy.model_copy(update={"dry_run": True})
    return policy


@dataclass
class Wiring:
    engine: Engine
    store: Store
    metrics: Metrics
    closers: list[Callable[[], None]]

    def close(self) -> None:
        for closer in reversed(self.closers):
            closer()


def _checkpointer(settings: Settings, closers: list[Callable[[], None]]) -> Any:
    if settings.store == "memory":
        from langgraph.checkpoint.memory import InMemorySaver

        return InMemorySaver()
    if settings.store == "sqlite":
        from langgraph.checkpoint.sqlite import SqliteSaver

        conn = sqlite3.connect(settings.sqlite_path + ".checkpoints", check_same_thread=False)
        closers.append(conn.close)
        saver = SqliteSaver(conn)
        saver.setup()
        return saver
    from langgraph.checkpoint.postgres import PostgresSaver
    from psycopg import Connection
    from psycopg.rows import dict_row

    pg = Connection.connect(settings.store_dsn, autocommit=True, prepare_threshold=0, row_factory=dict_row)
    closers.append(pg.close)
    pg_saver = PostgresSaver(pg)
    pg_saver.setup()
    return pg_saver


def build(settings: Settings, *, planner: Any = None, metrics: Metrics | None = None, **services: Any) -> Wiring:
    settings.check()
    closers: list[Callable[[], None]] = []
    if settings.store == "postgres":
        store = Store.postgres(settings.store_dsn)
    elif settings.store == "sqlite":
        store = Store.sqlite(settings.sqlite_path)
    else:
        store = Store.sqlite(":memory:")
    closers.append(store.close)
    metrics = metrics or Metrics()
    svc = Services(
        observer_dsn=settings.observer_dsn,
        operator_dsn=settings.operator_dsn,
        runbooks=load_runbooks(settings),
        policy=load_effective_policy(settings),
        planner=planner or build_planner(settings.planner),
        store=store,
        freeze=settings.change_freeze,
        on_event=metrics.on_event,
        **services,
    )
    engine = Engine(svc, _checkpointer(settings, closers))
    return Wiring(engine, store, metrics, closers)
