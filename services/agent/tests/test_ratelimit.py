"""What: tests for the Redis sliding-window rate limits (doc 07 s9, s2.4 Counting): the window maths, the 429 envelope,
fail open when Redis is down, client IP resolution behind Caddy, and every built route's row.
Use when: pytest (fakeredis stands in for Redis; in-process ASGI; no network). CI runs these in the python job.
Inputs: none. Output: pass or fail. Side effects: none.
Role: Done when of CS-E-07.S2.T1 (the 11th turn in a minute gets 429 SESSION_TURNS; the Caddy-forwarded IP resolves
and a spoofed header from outside the edge subnet is ignored)."""
from __future__ import annotations

import logging
import uuid
from types import SimpleNamespace
from typing import Any

import fakeredis
import httpx
from contracts import limits as L
from contracts.errors import ErrorEnvelope
from langchain_core.messages import AIMessage
from redis.exceptions import ConnectionError as RedisConnectionError
from starlette.datastructures import Headers

from agent.access import sign_cookie
from agent.ingress import SessionRow
from agent.limits.ratelimit import RULES, RateLimiter, client_ip
from agent.main import create_app
from test_access_api import CODE, KEY, MemorySessions, settings
from test_turns_route import OTHER_SID, SID, VISITOR, World
from test_turns_route import settings as turn_settings

EDGE = "172.30.0.0/24"
SID_B = "019a7c2e-5b10-7000-8000-00000000b003"


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

def req(peer: str | None, xff: str | list[str] | None, trusted: str = EDGE) -> Any:
    """What: a stand-in Request with client, headers (one X-Forwarded-For line or several) and settings.
    Use when: client_ip tests. Inputs: socket peer, header value(s), trusted list. Output: namespace.
    Side effects: none. Role: helper."""
    lines = [xff] if isinstance(xff, str) else (xff or [])
    headers = Headers(raw=[(b"x-forwarded-for", v.encode()) for v in lines])
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


def test_ip_text_is_canonical_and_every_xff_header_line_is_read():
    assert client_ip(req("172.30.0.2", "2001:DB8:0:0:0:0:0:1")) == "2001:db8::1"
    assert client_ip(req("172.30.0.2", "2001:db8::1")) == "2001:db8::1"
    # two header lines are one list: the rightmost entry (Caddy's) wins, like uvicorn
    assert client_ip(req("172.30.0.2", ["6.6.6.6", "203.0.113.9"])) == "203.0.113.9"


def test_labels_are_derived_from_the_limit_and_window():
    for rule in RULES.values():
        unit = {60: "minute", 3600: "hour"}.get(rule.window_s, f"{rule.window_s // 60} minutes")
        assert rule.label == f"{rule.limit}/{unit}"


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


# Turn counting uses the real composed Ingress (test_turns_route.World): only a turn that starts counts (07 s2.4).

def turn_body(text: str = "hi") -> dict[str, Any]:
    """What: a valid TurnRequest body with a fresh client_turn_id. Use when: turn tests. Inputs: text.
    Output: dict. Side effects: none. Role: helper."""
    return {"client_turn_id": str(uuid.uuid4()), "text": text}


def world(n_replies: int = 0, redis: Any = None) -> World:
    """What: a World (real Ingress and agent graph, fake models) with fakeredis and the edge subnet trusted.
    Use when: turn tests. Inputs: number of scripted one-line answers, redis client. Output: World.
    Side effects: none. Role: helper."""
    w = World(*[AIMessage(content="Ok, happy to help with Quillstack.") for _ in range(n_replies)])
    w.app.state.settings = turn_settings(forwarded_allow_ips=EDGE)
    w.app.state.redis = redis if redis is not None else fresh_redis()
    return w


async def session_hits(redis: Any, visitor: str = VISITOR, sid: str = SID) -> int:
    """What: hits recorded in a session's SESSION_TURNS window. Use when: counting tests.
    Inputs: redis, visitor, sid. Output: int. Side effects: none. Role: helper."""
    return await redis.zcard(f"rl:SESSION_TURNS:{visitor}:{sid}")


async def test_eleventh_turn_in_a_minute_gets_429_session_turns_with_envelope_and_header():
    w = world(10)
    async with w.client() as c:
        for i in range(10):
            r = await c.post(f"/api/sessions/{SID}/turns", json=turn_body())
            assert r.status_code == 200, f"turn {i + 1}: {r.status_code} {r.text}"
        r = await c.post(f"/api/sessions/{SID}/turns", json=turn_body())
    assert r.status_code == 429
    env = ErrorEnvelope.model_validate(r.json())
    assert env.error.class_ == "rate_limited" and env.error.code == "SESSION_TURNS"
    assert env.error.retryable is True and env.error.changed is False
    assert env.error.retry_after_s is not None and 1 <= env.error.retry_after_s <= 60
    assert r.headers["retry-after"] == str(env.error.retry_after_s)
    assert env.error.details == {"limit": "10/minute", "scope": "session"}
    assert env.error.trace_id == r.headers["x-trace-id"]
    assert len(w.registry.primary.seen) == 10 and w.lock.held == {}  # the refused turn ran nothing, lock released


async def test_replays_do_not_count_toward_session_turns():
    redis = fresh_redis()
    w = world(1, redis)
    body = turn_body()
    async with w.client() as c:
        codes = [(await c.post(f"/api/sessions/{SID}/turns", json=body)).status_code for _ in range(13)]
    assert codes == [200] * 13  # one real turn, twelve replays
    assert await session_hits(redis) == 1 and len(w.registry.primary.seen) == 1


async def test_a_resend_at_a_full_window_still_gets_its_replay():
    w = world(10)
    first = turn_body()
    async with w.client() as c:
        await c.post(f"/api/sessions/{SID}/turns", json=first)
        for _ in range(9):
            await c.post(f"/api/sessions/{SID}/turns", json=turn_body())
        assert (await c.post(f"/api/sessions/{SID}/turns", json=turn_body())).status_code == 429
        again = await c.post(f"/api/sessions/{SID}/turns", json=first)
    assert again.status_code == 200 and "replayed" in again.text


async def test_the_capped_turn_does_not_count():
    redis = fresh_redis()
    w = world(0, redis)
    w.turns.sessions[SID] = SessionRow(session_id=SID, visitor_id=VISITOR, user_turns=40)
    async with w.client() as c:
        codes = [(await c.post(f"/api/sessions/{SID}/turns", json=turn_body())).status_code for _ in range(12)]
    assert codes == [200] * 12 and await session_hits(redis) == 0


async def test_requests_refused_before_a_turn_starts_do_not_count():
    redis = fresh_redis()
    w = world(0, redis)
    async with w.client() as c:
        bad = [(await c.post(f"/api/sessions/{SID}/turns", json={"client_turn_id": "x", "text": "hi"})).status_code
               for _ in range(12)]
        foreign = [(await c.post(f"/api/sessions/{OTHER_SID}/turns", json=turn_body())).status_code
                   for _ in range(12)]
        ghost = f"/api/sessions/{uuid.uuid4()}/turns"
        missing = [(await c.post(ghost, json=turn_body())).status_code for _ in range(12)]
    async with w.client(cookie=False) as anon:
        unauth = [(await anon.post(f"/api/sessions/{SID}/turns", json=turn_body())).status_code for _ in range(12)]
    assert set(bad) == {422} and set(foreign) == {404} and set(missing) == {404} and set(unauth) == {401}
    assert [k async for k in redis.scan_iter("rl:*")] == []  # neither window was touched


async def test_a_refused_voice_ask_before_ingress_does_not_count():
    redis = fresh_redis()
    w = world(0, redis)
    ask = {"client_turn_id": str(uuid.uuid4()), "voice_session_id": "nope", "realtime_call_id": "call-1",
           "question": "hi", "transcript_final": True, "last_assistant_interrupted": False}
    async with w.client() as c:
        codes = [(await c.post(f"/api/sessions/{SID}/voice/ask", json=ask)).status_code for _ in range(12)]
    assert set(codes) == {429}  # VOICE_SESSION_EXPIRED, step 3
    assert [k async for k in redis.scan_iter("rl:*")] == []


async def test_session_window_is_per_session_and_per_visitor():
    redis = fresh_redis()
    w = world(11, redis)
    w.turns.sessions[SID_B] = SessionRow(session_id=SID_B, visitor_id=VISITOR)
    async with w.client() as c:
        for _ in range(11):
            await c.post(f"/api/sessions/{SID}/turns", json=turn_body())
        a = await c.post(f"/api/sessions/{SID}/turns", json=turn_body())
        b = await c.post(f"/api/sessions/{SID_B}/turns", json=turn_body())
    assert a.status_code == 429 and b.status_code == 200


async def test_ip_hourly_row_is_one_window_across_sessions():
    redis = fresh_redis()
    w = world(60, redis)
    sids = [str(uuid.UUID(int=0x019A7C2E5B1070008000000000C00000 + i)) for i in range(7)]
    for s in sids:
        w.turns.sessions[s] = SessionRow(session_id=s, visitor_id=VISITOR)
    codes: list[Any] = []
    async with w.client() as c:
        for s in sids:
            for _ in range(9):  # 63 turns, each session under 10 a minute
                r = await c.post(f"/api/sessions/{s}/turns", json=turn_body())
                codes.append(r.json()["error"]["code"] if r.status_code == 429 else r.status_code)
    assert codes[:60] == [200] * 60 and codes[60:] == ["IP_HOURLY"] * 3
    r429 = ErrorEnvelope.model_validate((await _last_ip_refusal(w)).json())
    assert r429.error.details == {"limit": "60/hour", "scope": "ip"}


async def _last_ip_refusal(w: World) -> httpx.Response:
    """What: one more turn after the IP window is full. Use when: the IP_HOURLY test. Inputs: world.
    Output: the 429 response. Side effects: none (refused). Role: helper."""
    async with w.client() as c:
        return await c.post(f"/api/sessions/{SID}/turns", json=turn_body())


async def test_a_turn_refused_by_the_ip_row_does_not_consume_a_session_slot():
    redis = fresh_redis()
    lim = RateLimiter(redis, clock=Clock())
    for _ in range(60):
        await lim.hit(RULES["IP_HOURLY"], "9.9.9.9")
    rej = await lim.hit_all([(RULES["SESSION_TURNS"], "v:s"), (RULES["IP_HOURLY"], "9.9.9.9")])
    assert rej is not None and rej.rule.code == "IP_HOURLY"
    assert await redis.zcard("rl:SESSION_TURNS:v:s") == 0


# ---- session id key (I2) ----

async def test_case_and_hyphen_variants_of_a_session_id_share_one_window():
    redis = fresh_redis()
    w = world(10, redis)
    variants = [SID, SID.upper(), SID.replace("-", ""), "{" + SID + "}", "urn:uuid:" + SID]
    async with w.client() as c:
        for i in range(10):
            r = await c.post(f"/api/sessions/{variants[i % 5]}/turns", json=turn_body())
            assert r.status_code == 200, (variants[i % 5], r.text)
        eleventh = await c.post(f"/api/sessions/{SID.upper()}/turns", json=turn_body())
    assert eleventh.status_code == 429
    assert [k async for k in redis.scan_iter("rl:SESSION_TURNS:*")] == [f"rl:SESSION_TURNS:{VISITOR}:{SID}".encode()]


async def test_invalid_session_ids_are_404_before_any_counting():
    redis = fresh_redis()
    w = world(0, redis)
    async with w.client() as c:
        for bad in ("not-a-uuid", "123", "zzzzzzzz-zzzz-zzzz-zzzz-zzzzzzzzzzzz"):
            for path in ("turns", "voice/token", "transcript"):
                r = await c.post(f"/api/sessions/{bad}/{path}", json={})
                assert r.status_code == 404 and r.json()["error"]["code"] == "NOT_FOUND", (bad, path, r.text)
    assert [k async for k in redis.scan_iter("rl:*")] == []



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
