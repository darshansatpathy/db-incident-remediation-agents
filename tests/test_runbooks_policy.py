from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from dbremedy.config import ConfigError, Settings, load_overrides
from dbremedy.detectors import Finding
from dbremedy.policy import FreezeWindow, Policy, load_policy
from dbremedy.resources import resource
from dbremedy.runbooks import Conditions, RunbookError, for_alert, load_folder, parse

BASE = {
    "id": "demo",
    "version": "1.0.0",
    "title": "Demo",
    "alerts": ["DemoAlert"],
    "detector": {"name": "idle_in_transaction", "params": {"min_state_age_s": "${idle}"}},
    "parameters": {"idle": 60},
    "steps": [{"name": "end it", "action": "terminate_backend", "when": {"min_state_age_s": "${idle}"}}],
    "escalation": {"team": "dba"},
}


def finding(**attributes: object) -> Finding:
    return Finding("s1-x", "d", "session", "s", {"session_key": "k1", **attributes}, {"type": "session"})


def test_bundled_runbooks_load_and_cover_distinct_alerts(runbooks_dir: Path) -> None:
    books = load_folder(runbooks_dir)
    assert set(books) == {
        "lock-contention",
        "runaway-query",
        "idle-in-transaction",
        "connection-saturation",
        "table-bloat",
    }
    assert for_alert(books, "PostgresBlockedSessions").id == "lock-contention"  # type: ignore[union-attr]
    assert for_alert(books, "Nope") is None
    assert books["lock-contention"].verify.stable_s == 10


def test_parameters_and_overrides() -> None:
    book = parse(BASE, {"idle": 2})
    assert book.detector.params == {"min_state_age_s": 2.0}
    assert book.steps[0].when.min_state_age_s == 2.0
    with pytest.raises(RunbookError, match="unknown parameter"):
        parse(BASE, {"nope": 1})
    with pytest.raises(RunbookError, match=r"unknown parameter \$\{missing\}"):
        parse({**BASE, "detector": {"name": "idle_in_transaction", "params": {"min_state_age_s": "${missing}"}}})


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"detector": {"name": "nope"}}, "unknown detector"),
        ({"steps": [{"name": "x", "action": "vacuum_analyze"}]}, "cannot act on session findings"),
        ({"steps": [{"name": "x", "action": "drop_table"}]}, "Input should be"),
        ({"extra": 1}, "Extra inputs"),
        ({"id": "Bad Id"}, "String should match"),
    ],
)
def test_invalid_runbooks(change: dict[str, object], message: str) -> None:
    with pytest.raises(RunbookError, match=message):
        parse({**BASE, **change})
    with pytest.raises(RunbookError, match="expected a mapping"):
        parse([])  # type: ignore[arg-type]


def test_folder_rejects_duplicates_and_unknown_overrides(tmp_path: Path) -> None:
    import yaml

    (tmp_path / "a.yaml").write_text(yaml.safe_dump(BASE))
    (tmp_path / "b.yaml").write_text(yaml.safe_dump({**BASE, "id": "other"}))
    with pytest.raises(RunbookError, match="handled by both"):
        load_folder(tmp_path)
    (tmp_path / "b.yaml").write_text(yaml.safe_dump(BASE))
    with pytest.raises(RunbookError, match="duplicate runbook id"):
        load_folder(tmp_path)
    (tmp_path / "b.yaml").unlink()
    with pytest.raises(RunbookError, match="unknown runbooks"):
        load_folder(tmp_path, {"ghost": {"x": 1}})


def test_conditions() -> None:
    c = Conditions(
        state_in=["active"],
        application_matches=["report*"],
        application_not_matches=["report-critical"],
        min_query_age_s=10,
        min_blocked=2,
        max_size_mb=5,
        after="cancel_backend",
    )
    ok = finding(state="active", application="reporting", query_age_s=11, blocked_count=3, size_mb=1)
    assert c.mismatches(ok, {("k1", "cancel_backend")}) == []
    reasons = c.mismatches(
        finding(state="idle", application="report-critical", query_age_s=1, blocked_count=0, size_mb=9), set()
    )
    assert len(reasons) == 6
    assert any("has not been tried" in r for r in reasons)
    assert Conditions(application_matches=["x*"]).mismatches(finding(application="y"), set())


def test_matching_steps_explains_mismatches() -> None:
    book = parse(BASE, {"idle": 60})
    steps, reasons = book.matching_steps(finding(state_age_s=5), "terminate_backend", set())
    assert steps == [] and "below 60" in reasons[0]
    steps, _ = book.matching_steps(finding(state_age_s=90), "terminate_backend", set())
    assert [s.name for s in steps] == ["end it"]
    assert book.matching_steps(finding(), "cancel_backend", set()) == ([], [])


def test_policy_and_freeze_windows(tmp_path: Path) -> None:
    policy = load_policy(resource("policies") / "policy.yaml")
    assert policy.protected.application("billing-batch") == "billing-*"
    assert policy.protected.application("checkout") is None
    assert policy.limits.cap("terminate_backend") == 60 and policy.limits.cap("drop") == 0
    saturday_night = datetime(2026, 11, 7, 20, 0, tzinfo=UTC)
    window = FreezeWindow(days=["sat"], start="18:00", end="23:59")
    assert window.contains(saturday_night)
    assert not window.contains(datetime(2026, 11, 7, 10, 0, tzinfo=UTC))
    assert not window.contains(datetime(2026, 11, 9, 20, 0, tzinfo=UTC))
    frozen = Policy.model_validate({"change_freeze": {"windows": [window.model_dump()]}})
    assert frozen.frozen(saturday_night) and not frozen.frozen(datetime(2026, 11, 9, tzinfo=UTC))
    assert Policy().frozen(saturday_night, override=True)
    with pytest.raises(ValueError):
        FreezeWindow(days=["sat"], start="25:00")
    with pytest.raises(ValueError):
        FreezeWindow(days=["sat"], timezone="Mars/Olympus")
    empty = tmp_path / "p.yaml"
    empty.write_text("")
    assert load_policy(empty) == Policy()


def test_settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(ConfigError, match="DBR_TARGET_OBSERVER_DSN"):
        Settings().check()
    with pytest.raises(ConfigError, match="DBR_STORE must be"):
        Settings(observer_dsn="x", operator_dsn="y", store="mongo").check()
    with pytest.raises(ConfigError, match="needs DBR_STORE_DSN"):
        Settings(observer_dsn="x", operator_dsn="y", store="postgres").check()
    monkeypatch.setenv("DBR_TARGET_OBSERVER_DSN", "a")
    monkeypatch.setenv("DBR_CHANGE_FREEZE", "yes")
    monkeypatch.setenv("DBR_RUNBOOK_OVERRIDES", str(tmp_path / "o.yaml"))
    settings = Settings.from_env()
    assert settings.observer_dsn == "a" and settings.change_freeze and settings.overrides_path is not None
    (tmp_path / "o.yaml").write_text("- 1\n")
    with pytest.raises(ConfigError, match="expected runbook id"):
        load_overrides(tmp_path / "o.yaml")
    (tmp_path / "o.yaml").write_text("lock-contention: {min_wait_s: 1}\n")
    assert load_overrides(tmp_path / "o.yaml") == {"lock-contention": {"min_wait_s": 1.0}}
    assert load_overrides(None) == {}
