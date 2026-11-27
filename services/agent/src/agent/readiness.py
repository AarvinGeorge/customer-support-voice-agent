"""What: dependency probes and the /api/readyz body (doc 07 s2.2 last row, s8.1 timeouts).
Use when: GET /api/readyz; Compose healthcheck and Caddy health checks call it.
Inputs: probe callables (real ones built from Settings and the app's clients; tests inject fakes).
Output: build_probes(...), readiness(...) -> (http status, AgentReady).
Side effects: real probes send one query or request per dependency.
Role: readiness. Policy (doc 07 silent, recorded in the report): postgres and redis are the agent's own
stores, so either down gives 503 (Caddy routes to the other replica); core or knowledge down gives 200
"degraded" because it is shared by both replicas and the agent still answers with fixed lines and tickets.
"""
from __future__ import annotations

import asyncio
from typing import Any, Awaitable, Callable

import httpx
from contracts import VERSION, limits
from contracts.agent_api import AgentReady

Probe = Callable[[], Awaitable[str]]
OWN_STORES = ("postgres", "redis")


async def _run(probe: Probe, timeout_s: float) -> str:
    """What: run one probe under a timeout and map failures to a short state word.
    Use when: readiness. Inputs: probe, timeout. Output: "ok", "timeout", "unavailable" or the probe's word.
    Side effects: the probe's. Role: helper."""
    try:
        return await asyncio.wait_for(probe(), timeout_s)
    except (TimeoutError, asyncio.TimeoutError):
        return "timeout"
    except Exception:  # noqa: BLE001 - any failure is "unavailable" for readiness
        return "unavailable"


async def readiness(probes: dict[str, Probe], demo_faults: bool) -> tuple[int, AgentReady]:
    """What: run all probes concurrently and build the readyz body.
    Use when: GET /api/readyz. Inputs: probes keyed redis, postgres, core, knowledge; fault flag.
    Output: (200 or 503, AgentReady) with status ready, degraded or not_ready. Side effects: probes.
    Role: readiness policy (module docstring)."""
    timeouts = {"redis": limits.REDIS_TIMEOUT_S * 3, "postgres": limits.POSTGRES_CHECKPOINTER_TIMEOUT_S,
                "core": limits.CORE_READ_TIMEOUT_S, "knowledge": limits.CORE_READ_TIMEOUT_S}
    names = ("redis", "postgres", "core", "knowledge")
    states = dict(zip(names, await asyncio.gather(*(_run(probes[n], timeouts[n]) for n in names))))
    if any(states[n] != "ok" for n in OWN_STORES):
        status, code = "not_ready", 503
    elif any(states[n] != "ok" for n in ("core", "knowledge")):
        status, code = "degraded", 200
    else:
        status, code = "ready", 200
    return code, AgentReady(status=status, contract_version=VERSION, demo_faults=demo_faults, **states)


def build_probes(*, pg_pool: Any, redis_client: Any, http: httpx.AsyncClient, core_url: str,
                 knowledge_url: str) -> dict[str, Probe]:
    """What: real probes: SELECT 1 on the agent_app pool, Redis PING, core and knowledge GET /readyz.
    Use when: app lifespan. Inputs: psycopg AsyncConnectionPool, redis asyncio client, shared httpx client,
    base URLs. Output: probe dict. Side effects: none until called. Role: production probes.
    A dependency answering non-200 reports "http_<status>"."""

    async def postgres() -> str:
        """What: SELECT 1. Use when: readyz. Inputs: none. Output: "ok". Side effects: one query. Role: probe."""
        async with pg_pool.connection() as conn:
            await conn.execute("SELECT 1")
        return "ok"

    async def redis() -> str:
        """What: PING. Use when: readyz. Inputs: none. Output: "ok". Side effects: one command. Role: probe."""
        await redis_client.ping()
        return "ok"

    def http_probe(url: str) -> Probe:
        """What: GET {url}/readyz probe factory. Use when: core and knowledge. Inputs: base URL.
        Output: probe. Side effects: none. Role: helper."""
        async def probe() -> str:
            """What: GET readyz. Use when: readyz. Inputs: none. Output: "ok" or "http_<status>".
            Side effects: one request. Role: probe."""
            r = await http.get(f"{url.rstrip('/')}/readyz")
            return "ok" if r.status_code == 200 else f"http_{r.status_code}"
        return probe

    return {"postgres": postgres, "redis": redis, "core": http_probe(core_url),
            "knowledge": http_probe(knowledge_url)}
