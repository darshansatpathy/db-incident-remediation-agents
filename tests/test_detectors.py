from __future__ import annotations

from dbremedy.detectors import (
    blocking_locks,
    connection_saturation,
    idle_in_transaction,
    long_running_queries,
    session_key,
    table_bloat,
)
from tests.conftest import session, snapshot, table


def test_blocking_finds_the_root_of_a_chain() -> None:
    root = session(10, state="idle in transaction", state_age_s=40, application="checkout")
    middle = session(11, blocked_by=(10,), wait_event_type="Lock", state_age_s=35)
    leaf = session(12, blocked_by=(11,), wait_event_type="Lock", state_age_s=31)
    fresh = session(13, blocked_by=(10,), wait_event_type="Lock", state_age_s=2)
    findings = blocking_locks(snapshot(root, middle, leaf, fresh), {"min_wait_s": 30})
    assert len(findings) == 1
    f = findings[0]
    assert f.attributes["pid"] == 10 and f.attributes["blocked_count"] == 2
    assert f.attributes["max_wait_s"] == 35
    assert f.target["type"] == "session" and f.target["pid"] == 10
    assert {b["pid"] for b in f.evidence["blocked"]} == {11, 12}
    assert f.id.startswith("s10-")


def test_blocking_ignores_short_waits_and_the_agents_own_sessions() -> None:
    root = session(10, usename="dbr_operator")
    waiter = session(11, blocked_by=(10,), wait_event_type="Lock", state_age_s=60)
    assert blocking_locks(snapshot(root, waiter), {"min_wait_s": 30}) == []
    assert (
        blocking_locks(
            snapshot(session(20), session(21, blocked_by=(20,), wait_event_type="Lock", state_age_s=5)),
            {"min_wait_s": 30},
        )
        == []
    )


def test_blocking_reports_roots_that_are_not_clients() -> None:
    root = session(10, backend_type="autovacuum worker", usename="")
    waiter = session(11, blocked_by=(10,), wait_event_type="Lock", state_age_s=60)
    (finding,) = blocking_locks(snapshot(root, waiter), {"min_wait_s": 1})
    assert finding.attributes["backend_type"] == "autovacuum worker"


def test_long_running_skips_lock_waiters_and_idle_sessions() -> None:
    slow = session(1, query_age_s=400, application="reporting")
    waiting = session(2, query_age_s=400, wait_event_type="Lock", blocked_by=(9,))
    idle = session(3, state="idle", query_age_s=900)
    background = session(4, backend_type="walsender", query_age_s=900)
    found = long_running_queries(snapshot(slow, waiting, idle, background), {"min_query_age_s": 300})
    assert [f.attributes["pid"] for f in found] == [1]


def test_idle_in_transaction_covers_aborted_transactions() -> None:
    a = session(1, state="idle in transaction", state_age_s=700)
    b = session(2, state="idle in transaction (aborted)", state_age_s=700)
    c = session(3, state="idle in transaction", state_age_s=10)
    assert [f.attributes["pid"] for f in idle_in_transaction(snapshot(a, b, c), {"min_state_age_s": 600})] == [1, 2]


def test_connection_saturation_targets_idle_sessions_of_the_hog() -> None:
    hog = [session(100 + i, application="leaky", state="idle", state_age_s=400) for i in range(60)]
    busy = [session(200 + i, application="leaky", state="active") for i in range(5)]
    others = [session(300 + i, application="checkout", state="idle", state_age_s=400) for i in range(15)]
    snap = snapshot(*hog, *busy, *others)
    found = connection_saturation(snap, {"max_ratio": 0.8, "min_idle_s": 300, "min_application_share": 0.3})
    assert len(found) == 60 and {f.attributes["application"] for f in found} == {"leaky"}
    assert found[0].attributes["usage_ratio"] == round(80 / 97, 3)
    assert connection_saturation(snap, {"max_ratio": 0.9}) == []


def test_table_bloat_needs_both_ratio_and_count() -> None:
    tables = (table("events", 10000, 20000), table("tiny", 10, 90), table("big", 10_000_000, 30000))
    found = table_bloat(snapshot(tables=tables), {"min_dead_ratio": 0.2, "min_dead_tuples": 10000})
    assert [f.attributes["table"] for f in found] == ["shop.events"]
    assert found[0].target == {"type": "table", "schema": "shop", "name": "events"}
    assert found[0].id == "t-shop.events"


def test_session_key_changes_when_a_pid_is_reused() -> None:
    first = session(5, age=60)
    reused = session(5, age=10)
    assert session_key(first) != session_key(reused)
    assert first.fingerprint != reused.fingerprint


def test_snapshot_helpers() -> None:
    snap = snapshot(
        session(1),
        session(2, state="idle", blocked_by=(1,)),
        session(3, backend_type="walsender"),
        tables=(table("events", 1, 1),),
    )
    assert snap.session(2) is not None and snap.session(99) is None
    assert len(snap.client_sessions) == 2
    assert snap.table("shop.events") is not None and snap.table("x.y") is None
    summary = snap.summary()
    assert summary["blocked_sessions"] == 1 and summary["states"] == {"active": 1, "idle": 1}
    assert table("t", 0, 0).dead_ratio == 0.0
