"""HTTP API: Alertmanager webhook in, incidents, approvals and reports out.

No ``from __future__ import annotations`` here: FastAPI must evaluate the dependency
annotations, which refer to closures inside ``create_app``.
"""

import hmac
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import asynccontextmanager, suppress
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, ConfigDict, Field

from dbremedy import __version__
from dbremedy.alerts import AlertmanagerPayload, payload_for
from dbremedy.config import Settings, Wiring
from dbremedy.detectors import DETECTORS
from dbremedy.engine import NotPaused
from dbremedy.observability import event
from dbremedy.snapshot import take

ROLES = {"viewer", "operator", "approver"}


class Caller(BaseModel):
    user: str
    roles: set[str]


class ManualIncident(BaseModel):
    model_config = ConfigDict(extra="forbid")

    alertname: str = Field(min_length=1, max_length=120)
    labels: dict[str, str] = {}
    summary: str = Field(default="", max_length=500)


class ApprovalDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    approved: bool
    comment: str = Field(default="", max_length=500)


def create_app(wiring: Wiring, settings: Settings, *, workers: int = 4) -> FastAPI:
    engine = wiring.engine
    pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="incident")
    running: dict[str, Future[Any]] = {}

    def submit(incident_id: str, fn: Any, *args: Any) -> None:
        def task() -> None:
            try:
                fn(*args)
            except Exception as exc:  # an incident must not take the service down
                wiring.store.event(incident_id, "error", f"run failed: {type(exc).__name__}: {exc}", {})
                event("incident_failed", incident_id=incident_id, error=str(exc))

        running[incident_id] = pool.submit(task)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> Any:
        pool.submit(engine.recover)
        yield
        pool.shutdown(wait=False, cancel_futures=True)

    app = FastAPI(title="db-incident-remediation-agents", version=__version__, lifespan=lifespan)

    def caller(
        x_user_id: Annotated[str | None, Header()] = None,
        x_user_roles: Annotated[str | None, Header()] = None,
        x_service_token: Annotated[str | None, Header()] = None,
    ) -> Caller:
        if settings.service_token and not hmac.compare_digest(x_service_token or "", settings.service_token):
            raise HTTPException(401, "missing or wrong service token")
        roles = {r.strip() for r in (x_user_roles or "").split(",") if r.strip()}
        unknown = roles - ROLES
        if unknown:
            raise HTTPException(400, f"unknown roles: {sorted(unknown)}")
        return Caller(user=x_user_id or "anonymous", roles=roles)

    def need(role: str) -> Any:
        def check(who: Annotated[Caller, Depends(caller)]) -> Caller:
            if role not in who.roles and not (role == "viewer" and who.roles):
                raise HTTPException(403, f"needs the {role} role")
            return who

        return check

    def webhook_auth(authorization: Annotated[str | None, Header()] = None) -> None:
        if not settings.webhook_token:
            return
        expected = f"Bearer {settings.webhook_token}"
        if not hmac.compare_digest(authorization or "", expected):
            raise HTTPException(401, "missing or wrong webhook token")

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/readyz")
    def readyz() -> dict[str, Any]:
        try:
            snap = take(settings.observer_dsn)
        except Exception as exc:
            raise HTTPException(503, f"target database unreachable: {type(exc).__name__}") from exc
        return {"status": "ready", "database": snap.database, "server_version": snap.server_version}

    @app.get("/metrics", response_class=PlainTextResponse)
    def metrics() -> str:
        return wiring.metrics.render()

    @app.post("/v1/alerts/alertmanager", status_code=202, dependencies=[Depends(webhook_auth)])
    def alertmanager(payload: AlertmanagerPayload) -> dict[str, Any]:
        results = engine.receive(payload)
        for r in results:
            if r["created"]:
                submit(r["incident_id"], engine.run, r["incident_id"])
        return {"incidents": results}

    @app.post("/v1/incidents", status_code=202)
    def open_incident(body: ManualIncident, who: Annotated[Caller, Depends(need("operator"))]) -> dict[str, Any]:
        payload = AlertmanagerPayload.model_validate(
            payload_for(body.alertname, {**body.labels, "opened_by": who.user}, body.summary)
        )
        results = engine.receive(payload)
        for r in results:
            if r["created"]:
                submit(r["incident_id"], engine.run, r["incident_id"])
        return {"incidents": results}

    @app.get("/v1/incidents")
    def incidents(
        _: Annotated[Caller, Depends(need("viewer"))],
        status: str | None = None,
        limit: Annotated[int, Query(ge=1, le=500)] = 50,
    ) -> dict[str, Any]:
        rows = wiring.store.incidents(limit=limit, status=status)
        return {"incidents": [{k: v for k, v in r.items() if k != "report"} for r in rows]}

    @app.get("/v1/incidents/{incident_id}")
    def incident(incident_id: str, _: Annotated[Caller, Depends(need("viewer"))]) -> dict[str, Any]:
        try:
            return engine.detail(incident_id)
        except KeyError as exc:
            raise HTTPException(404, "no such incident") from exc

    @app.get("/v1/incidents/{incident_id}/report", response_class=PlainTextResponse)
    def report(incident_id: str, _: Annotated[Caller, Depends(need("viewer"))]) -> str:
        try:
            text = wiring.store.get(incident_id).get("report")
        except KeyError as exc:
            raise HTTPException(404, "no such incident") from exc
        if not text:
            raise HTTPException(409, "the incident is still open")
        return str(text)

    @app.post("/v1/incidents/{incident_id}/approval", status_code=202)
    def approval(
        incident_id: str, body: ApprovalDecision, who: Annotated[Caller, Depends(need("approver"))]
    ) -> dict[str, Any]:
        try:
            incident = wiring.store.get(incident_id)
        except KeyError as exc:
            raise HTTPException(404, "no such incident") from exc
        if incident["status"] != "awaiting_approval":
            raise HTTPException(409, f"incident is {incident['status']}, not awaiting approval")
        if body.comment:
            wiring.store.event(incident_id, "comment", f"{who.user}: {body.comment}", {})

        def decide() -> None:
            with suppress(NotPaused):
                engine.decide(incident_id, body.approved, who.user)

        submit(incident_id, decide)
        return {"incident_id": incident_id, "approved": body.approved, "approver": who.user}

    @app.get("/v1/runbooks")
    def runbooks(_: Annotated[Caller, Depends(need("viewer"))]) -> dict[str, Any]:
        return {"runbooks": [b.model_dump() for b in engine.svc.runbooks.values()]}

    @app.get("/v1/diagnose")
    def diagnose(_: Annotated[Caller, Depends(need("viewer"))]) -> dict[str, Any]:
        snap = take(settings.observer_dsn)
        out = {}
        for book in engine.svc.runbooks.values():
            found = DETECTORS[book.detector.name](snap, dict(book.detector.params))
            out[book.id] = [f.to_dict() for f in found]
        return {"snapshot": snap.summary(), "findings": out}

    app.state.wait = lambda: [f.result(timeout=120) for f in list(running.values())]
    return app
