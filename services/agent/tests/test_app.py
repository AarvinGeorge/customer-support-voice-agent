"""What: tests for the agent FastAPI skeleton (doc 07 s1.2 headers, s1.3 envelope, s2.2 health rows).
Use when: pytest (in-process ASGI, dependency probes faked). Inputs: none. Output: pass/fail.
Side effects: none. Role: test for agent.main and agent.web."""
from __future__ import annotations

import uuid

import httpx
import pytest

from contracts import VERSION
from contracts.agent_api import AgentReady
from contracts.errors import ErrorEnvelope
from agent.access import sign_cookie
from agent.main import create_app
from agent.settings import Settings

KEY = "k" * 32


def settings(**kw) -> Settings:
    """What: test settings. Use when: building an app. Inputs: overrides. Output: Settings.
    Side effects: none. Role: helper."""
    base = dict(served_by="agent_a", agent_db_url="postgresql://x", redis_url="redis://x",
                core_url="http://core:8000", knowledge_url="http://knowledge:8000", demo_faults_enabled=True)
    base.update(kw)
    return Settings(**base)


def probes(**results):
    """What: fake dependency probes returning fixed states. Use when: readyz tests.
    Inputs: name=state ("ok" or an error word). Output: dict of async callables. Side effects: none. Role: fake."""
    def make(v):
        async def probe() -> str:
            """What: fixed probe. Use when: readyz. Inputs: none. Output: state. Side effects: none. Role: fake."""
            if isinstance(v, Exception):
                raise v
            return v
        return probe
    base = {"redis": "ok", "postgres": "ok", "core": "ok", "knowledge": "ok"}
    base.update(results)
    return {k: make(v) for k, v in base.items()}


def client(app) -> httpx.AsyncClient:
    """What: in-process client. Use when: tests. Inputs: app. Output: AsyncClient. Side effects: none. Role: helper."""
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


async def test_healthz_alive_with_served_by_request_and_trace_ids():
    async with client(create_app(settings(), probes=probes())) as c:
        r = await c.get("/api/healthz")
    assert r.status_code == 200 and r.json() == {"status": "alive"}
    assert r.headers["X-Served-By"] == "agent_a"
    assert uuid.UUID(r.headers["X-Request-Id"]).version == 4
    assert uuid.UUID(r.headers["X-Trace-Id"]).version == 7


async def test_request_id_from_browser_is_echoed_and_bad_one_is_replaced():
    rid = str(uuid.uuid4())
    async with client(create_app(settings(), probes=probes())) as c:
        ok = await c.get("/api/healthz", headers={"X-Request-Id": rid})
        bad = await c.get("/api/healthz", headers={"X-Request-Id": "not-a-uuid\r\nx"})
    assert ok.headers["X-Request-Id"] == rid
    assert bad.headers["X-Request-Id"] != "not-a-uuid\r\nx"
    assert uuid.UUID(bad.headers["X-Request-Id"]).version == 4


async def test_readyz_reports_every_field_when_all_dependencies_are_up():
    async with client(create_app(settings(), probes=probes())) as c:
        r = await c.get("/api/readyz")
    assert r.status_code == 200
    body = AgentReady.model_validate(r.json())
    assert body.model_dump() == {"status": "ready", "contract_version": VERSION, "redis": "ok",
                                 "postgres": "ok", "core": "ok", "knowledge": "ok", "demo_faults": True}


@pytest.mark.parametrize("dep", ["postgres", "redis"])
async def test_readyz_is_503_when_an_own_store_is_down(dep):
    async with client(create_app(settings(), probes=probes(**{dep: TimeoutError()}))) as c:
        r = await c.get("/api/readyz")
    assert r.status_code == 503
    body = AgentReady.model_validate(r.json())
    assert body.status == "not_ready" and getattr(body, dep) == "timeout"


async def test_readyz_core_or_knowledge_down_is_degraded_not_503():
    app = create_app(settings(demo_faults_enabled=False), probes=probes(core=ConnectionError("x"), knowledge="http_503"))
    async with client(app) as c:
        r = await c.get("/api/readyz")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "degraded" and body["core"] == "unavailable" and body["knowledge"] == "http_503"
    assert body["demo_faults"] is False


async def test_unknown_path_returns_the_error_envelope_with_trace_id():
    async with client(create_app(settings(session_signing_key=KEY), probes=probes())) as c:
        c.cookies.set("qs_access", sign_cookie(KEY))  # the access gate answers 401 before routing otherwise
        r = await c.get("/api/nope")
    assert r.status_code == 404
    env = ErrorEnvelope.model_validate(r.json())
    assert env.error.class_ == "not_found" and env.error.code == "NOT_FOUND"
    assert env.error.retryable is False and env.error.changed is False and env.error.fault_injected is False
    assert env.error.trace_id == r.headers["X-Trace-Id"]
    assert r.headers["X-Served-By"] == "agent_a"


async def test_wrong_method_returns_the_envelope():
    async with client(create_app(settings(), probes=probes())) as c:
        r = await c.post("/api/healthz")
    assert r.status_code == 405
    assert ErrorEnvelope.model_validate(r.json()).error.class_ == "validation"


async def test_unhandled_exception_returns_envelope_not_a_stack_trace():
    app = create_app(settings(session_signing_key=KEY), probes=probes())

    @app.get("/api/boom")
    async def boom() -> None:
        """What: raises. Use when: this test. Inputs: none. Output: none. Side effects: none. Role: probe."""
        raise RuntimeError("secret internals")

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
                                 base_url="http://test", cookies={"qs_access": sign_cookie(KEY)}) as c:
        r = await c.get("/api/boom")
    assert r.status_code == 500
    assert "secret internals" not in r.text
    env = ErrorEnvelope.model_validate(r.json())
    assert env.error.code == "INTERNAL" and env.error.changed is None and env.error.retryable is True
    assert env.error.class_ == "core_unavailable"  # closest internal class; never a provider_* class
    assert r.headers["X-Served-By"] == "agent_a"


def test_settings_from_env_reads_compose_names(monkeypatch):
    for k, v in {"SERVED_BY": "agent_b", "AGENT_DB_URL": "postgresql://a", "REDIS_URL": "redis://r",
                 "CORE_URL": "http://c", "KNOWLEDGE_URL": "http://k", "DEMO_FAULTS_ENABLED": "false"}.items():
        monkeypatch.setenv(k, v)
    s = Settings.from_env()
    assert (s.served_by, s.agent_db_url, s.redis_url, s.core_url, s.knowledge_url, s.demo_faults_enabled) == \
        ("agent_b", "postgresql://a", "redis://r", "http://c", "http://k", False)
