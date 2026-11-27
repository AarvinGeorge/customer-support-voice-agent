"""What: the knowledge service app: bearer auth, scopes, envelope errors, trace ids, /healthz, /readyz, /v1/corpus.
Use when: uvicorn imports `knowledge.main:app` (Compose command and dev reload).
Inputs: env KNOWLEDGE_TOKEN_AGENT, KNOWLEDGE_DB_URL. Output: ASGI app.
Side effects: lifespan checks the token is set (fails loudly) and creates the Database handle.
Role: doc 07 s4 skeleton from CS-E-02.S1.T1; search (S2.T2), chunks and ingest arrive in later tasks."""
from __future__ import annotations

import logging
import os
import time
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Request

from knowledge import auth, db, errors
from knowledge.ids import uuid7

log = logging.getLogger("knowledge")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """What: startup and shutdown hook. Use when: the server starts. Inputs: app.
    Output: yields while serving. Side effects: raises RuntimeError if a token is unset; sets app.state.db.
    Role: fail loudly instead of serving without auth."""
    auth.check_configured()
    app.state.db = db.Database(os.environ.get("KNOWLEDGE_DB_URL", ""))
    yield


def create_app() -> FastAPI:
    """What: build the FastAPI app. Use when: uvicorn import and tests. Inputs: none. Output: FastAPI app.
    Side effects: none until it is served. Role: factory so tests get a fresh app."""
    app = FastAPI(title="quillstack-knowledge", lifespan=lifespan)
    errors.install(app)

    @app.middleware("http")
    async def trace(request: Request, call_next):
        """What: set request and trace ids, echo them, log one line. Use when: every request.
        Inputs: request. Output: response with X-Trace-Id and X-Request-Id. Side effects: logs.
        Role: doc 07 s1.2; the trace id is in every log line."""
        request.state.trace_id = request.headers.get("X-Trace-Id") or uuid7()
        request.state.request_id = request.headers.get("X-Request-Id") or uuid7()
        started = time.monotonic()
        response = await call_next(request)
        response.headers["X-Trace-Id"] = request.state.trace_id
        response.headers["X-Request-Id"] = request.state.request_id
        log.info("trace_id=%s request_id=%s %s %s %s %dms", request.state.trace_id, request.state.request_id,
                 request.method, request.url.path, response.status_code, (time.monotonic() - started) * 1000)
        return response

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        """What: liveness. Use when: Compose or a caller checks the process. Inputs: none.
        Output: {"status": "alive"}. Side effects: none. Role: doc 07 s4.2."""
        return {"status": "alive"}

    @app.get("/readyz")
    def readyz(request: Request) -> dict[str, str]:
        """What: readiness (minimal: the database answers). Use when: Compose healthcheck.
        Inputs: request. Output: {"status": "ready", "database": "ok"} or a 503 envelope.
        Side effects: one SELECT 1. Role: doc 07 s4.2; chunk and calibration checks arrive with CS-E-02.S1.T6."""
        if not request.app.state.db.ping():
            raise errors.ApiError(503, "RETRIEVAL_UNAVAILABLE", "db_unavailable", "The knowledge database is not reachable.",
                                  retryable=True, retry_after_s=2)
        return {"status": "ready", "database": "ok"}

    @app.get("/v1/corpus", dependencies=[Depends(auth.require_scope("corpus.read"))])
    def corpus(request: Request) -> dict:
        """What: list the corpus. Use when: the agent or evals need versions and article counts.
        Inputs: bearer token with corpus.read. Output: {corpus_version, articles, last_ingest_at}.
        Side effects: reads the database; a DB error returns 503. Role: doc 07 s4.2."""
        try:
            return request.app.state.db.corpus()
        except Exception as exc:  # psycopg errors; the body never carries the exception text
            log.warning("trace_id=%s corpus read failed: %s", request.state.trace_id, type(exc).__name__)
            raise errors.ApiError(503, "RETRIEVAL_UNAVAILABLE", "db_unavailable",
                                  "The knowledge database is not reachable.", retryable=True, retry_after_s=2)

    return app


app = create_app()
