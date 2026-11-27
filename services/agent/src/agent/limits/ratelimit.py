"""What: Redis sliding-window rate limits for the keyed rows of doc 07 s9, the 429 envelope, client IP resolution,
and the FastAPI dependencies that apply them.
Use when: a route adds `dependencies=[Depends(limit_turn)]` (or limit_session_create, limit_session_post,
limit_voice_token, limit_access); create_app calls install_limits once for the 429 handler.
Inputs: app.state.redis (async Redis client shared by both replicas), app.state.settings (forwarded_allow_ips),
the request (path param sid, request.state.visitor_id from the access gate, socket peer, X-Forwarded-For).
Output: RULES, RateLimiter, client_ip, RateLimited, the dependencies, install_limits.
Side effects: sorted-set writes on Redis under keys `rl:<CODE>:<subject>` (expire with the window); warning logs when
failing open.
Role: ingress step 2 (doc 07 s2.4). Fails OPEN when Redis is down or missing (logged; no in-memory fallback, R-11);
the turn lock and voice call dedupe are different code and fail closed.
Not built (doc 09 s1.2): the per-replica admission semaphore (8 concurrent, 503 BUSY) and the operator login route
(its rule OPERATOR_ATTEMPTS is defined here so the row is not lost).
Counting note: the dependency counts every request that reaches the route, replays and the capped 41st turn
included; doc 07 s2.4 would exempt those (they are decided inside Ingress). See the S2 report, open concerns.
"""
from __future__ import annotations

import ipaddress
import logging
import math
import time
from dataclasses import dataclass
from functools import lru_cache
from typing import Any, Callable
from uuid import uuid4

from contracts import limits as L
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

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


def _rule(code: str, limit: int, window_s: int, scope: str, label: str, what: str) -> Rule:
    """What: build a Rule with the standard message. Use when: filling RULES. Inputs: row fields, what is limited.
    Output: Rule. Side effects: none. Role: helper."""
    return Rule(code, limit, window_s, scope, label, f"You are {what} too quickly. Please wait a moment and try again.")


RULES: dict[str, Rule] = {r.code: r for r in (
    _rule("SESSION_TURNS", L.SESSION_TURNS_PER_MINUTE, 60, "session", "10/minute", "sending messages"),
    _rule("IP_HOURLY", L.IP_TURNS_PER_HOUR, 3600, "ip", "60/hour", "sending messages"),
    _rule("IP_SESSIONS", L.IP_SESSIONS_PER_HOUR, 3600, "ip", "20/hour", "starting conversations"),
    _rule("SESSION_POSTS", L.SESSION_POSTS_PER_HOUR, 3600, "session", "120/hour", "sending updates"),
    _rule("VOICE_SESSIONS", L.IP_VOICE_TOKENS_PER_HOUR, 3600, "ip", "3/hour", "starting voice calls"),
    _rule("ACCESS_ATTEMPTS", L.IP_ACCESS_ATTEMPTS_PER_HOUR, 3600, "ip", "10/hour", "trying access codes"),
    _rule("OPERATOR_ATTEMPTS", 5, 900, "ip", "5/15 minutes", "trying operator sign-ins"),  # route designed only
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

    async def hit(self, rule: Rule, subject: str) -> Rejection | None:
        """What: record one hit and say whether the window is now over its limit.
        Use when: ingress step 2. Inputs: rule, subject (session or IP key). Output: None or Rejection with
        retry_after_s = ceil(oldest hit + window - now), minimum 1. Side effects: Redis writes (above); a refused
        hit is removed again so a blocked client does not extend its own window. Any Redis error is logged and
        the hit is allowed (fail open). Role: the limiter core."""
        key = f"rl:{rule.code}:{subject}"
        now = self._clock()
        member = f"{now:.6f}:{uuid4().hex}"
        try:
            async with self._redis.pipeline(transaction=True) as pipe:
                pipe.zremrangebyscore(key, 0, now - rule.window_s)
                pipe.zadd(key, {member: now})
                pipe.zcard(key)
                pipe.zrange(key, 0, 0, withscores=True)
                pipe.expire(key, rule.window_s + 1)
                _, _, count, oldest, _ = await pipe.execute()
            if count <= rule.limit:
                return None
            await self._redis.zrem(key, member)
            reset = float(oldest[0][1]) + rule.window_s
            return Rejection(rule, max(1, math.ceil(reset - now)))
        except Exception as exc:  # noqa: BLE001 - any Redis or network fault must not block the request
            log.warning("rate limit %s failing open: redis error %s", rule.code, type(exc).__name__)
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


def client_ip(request: Request) -> str:
    """What: the client address used as the IP key.
    Use when: an IP-keyed row is checked. Inputs: request (socket peer, X-Forwarded-For, settings).
    Output: the address, or "unknown" without a peer. Side effects: none.
    Role: doc 07 s9 and D-047. X-Forwarded-For is read only when the immediate peer is inside
    FORWARDED_ALLOW_IPS (the edge subnet, where Caddy runs); then the entries are walked right to left past trusted
    hops and the first untrusted valid address wins, so a value the client put on the left is never used. From any
    other peer the header is ignored and the socket peer is the key."""
    peer = request.client.host if request.client else None
    if not peer:
        return "unknown"
    nets = _trusted(getattr(request.app.state.settings, "forwarded_allow_ips", "") or "")
    xff = request.headers.get("x-forwarded-for")
    if not xff or not _in(nets, peer):
        return peer
    for raw in reversed([p.strip() for p in xff.split(",")]):
        try:
            ipaddress.ip_address(raw)
        except ValueError:
            return peer  # a malformed hop: do not guess
        if nets is None or not _in(nets, raw):  # "*" trusts every peer: the rightmost entry is the client
            return raw
    return peer


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


def _session_subject(request: Request) -> str:
    """What: the key for a session-scoped row. Use when: session rows. Inputs: request (path sid, visitor id).
    Output: "<visitor_id>:<sid>". Side effects: none. Role: the contract key is session_id; the visitor id is
    added so a caller who names someone else's session id burns only their own window (ownership is checked later
    in the handler, 404)."""
    return f"{getattr(request.state, 'visitor_id', '-')}:{request.path_params.get('sid', '-')}"


async def limit_turn(request: Request) -> None:
    """What: SESSION_TURNS (10/minute per session) then IP_HOURLY (60/hour per IP).
    Use when: POST /turns and POST /voice/ask. Inputs: request. Output: none; raises RateLimited.
    Side effects: Redis writes. Role: doc 07 s9 rows 1 and 2."""
    await enforce(request, RULES["SESSION_TURNS"], _session_subject(request))
    await enforce(request, RULES["IP_HOURLY"], client_ip(request))


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
