"""What: the agent FastAPI app: headers, envelope, access gate, /api/healthz, /api/readyz, visitor routes.
Use when: uvicorn runs `agent.main:app` (Compose agent_a, agent_b); tests call create_app with fake probes.
Inputs: Settings (env); optional probes dict (tests).
Output: create_app(settings, probes) and the module-level ASGI `app`.
Side effects: the lifespan opens a psycopg pool (agent_app), the TrajectoryWriter (its own small pool), a Redis
client and an httpx client and closes them on shutdown; nothing connects at import time.
Role: composition root of the HTTP service: access gate, visitor routes, turn and voice routes, health. The agent
loop and the shared Ingress are built by agent.composition.wire_app in the lifespan; without them /turns and
voice/ask answer 503.
"""
from __future__ import annotations

import logging
from contextlib import AsyncExitStack, asynccontextmanager
from typing import Any, AsyncIterator

import httpx
from contracts import limits
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from agent.access import install_access_gate, install_not_owned_handler
from agent.api import install_api
from agent.composition import wire_app
from agent.limits.ratelimit import install_limits
from agent.assembly import CheckpointerFactory, postgres_checkpointer_factory
from agent.ingress import PgTurnStore
from agent.readiness import Probe, build_probes, readiness
from agent.routes.turns import install_turns
from agent.sessions import PgSessionStore, SessionStore
from agent.settings import Settings
from agent.trajectory import TrajectoryWriter
from agent.voice import PgVoiceStore, VoiceDeps, install_voice, thread_summary
from agent.web import install_http

log = logging.getLogger("agent.main")


def create_app(settings: Settings | None = None, probes: dict[str, Probe] | None = None,
               checkpointer_factory: CheckpointerFactory | None = None,
               session_store: SessionStore | None = None, voice: VoiceDeps | None = None,
               ingress: Any = None) -> FastAPI:
    """What: build the app.
    Use when: process start (no args) or tests (settings, fake probes, fake checkpointer factory).
    Inputs: Settings (default from env); probes keyed redis, postgres, core, knowledge (default: real
    probes built in the lifespan); checkpointer factory (default: the agent_app Postgres pool; with
    injected probes and no factory, no checkpointer is opened); session_store (tests; default
    PgSessionStore built in the lifespan); voice (tests; default VoiceDeps built in the lifespan with the
    Postgres voice store, the OpenAI minter and the voice role of models.yaml); ingress (tests; default built by
    agent.composition.wire_app in the lifespan).
    Output: FastAPI app. Side effects: none until the lifespan runs.
    Role: app factory."""
    cfg = settings or Settings.from_env()
    if checkpointer_factory is None and probes is None:
        checkpointer_factory = postgres_checkpointer_factory(cfg.agent_db_url)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        """What: open and close the checkpointer, and the dependency clients when probes were not injected.
        Use when: uvicorn startup and shutdown. Inputs: app. Output: none.
        Side effects: checkpointer pool, Redis and HTTP clients. Role: resource lifetime (the app owns the
        checkpointer, so a later route builds the agent from app.state.checkpointer)."""
        async with AsyncExitStack() as stack:
            app.state.checkpointer = None
            if checkpointer_factory is not None:
                app.state.checkpointer = await stack.enter_async_context(checkpointer_factory())
            if probes is None:
                await stack.enter_async_context(_clients(app))
            else:
                app.state.probes = probes
            yield

    @asynccontextmanager
    async def _clients(app: FastAPI) -> AsyncIterator[None]:
        """What: the agent_app pool, Redis and HTTP clients plus the real probes.
        Use when: lifespan without injected probes. Inputs: app. Output: none.
        Side effects: opens and closes the clients; installs the redacting log formatter and the hiding LangSmith
        client (doc 07 s11.1). Role: resource lifetime."""
        import redis.asyncio as aioredis

        from agent.logs import install_log_redaction
        from agent.tracing import configure_tracing
        install_log_redaction()
        configure_tracing()
        from psycopg_pool import AsyncConnectionPool

        pool = AsyncConnectionPool(cfg.agent_db_url, min_size=1, max_size=4, open=False,
                                   timeout=limits.POSTGRES_CHECKPOINTER_TIMEOUT_S,
                                   kwargs={"connect_timeout": int(limits.POSTGRES_CHECKPOINTER_TIMEOUT_S)})
        await pool.open(wait=False)  # never block startup on the database; readyz reports it
        rds = aioredis.from_url(cfg.redis_url, socket_timeout=limits.REDIS_TIMEOUT_S,
                                socket_connect_timeout=limits.REDIS_TIMEOUT_S)
        http = httpx.AsyncClient(timeout=limits.CORE_READ_TIMEOUT_S)
        app.state.pg_pool, app.state.redis, app.state.http = pool, rds, http
        app.state.session_store = session_store or PgSessionStore(pool)
        # doc 07 s12: turn_events go through their own small pool; a drop marks events_complete=false on this pool.
        trajectory = TrajectoryWriter(cfg.agent_db_url, on_incomplete=PgTurnStore(pool).mark_events_incomplete)
        await trajectory.start()
        app.state.trajectory = trajectory
        if voice is None:
            app.state.voice = _voice_deps(app, pool, http)
        if ingress is None:
            wire_app(app, cfg)
        app.state.probes = build_probes(pg_pool=pool, redis_client=rds, http=http, core_url=cfg.core_url,
                                        knowledge_url=cfg.knowledge_url)
        try:
            yield
        finally:
            await trajectory.aclose()
            await http.aclose()
            await rds.aclose()
            await pool.close()

    def _voice_deps(app: FastAPI, pool: Any, http: httpx.AsyncClient) -> VoiceDeps:
        """What: production voice dependencies. Use when: the lifespan. Inputs: app, pool, HTTP client.
        Output: VoiceDeps (voice_spec None when models.yaml does not load: voice/token answers 503, loudly logged).
        Side effects: reads models.yaml. Role: wiring; the Ingress is attached when the agent loop is wired."""
        from agent.models.config import load_models_config
        from agent.realtime import OpenAIRealtimeMinter
        try:
            spec = load_models_config(cfg.models_path).roles["voice"]
        except Exception:  # noqa: BLE001
            log.exception("models.yaml did not load from %s: voice is off", cfg.models_path)
            spec = None
        if not cfg.openai_api_key:
            log.error("OPENAI_API_KEY is not set: voice/token will answer 503")

        async def summary(sid: str, context: Any) -> Any:
            """What: summary from the app's checkpointer. Use when: voice/token. Inputs: sid, RedactContext.
            Output: summary. Side effects: one read. Role: closure."""
            return await thread_summary(app.state.checkpointer, sid, context)

        return VoiceDeps(store=PgVoiceStore(pool), minter=OpenAIRealtimeMinter(http, cfg.openai_api_key),
                         voice_spec=spec, summary=summary)

    app = FastAPI(title="quillstack-agent", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.settings = cfg
    if probes is not None:
        app.state.probes = probes
    app.state.session_store = session_store
    app.state.voice = voice
    app.state.ingress = ingress
    install_access_gate(app)  # before install_http: ids and headers wrap the gate
    install_http(app, cfg.served_by)
    install_not_owned_handler(app)
    install_limits(app)
    for name, value in ("SESSION_SIGNING_KEY", cfg.session_signing_key), ("DEMO_ACCESS_CODE", cfg.demo_access_code):
        if not value:
            log.error("%s is not set: nobody can sign in until it is", name)
    install_api(app)
    install_turns(app)
    install_voice(app)

    @app.get("/api/healthz")
    async def healthz() -> dict[str, str]:
        """What: liveness. Use when: a caller checks the process is up. Inputs: none.
        Output: {"status": "alive"}. Side effects: none. Role: doc 07 s2.2."""
        return {"status": "alive"}

    @app.get("/api/readyz")
    async def readyz(request: Request) -> Any:
        """What: readiness with contract version, dependency states and the fault flag.
        Use when: Compose healthcheck, Caddy, the drawer. Inputs: request.
        Output: AgentReady JSON, 200 (ready or degraded) or 503 (not_ready). Side effects: probes.
        Role: doc 07 s2.2 last row."""
        code, body = await readiness(request.app.state.probes, cfg.demo_faults_enabled)
        return JSONResponse(body.model_dump(mode="json"), status_code=code)

    return app


def __getattr__(name: str) -> Any:
    """What: lazy module attribute `app` so importing agent.main (tests) does not read the environment.
    Use when: uvicorn resolves `agent.main:app`. Inputs: attribute name. Output: the app.
    Side effects: builds the app once from env. Role: ASGI entry point."""
    if name == "app":
        global app
        app = create_app()
        return app
    raise AttributeError(name)
