"""What: tests for the Redis sliding-window rate limits (doc 07 s9, s2.4 Counting): the window maths, the 429 envelope,
fail open when Redis is down, client IP resolution behind Caddy, and every built route's row.
Use when: pytest (fakeredis stands in for Redis; in-process ASGI; no network). CI runs these in the python job.
Inputs: none. Output: pass or fail. Side effects: none.
Role: Done when of CS-E-07.S2.T1 (the 11th turn in a minute gets 429 SESSION_TURNS; the Caddy-forwarded IP resolves
and a spoofed header from outside the edge subnet is ignored)."""
from __future__ import annotations

import logging
from types import SimpleNamespace
from typing import Any

import fakeredis
import httpx
from contracts import limits as L
from contracts.errors import ErrorEnvelope
from redis.exceptions import ConnectionError as RedisConnectionError

from agent.access import sign_cookie
from agent.limits.ratelimit import RULES, RateLimiter, client_ip
from agent.main import create_app
from test_access_api import CODE, KEY, MemorySessions, settings

EDGE = "172.30.0.0/24"
SID = "019a7c2e-5b10-7000-8000-00000000b001"


class Clock:
    """What: a settable clock. Use when: window tests. Inputs: start. Output: callable returning seconds.
    Side effects: none. Role: test double for time.time."""

    def __init__(self, start: float = 1000.0) -> None:
        """What: set the start. Use when: tests. Inputs: start. Output: none. Side effects: none. Role: fake."""
        self.t = start

    def __call__(self) -> float:
        """What: current fake time. Use when: the limiter reads the clock. Inputs: none. Output: float.
        Side effects: none. Role: fake."""
        return self.t


class DownRedis:
    """What: a Redis client whose every call fails. Use when: the fail-open test. Inputs: none.
    Output: raises ConnectionError from pipeline and zrem. Side effects: none. Role: test double."""

    def pipeline(self, transaction: bool = True) -> Any:
        """What: refuse. Use when: the limiter opens a pipeline. Inputs: transaction flag. Output: raises.
        Side effects: none. Role: fake."""
        raise RedisConnectionError("redis is down")


def fresh_redis() -> Any:
    """What: an empty async fakeredis. Use when: each test. Inputs: none. Output: client. Side effects: none.
    Role: helper."""
    return fakeredis.FakeAsyncRedis()


# ---- the window ----

async def test_eleventh_hit_in_a_minute_is_rejected_and_the_first_ten_pass():
    clock = Clock()
    lim = RateLimiter(fresh_redis(), clock=clock)
    for i in range(10):
        clock.t += 1
        assert await lim.hit(RULES["SESSION_TURNS"], "s1") is None, f"hit {i + 1} should pass"
    clock.t += 1
    rej = await lim.hit(RULES["SESSION_TURNS"], "s1")
    assert rej is not None and rej.retry_after_s >= 1


async def test_retry_after_is_ceil_of_the_oldest_hit_leaving_the_window_and_min_one():
    clock = Clock()
    lim = RateLimiter(fresh_redis(), clock=clock)
    for _ in range(10):
        await lim.hit(RULES["SESSION_TURNS"], "s1")  # all at t=1000.0
    clock.t = 1010.5
    rej = await lim.hit(RULES["SESSION_TURNS"], "s1")
    assert rej is not None and rej.retry_after_s == 50  # ceil(1060.0 - 1010.5) = 50
    clock.t = 1059.99
    rej = await lim.hit(RULES["SESSION_TURNS"], "s1")
    assert rej is not None and rej.retry_after_s == 1


async def test_window_slides_so_a_slot_frees_when_the_oldest_hit_ages_out():
    clock = Clock()
    lim = RateLimiter(fresh_redis(), clock=clock)
    for _ in range(10):
        await lim.hit(RULES["SESSION_TURNS"], "s1")
    assert await lim.hit(RULES["SESSION_TURNS"], "s1") is not None
    clock.t += 61
    assert await lim.hit(RULES["SESSION_TURNS"], "s1") is None


async def test_rejected_hits_do_not_extend_the_window():
    clock = Clock()
    lim = RateLimiter(fresh_redis(), clock=clock)
    for _ in range(10):
        await lim.hit(RULES["SESSION_TURNS"], "s1")
    for _ in range(50):
        clock.t += 1  # hammering while blocked
        assert await lim.hit(RULES["SESSION_TURNS"], "s1") is not None or clock.t > 1060
    clock.t = 1061
    assert await lim.hit(RULES["SESSION_TURNS"], "s1") is None


async def test_subjects_and_rules_are_independent():
    lim = RateLimiter(fresh_redis(), clock=Clock())
    for _ in range(10):
        await lim.hit(RULES["SESSION_TURNS"], "s1")
    assert await lim.hit(RULES["SESSION_TURNS"], "s1") is not None
    assert await lim.hit(RULES["SESSION_TURNS"], "s2") is None
    assert await lim.hit(RULES["IP_HOURLY"], "s1") is None


def test_rule_table_matches_doc_07_s9():
    assert (RULES["SESSION_TURNS"].limit, RULES["SESSION_TURNS"].window_s) == (L.SESSION_TURNS_PER_MINUTE, 60)
    assert (RULES["IP_HOURLY"].limit, RULES["IP_HOURLY"].window_s) == (L.IP_TURNS_PER_HOUR, 3600)
    assert (RULES["IP_SESSIONS"].limit, RULES["IP_SESSIONS"].window_s) == (L.IP_SESSIONS_PER_HOUR, 3600)
    assert (RULES["SESSION_POSTS"].limit, RULES["SESSION_POSTS"].window_s) == (L.SESSION_POSTS_PER_HOUR, 3600)
    assert (RULES["VOICE_SESSIONS"].limit, RULES["VOICE_SESSIONS"].window_s) == (L.IP_VOICE_TOKENS_PER_HOUR, 3600)
    assert (RULES["ACCESS_ATTEMPTS"].limit, RULES["ACCESS_ATTEMPTS"].window_s) == (L.IP_ACCESS_ATTEMPTS_PER_HOUR, 3600)
    assert (RULES["OPERATOR_ATTEMPTS"].limit, RULES["OPERATOR_ATTEMPTS"].window_s) == (5, 900)
    assert RULES["SESSION_TURNS"].label == "10/minute" and RULES["SESSION_TURNS"].scope == "session"
    assert RULES["IP_HOURLY"].label == "60/hour" and RULES["IP_HOURLY"].scope == "ip"
    assert RULES["OPERATOR_ATTEMPTS"].label == "5/15 minutes"


async def test_fail_open_when_redis_raises_and_it_is_logged(caplog):
    lim = RateLimiter(DownRedis(), clock=Clock())
    with caplog.at_level(logging.WARNING, logger="agent.limits"):
        assert await lim.hit(RULES["SESSION_TURNS"], "s1") is None
    assert any("failing open" in r.getMessage() for r in caplog.records)


# ---- client IP ----

def req(peer: str | None, xff: str | None, trusted: str = EDGE) -> Any:
    """What: a stand-in Request with client, headers and settings. Use when: client_ip tests.
    Inputs: socket peer, X-Forwarded-For, trusted list. Output: namespace. Side effects: none. Role: helper."""
    headers = {"x-forwarded-for": xff} if xff is not None else {}
    app = SimpleNamespace(state=SimpleNamespace(settings=SimpleNamespace(forwarded_allow_ips=trusted)))
    return SimpleNamespace(client=SimpleNamespace(host=peer) if peer else None, headers=headers, app=app)


def test_ip_comes_from_xff_when_the_peer_is_caddy_on_the_edge_subnet():
    assert client_ip(req("172.30.0.2", "203.0.113.9")) == "203.0.113.9"


def test_spoofed_xff_from_outside_the_edge_subnet_is_ignored():
    assert client_ip(req("198.51.100.7", "203.0.113.9")) == "198.51.100.7"


def test_spoofed_leading_entry_behind_caddy_is_not_trusted_rightmost_untrusted_wins():
    # Caddy appended the real client; the attacker's own header value sits to the left.
    assert client_ip(req("172.30.0.2", "1.2.3.4, 203.0.113.9")) == "203.0.113.9"
    assert client_ip(req("172.30.0.2", "203.0.113.9, 172.30.0.7")) == "203.0.113.9"


def test_missing_or_garbage_xff_or_peer_falls_back_safely():
    assert client_ip(req("172.30.0.2", None)) == "172.30.0.2"
    assert client_ip(req("172.30.0.2", "not-an-ip")) == "172.30.0.2"
    assert client_ip(req(None, "203.0.113.9")) == "unknown"
    assert client_ip(req("172.30.0.2", "203.0.113.9", trusted="")) == "172.30.0.2"


def test_trusted_list_accepts_commas_single_ips_and_star():
    assert client_ip(req("10.0.0.5", "203.0.113.9", trusted="10.0.0.5, 172.30.0.0/24")) == "203.0.113.9"
    assert client_ip(req("8.8.8.8", "203.0.113.9", trusted="*")) == "203.0.113.9"


# ---- routes ----

def app_with_redis(redis: Any, **kw):
    """What: app with fake probes, an in-memory session store, one owned session and the given Redis.
    Use when: route tests. Inputs: redis client, settings overrides. Output: FastAPI app.
    Side effects: none. Role: helper."""
    async def ok() -> str:
        """What: healthy probe. Use when: readyz. Inputs: none. Output: "ok". Side effects: none. Role: fake."""
        return "ok"
    app = create_app(settings(forwarded_allow_ips=EDGE, **kw), probes={k: ok for k in ("redis", "postgres", "core", "knowledge")},
                     session_store=MemorySessions())
    app.state.redis = redis
    return app


def http(app: Any, peer: tuple[str, int] = ("172.30.0.2", 5000), **kw: Any) -> httpx.AsyncClient:
    """What: in-process client whose ASGI socket peer is `peer` (the Caddy container by default).
    Use when: route tests. Inputs: app, peer. Output: AsyncClient. Side effects: none. Role: helper."""
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app, client=peer), base_url="https://test", **kw)


def cookie() -> dict[str, str]:
    """What: a valid qs_access cookie header value. Use when: authenticated calls. Inputs: none.
    Output: cookies dict. Side effects: none. Role: helper."""
    return {"qs_access": sign_cookie(KEY)}


async def own_session(app: Any, c: httpx.AsyncClient) -> str:
    """What: create a session through the API. Use when: turn and voice tests. Inputs: app, client.
    Output: session id. Side effects: stores a row and hits the IP_SESSIONS window once. Role: helper."""
    r = await c.post("/api/sessions", json={})
    assert r.status_code == 201, r.text
    return r.json()["session_id"]


async def test_eleventh_turn_in_a_minute_gets_429_session_turns_with_envelope_and_header():
    app = app_with_redis(fresh_redis())
    async with http(app, cookies=cookie()) as c:
        sid = await own_session(app, c)
        for i in range(10):
            r = await c.post(f"/api/sessions/{sid}/turns", json={})
            assert r.status_code != 429, f"turn {i + 1}: {r.status_code} {r.text}"
        r = await c.post(f"/api/sessions/{sid}/turns", json={})
    assert r.status_code == 429
    env = ErrorEnvelope.model_validate(r.json())
    assert env.error.class_ == "rate_limited" and env.error.code == "SESSION_TURNS"
    assert env.error.retryable is True and env.error.changed is False
    assert env.error.retry_after_s is not None and 1 <= env.error.retry_after_s <= 60
    assert r.headers["retry-after"] == str(env.error.retry_after_s)
    assert env.error.details == {"limit": "10/minute", "scope": "session"}
    assert env.error.trace_id == r.headers["x-trace-id"]


async def test_session_turn_window_is_per_session_and_per_visitor():
    app = app_with_redis(fresh_redis())
    async with http(app, cookies=cookie()) as c:
        a = await own_session(app, c)
        b = await own_session(app, c)
        for _ in range(11):
            await c.post(f"/api/sessions/{a}/turns", json={})
        assert (await c.post(f"/api/sessions/{a}/turns", json={})).json()["error"]["code"] == "SESSION_TURNS"
        assert (await c.post(f"/api/sessions/{b}/turns", json={})).status_code != 429


async def test_ip_hourly_row_on_turns_and_voice_ask_shares_one_window():
    redis = fresh_redis()
    app = app_with_redis(redis)
    async with http(app, cookies=cookie()) as c:
        sids = [await own_session(app, c) for _ in range(7)]
        # 7 sessions x 9 turns each = 63 calls, each session stays under 10/minute; IP crosses 60/hour.
        codes = []
        for sid in sids:
            for _ in range(9):
                r = await c.post(f"/api/sessions/{sid}/turns", json={})
                codes.append(r.json()["error"]["code"] if r.status_code == 429 else r.status_code)
        assert codes[:60].count("IP_HOURLY") == 0 and codes[60:] == ["IP_HOURLY"] * 3
        r = await c.post(f"/api/sessions/{sids[0]}/voice/ask", json={})
    assert r.status_code == 429 and r.json()["error"]["code"] == "IP_HOURLY"
    assert r.json()["error"]["details"] == {"limit": "60/hour", "scope": "ip"}


async def test_session_creation_is_limited_to_20_per_hour_per_ip():
    app = app_with_redis(fresh_redis())
    async with http(app, cookies=cookie()) as c:
        for _ in range(20):
            assert (await c.post("/api/sessions", json={})).status_code == 201
        r = await c.post("/api/sessions", json={})
    assert r.status_code == 429 and r.json()["error"]["code"] == "IP_SESSIONS"


async def test_access_attempts_are_limited_to_10_per_hour_per_ip_even_when_the_code_is_wrong():
    app = app_with_redis(fresh_redis())
    async with http(app) as c:
        for _ in range(10):
            assert (await c.post("/api/access", json={"code": "wrong"})).status_code == 401
        r = await c.post("/api/access", json={"code": CODE})  # the right code is also blocked
    assert r.status_code == 429 and r.json()["error"]["code"] == "ACCESS_ATTEMPTS"


async def test_voice_token_is_limited_to_3_per_hour_per_ip_before_any_mint():
    app = app_with_redis(fresh_redis())
    async with http(app, cookies=cookie()) as c:
        sid = await own_session(app, c)
        for _ in range(3):
            assert (await c.post(f"/api/sessions/{sid}/voice/token", json={})).status_code != 429
        r = await c.post(f"/api/sessions/{sid}/voice/token", json={})
    assert r.status_code == 429 and r.json()["error"]["code"] == "VOICE_SESSIONS"


async def test_transcript_posts_are_limited_to_120_per_hour_per_session():
    app = app_with_redis(fresh_redis())
    async with http(app, cookies=cookie()) as c:
        sid = await own_session(app, c)
        for _ in range(120):
            assert (await c.post(f"/api/sessions/{sid}/transcript", json={})).status_code != 429
        r = await c.post(f"/api/sessions/{sid}/transcript", json={})
    assert r.status_code == 429 and r.json()["error"]["code"] == "SESSION_POSTS"


async def test_caddy_forwarded_ip_is_what_the_ip_window_counts():
    redis = fresh_redis()
    app = app_with_redis(redis)
    async with http(app, peer=("172.30.0.2", 5000)) as c:  # Caddy on the edge subnet
        for _ in range(10):
            await c.post("/api/access", json={"code": "x"}, headers={"x-forwarded-for": "203.0.113.9"})
        blocked = await c.post("/api/access", json={"code": "x"}, headers={"x-forwarded-for": "203.0.113.9"})
        other = await c.post("/api/access", json={"code": "x"}, headers={"x-forwarded-for": "203.0.113.10"})
    assert blocked.status_code == 429  # counted against 203.0.113.9, not against Caddy's 172.30.0.2
    assert other.status_code == 401    # a different real client has its own window
    assert await redis.exists("rl:ACCESS_ATTEMPTS:203.0.113.9") == 1
    assert await redis.exists("rl:ACCESS_ATTEMPTS:172.30.0.2") == 0


async def test_spoofed_xff_from_outside_the_edge_subnet_cannot_dodge_or_frame_a_window():
    redis = fresh_redis()
    app = app_with_redis(redis)
    async with http(app, peer=("198.51.100.7", 4000)) as c:  # not Caddy
        for i in range(10):
            await c.post("/api/access", json={"code": "x"}, headers={"x-forwarded-for": f"203.0.113.{i}"})
        blocked = await c.post("/api/access", json={"code": "x"}, headers={"x-forwarded-for": "203.0.113.200"})
    assert blocked.status_code == 429  # rotating the header did not help
    assert await redis.exists("rl:ACCESS_ATTEMPTS:198.51.100.7") == 1
    assert await redis.exists("rl:ACCESS_ATTEMPTS:203.0.113.0") == 0


async def test_routes_fail_open_when_redis_is_down_and_without_a_redis_client(caplog):
    for redis in (DownRedis(), None):
        app = app_with_redis(redis)
        with caplog.at_level(logging.WARNING, logger="agent.limits"):
            async with http(app, cookies=cookie()) as c:
                sid = await own_session(app, c)
                codes = {(await c.post(f"/api/sessions/{sid}/turns", json={})).status_code for _ in range(12)}
        assert 429 not in codes
    assert any("failing open" in r.getMessage() for r in caplog.records)


async def test_health_and_other_routes_are_not_limited():
    app = app_with_redis(fresh_redis())
    async with http(app, cookies=cookie()) as c:
        for _ in range(30):
            assert (await c.get("/api/healthz")).status_code == 200
            assert (await c.get("/api/me")).status_code == 200
