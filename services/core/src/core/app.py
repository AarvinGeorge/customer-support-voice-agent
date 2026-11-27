"""What: the core FastAPI application factory: lifespan, correlation middleware, health endpoints, routes.
Use when: uvicorn imports core.main, or a test builds an app with fake settings and clock.
Inputs: Settings, optional Clock. Output: create_app.
Side effects: lifespan loads and validates data/policy.yaml, opens the database pool (if configured) and logs
the contract and policy versions.
Role: doc 07 s3.6 health plus the s1.2 header handling; policy errors stop the process at startup."""
from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from contracts import VERSION
from contracts.core_api import CoreReady
from contracts.policy import load_policy
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from core.auth import clean_id, new_uuid7
from core.clock import Clock, SystemClock
from core.config import Settings
from core.db import Database
from core.errors import CoreError, install_handlers
from core.routes import register_routes

log = logging.getLogger("core")


def create_app(settings: Settings, clock: Clock | None = None) -> FastAPI:
    """What: assemble the app. Use when: process start (core.main) and tests. Inputs: settings, optional clock
    (default: system clock in the policy timezone). Output: FastAPI app with state settings, clock, policy, db.
    Side effects: none until the lifespan runs. Role: the single wiring point."""

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        """What: load policy, build clock and database, log versions; close the pool at shutdown.
        Use when: the server starts and stops. Inputs: app. Output: none.
        Side effects: reads policy.yaml, opens connections. Role: startup validation (PolicyError stops the process)."""
        policy = load_policy(settings.policy_path)
        app.state.policy = policy
        app.state.clock = clock or SystemClock(policy.timezone)
        app.state.db = Database(settings.db_url) if settings.db_url else None
        if app.state.db:
            app.state.db.open()
        log.info("core starting contract_version=%s policy_version=%s demo_faults=%s",
                 VERSION, policy.policy_version, settings.demo_faults)
        try:
            yield
        finally:
            if app.state.db:
                app.state.db.close()

    app = FastAPI(title="quillstack-core", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.settings = settings
    install_handlers(app)

    @app.middleware("http")
    async def correlation(request: Request, call_next):  # type: ignore[no-untyped-def]
        """What: read or mint X-Request-Id and X-Trace-Id, store them, echo them on the response.
        Use when: every request. Inputs: request, next handler. Output: response with both headers.
        Side effects: sets request.state. Role: doc 07 s1.2 log correlation."""
        request.state.request_id = clean_id(request.headers.get("x-request-id")) or new_uuid7()
        request.state.trace_id = clean_id(request.headers.get("x-trace-id")) or new_uuid7()
        response = await call_next(request)
        response.headers["X-Request-Id"] = request.state.request_id
        response.headers["X-Trace-Id"] = request.state.trace_id
        return response

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        """What: liveness probe. Use when: Compose healthcheck. Inputs: none. Output: {"status": "alive"}.
        Side effects: none. Role: doc 07 s3.6 (no auth)."""
        return {"status": "alive"}

    @app.get("/readyz")
    def readyz(request: Request) -> JSONResponse:
        """What: readiness with versions, the fault flag and the seed date; 503 if the database fails.
        Use when: Compose healthcheck, deploy checks. Inputs: request. Output: CoreReady JSON.
        Side effects: one SELECT when a database is configured. Role: doc 07 s3.6 (no auth)."""
        db: Database | None = request.app.state.db
        try:
            seed = db.seed_date() if db else "unseeded"
        except Exception as exc:  # noqa: BLE001 (any driver failure means not ready)
            log.warning("readyz database check failed: %s", type(exc).__name__)
            raise CoreError("TOOL_ERROR", "The database is unavailable.", retry_after_s=5,
                            klass="db_unavailable") from exc
        body = CoreReady(status="ready", contract_version=VERSION,
                         policy_version=request.app.state.policy.policy_version,
                         demo_faults=settings.demo_faults, seed_date=seed)
        return JSONResponse(body.model_dump(mode="json"))

    register_routes(app)
    return app
