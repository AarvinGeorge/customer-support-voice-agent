"""What: the knowledge service app: bearer auth, scopes, envelope errors, trace ids, /healthz, /readyz, /v1/corpus.
Use when: uvicorn imports `knowledge.main:app` (Compose command and dev reload).
Inputs: env KNOWLEDGE_TOKEN_AGENT, KNOWLEDGE_DB_URL, optional CALIBRATION_PATH (Gate 1 file, doc 07 s13.3). Output: ASGI app.
Side effects: lifespan checks the token is set (fails loudly) and creates the Database handle.
Role: doc 07 s4: skeleton, hybrid search and Gate 1; chunk lookup arrives in a later task."""
from __future__ import annotations

import logging
import os
import re
import time
import uuid
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Request

from contracts.knowledge_api import SearchRequest, SearchResponse
from knowledge import auth, config, db, errors, gate, search
from knowledge.embeddings import EmbeddingError, OpenAIEmbedder
from knowledge.ids import uuid7

log = logging.getLogger("knowledge")
_UUID = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")


def configure_logging() -> None:
    """What: make the 'knowledge' logger emit INFO to stderr once. Use when: create_app. Inputs: none.
    Output: none. Side effects: sets the logger level and adds one stream handler if none exists.
    Role: uvicorn configures only its own loggers, so without this the per-request trace id line was dropped (review PR9 I1)."""
    lg = logging.getLogger("knowledge")
    lg.setLevel(logging.INFO)
    if not lg.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
        lg.addHandler(handler)


def _id_or(value: str | None, make) -> str:
    """What: accept an inbound id only when it is a uuid, else mint one. Use when: reading X-Trace-Id and X-Request-Id.
    Inputs: header value or None, a minting function. Output: id string. Side effects: none.
    Role: keeps hostile or oversized ids out of logs and responses (review PR9 M3)."""
    return value if value and _UUID.fullmatch(value) else make()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """What: startup and shutdown hook. Use when: the server starts. Inputs: app.
    Output: yields while serving. Side effects: raises RuntimeError if a token is unset; sets app.state.db and app.state.calibration (None when
    the file is absent; a bad file stops startup with CalibrationError).
    Role: fail loudly instead of serving without auth."""
    auth.check_configured()
    if not os.environ.get("KNOWLEDGE_DB_URL"):
        raise RuntimeError("KNOWLEDGE_DB_URL is not set")
    app.state.db = db.Database(os.environ.get("KNOWLEDGE_DB_URL", ""))
    cal_path = gate.calibration_path()
    app.state.calibration = gate.load_calibration(cal_path)
    cal = app.state.calibration
    if cal is None:
        log.error("calibration file %s is missing: every search verdict will be 'uncalibrated' (the agent treats it as "
                  "weak) and /readyz reports calibration_status none", cal_path)
    else:
        log.info("calibration %s status=%s tau_low=%s tau_high=%s corpus_version=%s", cal.calibration_id, cal.status,
                 cal.tau_low, cal.tau_high, cal.corpus_version[:12])
    try:
        app.state.embedder = OpenAIEmbedder()
    except EmbeddingError as exc:  # no key: search runs keyword-only instead of the service refusing to start
        log.warning("embedder unavailable, search will be keyword-only: %s", exc)
        app.state.embedder = None
    yield


def create_app() -> FastAPI:
    """What: build the FastAPI app. Use when: uvicorn import and tests. Inputs: none. Output: FastAPI app.
    Side effects: none until it is served. Role: factory so tests get a fresh app."""
    configure_logging()
    app = FastAPI(title="quillstack-knowledge", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    errors.install(app)

    @app.middleware("http")
    async def trace(request: Request, call_next):
        """What: set request and trace ids, echo them, log one line. Use when: every request.
        Inputs: request. Output: response with X-Trace-Id and X-Request-Id. Side effects: logs.
        Role: doc 07 s1.2; the trace id is in every log line."""
        request.state.trace_id = _id_or(request.headers.get("X-Trace-Id"), uuid7)
        request.state.request_id = _id_or(request.headers.get("X-Request-Id"), lambda: str(uuid.uuid4()))
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
    def readyz(request: Request) -> dict:
        """What: readiness: database answers and at least one chunk is ingested. Use when: callers and checks.
        Inputs: request. Output: {"status": "ready", "database": "ok", "corpus_version", "chunks", "embedding_model"}
        calibration_status, or a 503 envelope (details carry chunks, or the model mismatch). Side effects: two SELECTs.
        Role: doc 07 s4.2: a calibration file whose embedding model differs from the configured one is not ready."""
        st = request.app.state.db.status()
        if st is None:
            raise errors.ApiError(503, "RETRIEVAL_UNAVAILABLE", "db_unavailable", "The knowledge database is not reachable.",
                                  retryable=True, retry_after_s=2)
        if st["chunks"] < 1:
            raise errors.ApiError(503, "RETRIEVAL_UNAVAILABLE", "retrieval_failed",
                                  "The knowledge base has no content yet.", retryable=True, retry_after_s=5,
                                  details={"chunks": st["chunks"]})
        cal = request.app.state.calibration
        if cal is not None and cal.embedding_model != config.embedding_model():
            raise errors.ApiError(503, "RETRIEVAL_UNAVAILABLE", "retrieval_failed",
                                  "The calibration file was made for a different embedding model.", retryable=False,
                                  details={"calibration_model": cal.embedding_model,
                                           "embedding_model": config.embedding_model()})
        return {"status": "ready", "database": "ok", "corpus_version": st["corpus_version"], "chunks": st["chunks"],
                "embedding_model": config.embedding_model(),
                "calibration_status": cal.status if cal else "none"}

    @app.post("/v1/search", response_model=SearchResponse, dependencies=[Depends(auth.require_scope("search"))])
    async def search_route(req: SearchRequest, request: Request) -> SearchResponse:
        """What: hybrid search. Use when: the agent's search_kb wrapper or the calibration script calls it.
        Inputs: SearchRequest body, bearer token with scope search. Output: SearchResponse (200), 401, 422 or 503
        envelopes. Side effects: one embedding call, two SQL queries. Role: doc 07 s4.1."""
        return await search.run_search(req, request.app.state.db, request.app.state.embedder,
                                       request.state.request_id, request.state.trace_id, request.app.state.calibration)

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
