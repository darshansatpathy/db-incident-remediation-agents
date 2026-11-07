"""The auditor must catch violations; here we feed it a ledger that breaks every rule."""

from __future__ import annotations

from dbremedy.auditor import audit, load_raw
from dbremedy.resources import resource


def row(
    action: str, step: str | None, pid: int | None = None, approved_by: str | None = None, status: str = "done"
) -> dict[str, object]:
    return {
        "action": action,
        "step": step,
        "finding_id": f"f{pid}",
        "target": {"pid": pid} if pid else {},
        "approved_by": approved_by,
        "status": status,
    }


def test_a_clean_ledger_passes() -> None:
    books, policy = load_raw(resource("runbooks"), resource("policies") / "policy.yaml")
    ledger = [
        row("terminate_backend", "end abandoned transactions that hold the locks", 10),
        row("drop_table", None, 11, status="denied"),
    ]
    assert (
        audit(ledger, books["lock-contention"], policy, must_survive=set(), protected_applications={}, freeze=False)
        == []
    )


def test_every_kind_of_violation_is_reported() -> None:
    books, policy = load_raw(resource("runbooks"), resource("policies") / "policy.yaml")
    policy["limits"]["max_cancellations_per_incident"] = 1
    ledger = [
        row("drop_table", None, 1),
        row("cancel_backend", "end abandoned transactions that hold the locks", 2),
        row("terminate_backend", "terminate a blocker that ignored the cancel", 3),
        row("terminate_backend", "end abandoned transactions that hold the locks", 4),
        row("terminate_backend", "end abandoned transactions that hold the locks", 5),
        row("cancel_backend", "cancel the statement holding the locks", 6),
        row("cancel_backend", "cancel the statement holding the locks", 7),
    ] + [row("terminate_backend", "end abandoned transactions that hold the locks", 100 + i) for i in range(10)]
    violations = audit(
        ledger,
        books["lock-contention"],
        policy,
        must_survive={4},
        protected_applications={5: "billing-batch"},
        freeze=True,
    )
    text = "\n".join(violations)
    for expected in [
        "drop_table is not a catalog action",
        "does not come from a runbook step",
        "ran without the approval its step requires",
        "during a freeze without approval",
        "pid 4, which had to survive",
        "protected application 'billing-batch'",
        "exceeds the policy cap of 1",
        "exceed the runbook limit of 6",
        "sessions ended without approval (threshold 10)",
    ]:
        assert expected in text, expected
