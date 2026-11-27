"""What: Redis sliding-window rate limits for the keyed rows of doc 07 s9, the 429 envelope, client IP resolution,
and the FastAPI dependencies that apply them.
Use when: a route adds `dependencies=[Depends(limit_session_create)]` (or limit_session_post, limit_voice_token,
limit_access), or passes turn_counter to Ingress for turns; create_app calls install_limits once for the 429 handler.
Inputs: app.state.redis (async Redis client shared by both replicas), app.state.settings (forwarded_allow_ips),
the request (path param sid, request.state.visitor_id from the access gate, socket peer, X-Forwarded-For).
Output: RULES, RateLimiter, client_ip, RateLimited, the dependencies, install_limits.
Side effects: sorted-set writes on Redis under keys `rl:<CODE>:<subject>` (expire with the window); warning logs when
failing open.
Role: ingress step 2 (doc 07 s2.4). Fails OPEN when Redis is down or missing (logged; no in-memory fallback, R-11);
the turn lock and voice call dedupe are different code and fail closed.
Not built (doc 09 s1.2): the per-replica admission semaphore (8 concurrent, 503 BUSY) and the operator login route
(its rule OPERATOR_ATTEMPTS is defined here so the row is not lost).
Counting (doc 07 s2.4): SESSION_TURNS and IP_HOURLY are recorded by Ingress at step 9, through the `count_turn`
callback from turn_counter, so replays, the capped turn, and requests refused earlier (401, 404, 422, 409) do not
count. The other rows are route dependencies and count each request that reaches the route.
"""
from __future__ import annotations

import ipaddress
import logging
import math
import time
import uuid
from dataclasses import dataclass
from functools import lru_cache
from typing import Any, Awaitable, Callable
from contracts import limits as L
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from agent.access import NotOwned
from agent.ingress import IngressError
from agent.web import error_response

log = logging.getLogger("agent.limits")


@dataclass(frozen=True)
class Rule:
    """What: one row of the doc 07 s9 table.
    Use when: RULES holds them; RateLimiter.hit takes one. Inputs: code (the 429 error code), limit, window in
    seconds, scope ("session" or "ip"), label shown in details.limit, customer-safe message.
    Output: value. Side effects: none. Role: data."""

    code: str
    limit: int
    window_s: int
    scope: str
    label: str
    message: str


def _rule(code: str, limit: int, window_s: int, scope: str, what: str) -> Rule:
    """What: build a Rule with the standard message and a label derived from the numbers (10/minute, 5/15 minutes).
    Use when: filling RULES. Inputs: row fields, what is limited. Output: Rule. Side effects: none. Role: helper."""
    unit = {60: "minute", 3600: "hour"}.get(window_s, f"{window_s // 60} minutes")
    return Rule(code, limit, window_s, scope, f"{limit}/{unit}",
                f"You are {what} too quickly. Please wait a moment and try again.")


RULES: dict[str, Rule] = {r.code: r for r in (
    _rule("SESSION_TURNS", L.SESSION_TURNS_PER_MINUTE, 60, "session", "sending messages"),
    _rule("IP_HOURLY", L.IP_TURNS_PER_HOUR, 3600, "ip", "sending messages"),
    _rule("IP_SESSIONS", L.IP_SESSIONS_PER_HOUR, 3600, "ip", "starting conversations"),
    _rule("SESSION_POSTS", L.SESSION_POSTS_PER_HOUR, 3600, "session", "sending updates"),
    _rule("VOICE_SESSIONS", L.IP_VOICE_TOKENS_PER_HOUR, 3600, "ip", "starting voice calls"),
    _rule("ACCESS_ATTEMPTS", L.IP_ACCESS_ATTEMPTS_PER_HOUR, 3600, "ip", "trying access codes"),
    _rule("OPERATOR_ATTEMPTS", 5, 900, "ip", "trying operator sign-ins"),  # route designed only
)}


@dataclass(frozen=True)
class Rejection:
    """What: a refused hit. Use when: RateLimiter.hit returns it. Inputs: the rule, retry_after_s (>= 1).
    Output: value. Side effects: none. Role: data."""

    rule: Rule
    retry_after_s: int


class RateLimited(Exception):
    """What: raised by a dependency when a window is full. Use when: enforce rejects.
    Inputs: Rejection. Output: exception. Side effects: none. Role: carries the rejection to the 429 handler."""

    def __init__(self, rejection: Rejection) -> None:
        """What: keep the rejection. Use when: raising. Inputs: rejection. Output: none. Side effects: none.
        Role: constructor."""
        super().__init__(rejection.rule.code)
        self.rejection = rejection


class RateLimiter:
    """What: sliding-window counters on Redis sorted sets (one member per hit, score = time).
    Use when: a dependency needs to count a request. Inputs: an async Redis client, optional clock (tests).
    Output: hit(rule, subject) returns None (allowed) or a Rejection.
    Side effects: ZADD, ZREMRANGEBYSCORE, ZCARD, ZRANGE, EXPIRE in one transaction, ZREM of a refused hit.
    Role: doc 07 s9 counters. Same moving-window rule as the `limits` library named in the contract, written on
    redis.asyncio so tests run on fakeredis (the library needs a URI)."""

    def __init__(self, redis: Any, clock: Callable[[], float] = time.time) -> None:
        """What: store the client and clock. Use when: building the limiter. Inputs: redis, clock. Output: none.
        Side effects: none. Role: constructor."""
        self._redis = redis
        self._clock = clock

    async def _hit(self, rule: Rule, subject: str) -> tuple[Rejection | None, str | None]:
        """What: record one hit; say whether the window is now over its limit.
        Use when: hit and hit_all. Inputs: rule, subject. Output: (Rejection or None, key and member of the
        recorded hit as "key|member", or None when nothing was recorded). Side effects: Redis writes; a refused
        hit is removed again. Any Redis error is logged and the hit is allowed (fail open). Role: limiter core."""
        key = f"rl:{rule.code}:{subject}"
        now = self._clock()
        member = f"{now:.6f}:{uuid.uuid4().hex}"
        try:
            async with self._redis.pipeline(transaction=True) as pipe:
                pipe.zremrangebyscore(key, 0, now - rule.window_s)
                pipe.zadd(key, {member: now})
                pipe.zcard(key)
                pipe.zrange(key, 0, 0, withscores=True)
                pipe.expire(key, rule.window_s + 1)
                _, _, count, oldest, _ = await pipe.execute()
            if count <= rule.limit:
                return None, f"{key}|{member}"
            await self._redis.zrem(key, member)
            reset = float(oldest[0][1]) + rule.window_s
            return Rejection(rule, max(1, math.ceil(reset - now))), None
        except Exception as exc:  # noqa: BLE001 - any Redis or network fault must not block the request
            log.warning("rate limit %s failing open: redis error %s", rule.code, type(exc).__name__)
            return None, None

    async def hit(self, rule: Rule, subject: str) -> Rejection | None:
        """What: record one hit and say whether it was refused.
        Use when: a single row applies. Inputs: rule, subject. Output: None (allowed) or Rejection with
        retry_after_s = ceil(oldest hit + window - now), minimum 1. Side effects: see _hit. Role: limiter API."""
        return (await self._hit(rule, subject))[0]

    async def hit_all(self, checks: list[tuple[Rule, str]]) -> Rejection | None:
        """What: record hits for several rows; if one refuses, take back the hits already recorded for the others.
        Use when: a turn starts (SESSION_TURNS then IP_HOURLY). Inputs: (rule, subject) pairs in order.
        Output: None or the first Rejection. Side effects: Redis writes; ZREM of the earlier hits on a refusal
        (best effort, logged). Role: a request refused by one row does not use up a slot in another."""
        taken: list[str] = []
        for rule, subject in checks:
            rejection, token = await self._hit(rule, subject)
            if rejection is not None:
                for t in taken:
                    key, member = t.split("|", 1)
                    try:
                        await self._redis.zrem(key, member)
                    except Exception as exc:  # noqa: BLE001
                        log.warning("rate limit refund failed: redis error %s", type(exc).__name__)
                return rejection
            if token:
                taken.append(token)
        return None


@lru_cache(maxsize=16)
def _trusted(spec: str) -> tuple[Any, ...] | None:
    """What: parse FORWARDED_ALLOW_IPS. Use when: client_ip. Inputs: comma list of IPs and CIDRs, or "*".
    Output: tuple of networks, or None for "*" (trust every peer). Side effects: none (cached).
    Role: helper; entries that do not parse are skipped."""
    nets: list[Any] = []
    for part in (p.strip() for p in spec.split(",")):
        if part == "*":
            return None
        try:
            nets.append(ipaddress.ip_network(part, strict=False))
        except ValueError:
            continue
    return tuple(nets)


def _in(nets: tuple[Any, ...] | None, ip: str) -> bool:
    """What: is the address inside the trusted networks. Use when: client_ip. Inputs: networks (None = all), ip.
    Output: bool (False for an unparseable ip). Side effects: none. Role: helper."""
    if nets is None:
        return True
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return any(addr in n for n in nets)


def _canon(ip: str) -> str:
    """What: canonical text of an address. Use when: building a key. Inputs: address text.
    Output: the canonical form, or the input when it does not parse. Side effects: none. Role: helper
    (2001:DB8::1 and 2001:db8:0:0:0:0:0:1 share one window)."""
    try:
        return str(ipaddress.ip_address(ip))
    except ValueError:
        return ip


def client_ip(request: Request) -> str:
    """What: the client address used as the IP key.
    Use when: an IP-keyed row is checked. Inputs: request (socket peer, X-Forwarded-For, settings).
    Output: the canonical address, or "unknown" without a peer. Side effects: none.
    Role: doc 07 s9 and D-047. X-Forwarded-For (all header lines, as one list) is read only when the immediate peer
    is inside FORWARDED_ALLOW_IPS (the edge subnet, where Caddy runs); then the entries are walked right to left
    past trusted hops and the first untrusted valid address wins, so a value the client put on the left is never
    used. From any other peer the header is ignored and the socket peer is the key."""
    peer = request.client.host if request.client else None
    if not peer:
        return "unknown"
    nets = _trusted(getattr(request.app.state.settings, "forwarded_allow_ips", "") or "")
    xff = ",".join(request.headers.getlist("x-forwarded-for"))
    if not xff or not _in(nets, peer):
        return _canon(peer)
    for raw in reversed([p.strip() for p in xff.split(",")]):
        try:
            ipaddress.ip_address(raw)
        except ValueError:
            return _canon(peer)  # a malformed hop: do not guess
        if nets is None or not _in(nets, raw):  # "*" trusts every peer: the rightmost entry is the client
            return _canon(raw)
    return _canon(peer)


async def enforce(request: Request, rule: Rule, subject: str) -> None:
    """What: count one hit for a rule and raise if the window is full.
    Use when: a dependency applies a row. Inputs: request, rule, subject. Output: none; raises RateLimited.
    Side effects: Redis writes; a warning when app.state.redis is missing (fail open). Role: glue."""
    redis = getattr(request.app.state, "redis", None)
    if redis is None:
        log.warning("rate limit %s failing open: no redis client", rule.code)
        return
    rejection = await RateLimiter(redis).hit(rule, subject)
    if rejection is not None:
        raise RateLimited(rejection)


def canonical_sid(sid: str) -> str:
    """What: the one spelling of a session id: lowercase hyphenated UUID.
    Use when: a route names a session (rate-limit key, ownership, lock, thread). Inputs: path text.
    Output: canonical string. Side effects: none. Role: Postgres accepts case, brace and hyphen variants of a uuid,
    so without this one session would get many windows. Raises NotOwned (404 NOT_FOUND, no hint) when it does not parse."""
    try:
        return str(uuid.UUID(sid))
    except ValueError as exc:
        raise NotOwned(sid) from exc


def _session_subject(request: Request) -> str:
    """What: the key for a session-scoped row. Use when: session rows. Inputs: request (path sid, visitor id).
    Output: "<visitor_id>:<canonical sid>". Side effects: none (raises NotOwned for an invalid id, before any count).
    Role: the contract key is session_id; the visitor id is added so a caller who names someone else's session id
    burns only their own window (ownership is checked later in the handler, 404)."""
    return f"{getattr(request.state, 'visitor_id', '-')}:{canonical_sid(request.path_params.get('sid', ''))}"


def turn_counter(request: Request, sid: str) -> Callable[[], Awaitable[IngressError | None]]:
    """What: the callback Ingress calls when a turn starts (step 9): record SESSION_TURNS then IP_HOURLY.
    Use when: building IngressInput for /turns and /voice/ask. Inputs: request, canonical session id.
    Output: async callable returning None (go on) or an IngressError 429 (the route answers it).
    Side effects: Redis writes when called; none otherwise. Role: doc 07 s2.4 Counting; a refusal by one row
    takes back the other's hit."""
    async def count() -> IngressError | None:
        """What: count this turn. Use when: Ingress step 9. Inputs: none. Output: None or IngressError 429.
        Side effects: Redis writes. Role: closure over the request."""
        redis = getattr(request.app.state, "redis", None)
        if redis is None:
            log.warning("rate limit SESSION_TURNS failing open: no redis client")
            return None
        subject = f"{getattr(request.state, 'visitor_id', '-')}:{sid}"
        rej = await RateLimiter(redis).hit_all([(RULES["SESSION_TURNS"], subject),
                                                (RULES["IP_HOURLY"], client_ip(request))])
        if rej is None:
            return None
        return IngressError(429, "rate_limited", rej.rule.code, rej.rule.message, retryable=True,
                            retry_after_s=rej.retry_after_s,
                            details={"limit": rej.rule.label, "scope": rej.rule.scope})

    return count


async def limit_session_create(request: Request) -> None:
    """What: IP_SESSIONS (20/hour per IP). Use when: POST /api/sessions. Inputs: request.
    Output: none; raises RateLimited. Side effects: Redis writes. Role: doc 07 s9 row 3."""
    await enforce(request, RULES["IP_SESSIONS"], client_ip(request))


async def limit_session_post(request: Request) -> None:
    """What: SESSION_POSTS (120/hour per session). Use when: POST /transcript (and POST /voice/usage when built).
    Inputs: request. Output: none; raises RateLimited. Side effects: Redis writes. Role: doc 07 s9 row 4."""
    await enforce(request, RULES["SESSION_POSTS"], _session_subject(request))


async def limit_voice_token(request: Request) -> None:
    """What: VOICE_SESSIONS (3/hour per IP). Use when: POST /voice/token. Inputs: request.
    Output: none; raises RateLimited. Side effects: Redis writes. Role: doc 07 s9 row 5."""
    canonical_sid(request.path_params.get("sid", ""))  # invalid id: 404 before any count
    await enforce(request, RULES["VOICE_SESSIONS"], client_ip(request))


async def limit_access(request: Request) -> None:
    """What: ACCESS_ATTEMPTS (10/hour per IP), counting wrong and right codes alike. Use when: POST /api/access.
    Inputs: request. Output: none; raises RateLimited. Side effects: Redis writes. Role: doc 07 s9 row 6."""
    await enforce(request, RULES["ACCESS_ATTEMPTS"], client_ip(request))


def install_limits(app: FastAPI) -> None:
    """What: register the handler that turns RateLimited into the 429 envelope.
    Use when: once in create_app. Inputs: app. Output: none. Side effects: mutates app.
    Role: doc 07 s9 429 body: class rate_limited, retry_after_s, Retry-After header, details {limit, scope}."""

    @app.exception_handler(RateLimited)
    async def rate_limited(request: Request, exc: RateLimited) -> JSONResponse:
        """What: 429 envelope. Use when: a dependency raised RateLimited. Inputs: request, exc.
        Output: response with Retry-After. Side effects: none. Role: s9 reject shape."""
        rule = exc.rejection.rule
        return error_response(request, 429, "rate_limited", rule.code, rule.message, retryable=True, changed=False,
                              retry_after_s=exc.rejection.retry_after_s,
                              details={"limit": rule.label, "scope": rule.scope})
