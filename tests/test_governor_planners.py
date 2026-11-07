from __future__ import annotations

import json
from collections import Counter
from typing import Any

import httpx
import pytest

from dbremedy.config import Settings, load_runbooks
from dbremedy.detectors import Finding
from dbremedy.governor import Context, Proposal, review
from dbremedy.planners import build
from dbremedy.planners.llm import LLMPlanner, PlannerError
from dbremedy.planners.rogue import RoguePlanner
from dbremedy.planners.rule import RulePlanner
from dbremedy.policy import Policy
from dbremedy.resources import resource
from dbremedy.runbooks import Runbook
from tests.conftest import NOW, session, snapshot

BOOKS = load_runbooks(Settings(observer_dsn="x", operator_dsn="y"))
LOCKS = BOOKS["lock-contention"]


def blocker(pid: int = 10, **attrs: Any) -> Finding:
    base = {
        "pid": pid,
        "state": "idle in transaction",
        "application": "checkout",
        "user": "app",
        "superuser": False,
        "backend_type": "client backend",
        "query_age_s": 500.0,
        "state_age_s": 500.0,
        "xact_age_s": 500.0,
        "session_key": f"key{pid}",
        "blocked_count": 3,
    }
    base.update(attrs)
    return Finding(f"s{pid}-abc", "blocking_locks", "session", f"pid {pid}", base, {"type": "session", "pid": pid})


def ctx(findings: list[Finding], *, book: Runbook = LOCKS, policy: Policy | None = None, **kw: Any) -> Context:
    return Context(
        runbook=book,
        policy=policy or Policy(),
        findings={f.id: f for f in findings},
        history=kw.get("history", set()),
        executed=kw.get("executed", set()),
        counts=kw.get("counts", Counter()),
        now=NOW,
        freeze=kw.get("freeze", False),
        unapproved_terminations=kw.get("unapproved", 0),
    )


def verdicts(decisions: list[Any]) -> list[tuple[str, str]]:
    return [(d.verdict, d.code) for d in decisions]


def test_allows_a_matching_step() -> None:
    f = blocker()
    (d,) = review([Proposal(f.id, "terminate_backend")], ctx([f]))
    assert (d.verdict, d.step) == ("allow", "end abandoned transactions that hold the locks")
    assert d.to_dict()["target"] == {"type": "session", "pid": 10}


@pytest.mark.parametrize(
    ("proposal", "attrs", "code"),
    [
        (Proposal("s10-abc", "drop_table"), {}, "unknown_action"),
        (Proposal("s10-abc", "vacuum_analyze"), {}, "not_in_runbook"),
        (Proposal("s99-zzz", "terminate_backend"), {}, "unknown_target"),
        (Proposal("s10-abc", "terminate_backend"), {"superuser": True}, "protected"),
        (Proposal("s10-abc", "terminate_backend"), {"backend_type": "autovacuum worker"}, "protected"),
        (Proposal("s10-abc", "terminate_backend"), {"user": "dbr_operator"}, "protected"),
        (Proposal("s10-abc", "terminate_backend"), {"user": "postgres"}, "protected"),
        (Proposal("s10-abc", "terminate_backend"), {"application": "billing-nightly"}, "protected"),
        (Proposal("s10-abc", "terminate_backend"), {"state_age_s": 5.0}, "conditions_not_met"),
        (Proposal("s10-abc", "cancel_backend"), {}, "conditions_not_met"),
    ],
)
def test_denials(proposal: Proposal, attrs: dict[str, Any], code: str) -> None:
    policy = Policy.model_validate({"protected": {"applications": ["billing-*"], "users": ["postgres"]}})
    (d,) = review([proposal], ctx([blocker(**attrs)], policy=policy))
    assert (d.verdict, d.code) == ("deny", code)


def test_wrong_target_kind() -> None:
    book = BOOKS["table-bloat"]
    f = blocker()
    (d,) = review([Proposal(f.id, "vacuum_analyze")], ctx([f], book=book))
    assert d.code == "wrong_target"


def test_duplicates_and_history() -> None:
    f = blocker()
    decisions = review([Proposal(f.id, "terminate_backend")] * 2, ctx([f]))
    assert verdicts(decisions) == [("allow", "allow"), ("deny", "duplicate")]
    (d,) = review([Proposal(f.id, "terminate_backend")], ctx([f], executed={(f.id, "terminate_backend")}))
    assert d.code == "duplicate"


def test_after_conditions_use_history() -> None:
    active = blocker(state="active")
    (d,) = review([Proposal(active.id, "terminate_backend")], ctx([active]))
    assert d.code == "conditions_not_met"
    (d,) = review([Proposal(active.id, "terminate_backend")], ctx([active], history={("key10", "cancel_backend")}))
    assert (d.verdict, d.step) == ("approval", "terminate a blocker that ignored the cancel")


def test_caps() -> None:
    findings = [blocker(pid) for pid in range(10, 20)]
    decisions = review([Proposal(f.id, "terminate_backend") for f in findings], ctx(findings))
    assert Counter(d.verdict for d in decisions) == {"allow": 6, "deny": 4}  # runbook max_actions 6
    policy = Policy.model_validate({"limits": {"max_terminations_per_incident": 2}})
    decisions = review([Proposal(f.id, "terminate_backend") for f in findings[:3]], ctx(findings, policy=policy))
    assert verdicts(decisions)[-1] == ("deny", "cap_reached")
    (d,) = review([Proposal(findings[0].id, "terminate_backend")], ctx(findings, counts=Counter(terminate_backend=6)))
    assert d.code == "cap_reached"


def test_batch_threshold_is_cumulative() -> None:
    book = BOOKS["connection-saturation"]
    findings = [blocker(pid, state="idle") for pid in range(100, 112)]
    decisions = review([Proposal(f.id, "terminate_backend") for f in findings], ctx(findings, book=book))
    assert {d.verdict for d in decisions} == {"approval"} and "batch" in decisions[0].reasons[0]
    few = findings[:3]
    decisions = review([Proposal(f.id, "terminate_backend") for f in few], ctx(few, book=book))
    assert {d.verdict for d in decisions} == {"allow"}
    decisions = review([Proposal(f.id, "terminate_backend") for f in few], ctx(few, book=book, unapproved=9))
    assert {d.verdict for d in decisions} == {"approval"}


def test_freeze_turns_allow_into_approval() -> None:
    f = blocker()
    (d,) = review([Proposal(f.id, "terminate_backend")], ctx([f], freeze=True))
    assert d.verdict == "approval" and d.reasons == ["freeze: a change freeze is in effect"]


def test_rule_planner_picks_the_first_matching_step() -> None:
    planner = RulePlanner()
    idle, active, young = blocker(1), blocker(2, state="active"), blocker(3, state_age_s=1.0, query_age_s=1.0)
    proposals = planner.propose(LOCKS, [idle, active, young], set(), None)
    assert [(p.finding_id, p.action) for p in proposals] == [
        (idle.id, "terminate_backend"),
        (active.id, "cancel_backend"),
    ]


def test_rogue_planner_proposes_everything_and_the_governor_keeps_only_the_runbook() -> None:
    f = blocker()
    snap = snapshot(session(10), session(11, superuser=True), session(12, application="billing-batch"))
    proposals = RoguePlanner().propose(LOCKS, [f], set(), snap)
    assert len(proposals) > 8
    decisions = review(proposals, ctx([f]))
    allowed = [d for d in decisions if d.verdict != "deny"]
    assert [(d.proposal.finding_id, d.proposal.action) for d in allowed] == [(f.id, "terminate_backend")]
    assert {"unknown_action", "unknown_target", "duplicate", "not_in_runbook"} <= {d.code for d in decisions}


def test_build_planners(monkeypatch: pytest.MonkeyPatch) -> None:
    assert build("rule").name == "rule" and build("rogue").name == "rogue"
    with pytest.raises(ValueError, match="unknown planner"):
        build("oracle")
    monkeypatch.delenv("DBR_LLM_BASE_URL", raising=False)
    with pytest.raises(PlannerError, match="DBR_LLM_BASE_URL"):
        build("llm")
    monkeypatch.setenv("DBR_LLM_BASE_URL", "https://llm.example/v1/")
    monkeypatch.setenv("DBR_LLM_MODEL", "m")
    planner = build("llm")
    assert isinstance(planner, LLMPlanner) and planner.base_url == "https://llm.example/v1"


def _llm(handler: Any) -> LLMPlanner:
    return LLMPlanner("https://llm.example/v1", "m", "key", transport=httpx.MockTransport(handler))


def test_llm_planner_sends_constrained_tools_and_parses_calls() -> None:
    f = blocker()
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        seen["auth"] = request.headers.get("authorization")
        arguments = {
            "actions": [
                {"finding_id": f.id, "action": "terminate_backend", "reason": "abandoned"},
                "junk",
                {"finding_id": "x", "action": "drop_table"},
            ]
        }
        call = {"function": {"name": "propose_actions", "arguments": json.dumps(arguments)}}
        other = {"function": {"name": "something_else", "arguments": "{}"}}
        return httpx.Response(200, json={"choices": [{"message": {"tool_calls": [other, call]}}]})

    proposals = _llm(handler).propose(LOCKS, [f], {("key10", "cancel_backend")}, None)
    assert [(p.finding_id, p.action, p.source) for p in proposals] == [
        (f.id, "terminate_backend", "llm"),
        ("x", "drop_table", "llm"),
    ]
    tool = seen["body"]["tools"][0]["function"]["parameters"]["properties"]["actions"]["items"]["properties"]
    assert tool["finding_id"]["enum"] == [f.id]
    assert tool["action"]["enum"] == ["cancel_backend", "terminate_backend"]
    assert "<incident_data>" in seen["body"]["messages"][1]["content"]
    assert seen["auth"] == "Bearer key"


def test_llm_planner_errors() -> None:
    f = blocker()
    with pytest.raises(PlannerError, match="model call failed"):
        _llm(lambda r: httpx.Response(500)).propose(LOCKS, [f], set(), None)
    bad = {"choices": [{"message": {"tool_calls": [{"function": {"name": "propose_actions", "arguments": "{"}}]}}]}
    with pytest.raises(PlannerError, match="invalid JSON"):
        _llm(lambda r: httpx.Response(200, json=bad)).propose(LOCKS, [f], set(), None)
    assert (
        _llm(lambda r: httpx.Response(200, json={"choices": [{"message": {}}]})).propose(LOCKS, [f], set(), None) == []
    )
    assert _llm(lambda r: httpx.Response(500)).propose(LOCKS, [], set(), None) == []


def test_resource_paths() -> None:
    assert (resource("runbooks") / "lock-contention.yaml").exists()
