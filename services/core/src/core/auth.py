"""What: bearer-token authentication, the token to scope map, the per-route scope gate and the header dependency.
Use when: a core route is declared (scoped_route) or a handler needs the caller and request headers.
Inputs: Authorization and the doc 07 s1.2 headers of the request.
Output: CALLER_SCOPES, caller_scopes, RequestContext, scoped, scoped_route, get_ctx, ROUTE_SCOPES, new_uuid7.
Side effects: scoped_route records the route's scope in ROUTE_SCOPES (import time).
Role: doc 07 s1.2 to s1.4. Least privilege is by token: the scope table is in code, the test
tests/test_scope_matrix.py re-types it from the doc and fails on any difference. Order of checks on a
protected route: token (401 AUTH_INVALID), scope (403 SCOPE_DENIED), headers (X-Session-Id needs
session.act_as and must be a uuid, a {session_id} path value must be a uuid, X-Mode must be chat or voice; all 422), then the handler."""
from __future__ import annotations

import hmac
import os
import re
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from fastapi import APIRouter, Depends, FastAPI, Request

from core.errors import CoreError

CALLER_SCOPES: dict[str, frozenset[str]] = {
    "agent_tools": frozenset({"session.act_as", "identity.verify", "claims.read", "me.read", "billing.read",
                              "context.read", "actions.prepare", "actions.status", "prefs.write",
                              "tickets.create", "tickets.read_own"}),
    "agent_commit": frozenset({"session.act_as", "identity.step_up", "actions.status", "actions.commit",
                               "actions.submit", "actions.cancel"}),
    "ops": frozenset({"operator.login", "approvals.read", "approvals.decide", "tickets.read_all",
                      "tickets.resolve", "tickets.close"}),
}
# (METHOD, path template) -> required scope, filled by scoped_route.
ROUTE_SCOPES: dict[tuple[str, str], str] = {}
_DEMO_FAULTS = {"tool_error", "llm_429"}
_ID_OK = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")


def caller_scopes(name: str) -> frozenset[str]:
    """What: the scopes of a caller. Use when: tests or the gate. Inputs: caller name. Output: frozenset.
    Side effects: KeyError for an unknown caller. Role: read access to the token map."""
    return CALLER_SCOPES[name]


def new_uuid7() -> str:
    """What: mint a uuid7 string (time-ordered). Use when: a request carries no X-Trace-Id.
    Inputs: none. Output: uuid7 text. Side effects: reads wall time and os randomness.
    Role: doc 07 s1.2 (Python 3.12 has no uuid.uuid7)."""
    ms = time.time_ns() // 1_000_000
    rand = int.from_bytes(os.urandom(10), "big")
    value = (ms & ((1 << 48) - 1)) << 80
    value |= 0x7 << 76
    value |= ((rand >> 62) & 0xFFF) << 64
    value |= 0b10 << 62
    value |= rand & ((1 << 62) - 1)
    return str(uuid.UUID(int=value))


def parse_uuid(raw: str, loc: list[str]) -> str:
    """What: parse a uuid or fail with 422. Use when: a session id arrives in a header or the path.
    Inputs: raw text, the error location. Output: canonical lower-case uuid string.
    Side effects: raises CoreError VALIDATION_ERROR (the bad value is not echoed). Role: every session_id column
    is uuid, so a non-uuid must never reach SQL (it would be a 500) nor be dropped (it would look like no session)."""
    try:
        return str(uuid.UUID(raw))
    except ValueError:
        raise CoreError("VALIDATION_ERROR", details={"errors": [{"loc": loc, "type": "uuid_parsing",
                                                                 "msg": "must be a uuid"}]}) from None


def clean_id(value: str | None) -> str | None:
    """What: accept a correlation id only if it is short and made of safe characters.
    Use when: reading X-Request-Id and X-Trace-Id. Inputs: raw header. Output: the value or None.
    Side effects: none. Role: stops header text from forging log lines."""
    return value if value and _ID_OK.match(value) else None


@dataclass(frozen=True)
class RequestContext:
    """What: the authenticated caller and the parsed doc 07 s1.2 headers.
    Use when: a handler needs who is calling and for which session, turn and mode.
    Inputs: built by the scoped dependency. Output: frozen context. Side effects: none.
    Role: the only way handlers see headers; demo_fault is already gated by DEMO_FAULTS_ENABLED."""

    caller: str
    scopes: frozenset[str]
    request_id: str
    trace_id: str
    session_id: str | None
    mode: str | None
    turn_id: str | None
    idempotency_key: str | None
    demo_fault: str | None

    def require_session(self) -> str:
        """What: the session id, or a 422 when the request carried no X-Session-Id.
        Use when: a handler that acts for a session (identity, reads, actions). Inputs: none.
        Output: the canonical lower-case uuid string. Side effects: raises CoreError VALIDATION_ERROR if absent.
        Role: the dependency accepts a missing header (health-like and ticket routes may not need one); session
        handlers must not proceed without it (review PR19 I2)."""
        if self.session_id is None:
            raise CoreError("VALIDATION_ERROR", details={"errors": [{"loc": ["header", "x-session-id"],
                                                                     "type": "missing", "msg": "required"}]})
        return self.session_id


def _authenticate(request: Request) -> str:
    """What: map the bearer token to a caller name, comparing all three in constant time.
    Use when: starting every protected request. Inputs: request. Output: caller name.
    Side effects: raises CoreError AUTH_INVALID for a missing, malformed or unknown token.
    Role: doc 07 s1.4 (constant-time compare)."""
    header = request.headers.get("authorization", "")
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        raise CoreError("AUTH_INVALID")
    s = request.app.state.settings
    found: str | None = None
    for name, expected in (("agent_tools", s.token_agent), ("agent_commit", s.token_commit), ("ops", s.token_ops)):
        if hmac.compare_digest(token.strip().encode(), expected.encode()):
            found = name
    if found is None:
        raise CoreError("AUTH_INVALID")
    return found


def scoped(scope: str) -> Callable[[Request], RequestContext]:
    """What: build the dependency that authenticates, checks one scope and parses headers.
    Use when: declaring a route (via scoped_route). Inputs: the required scope string.
    Output: a FastAPI dependency returning RequestContext.
    Side effects: the dependency raises CoreError AUTH_INVALID, SCOPE_DENIED or VALIDATION_ERROR.
    Role: the 403 gate of CS-E-03.S13.T1."""

    def dependency(request: Request) -> RequestContext:
        """What: run the checks in the documented order. Use when: FastAPI resolves the route.
        Inputs: request. Output: RequestContext. Side effects: raises CoreError. Role: see scoped."""
        caller = _authenticate(request)
        scopes = CALLER_SCOPES[caller]
        if scope not in scopes:
            raise CoreError("SCOPE_DENIED", details={"required_scope": scope})
        h = request.headers
        raw_session = h.get("x-session-id")
        if raw_session is not None and "session.act_as" not in scopes:
            raise CoreError("SCOPE_DENIED", details={"required_scope": "session.act_as"})
        session_id = parse_uuid(raw_session, ["header", "x-session-id"]) if raw_session is not None else None
        path_session = request.path_params.get("session_id")
        if path_session is not None:
            parse_uuid(path_session, ["path", "session_id"])
        mode = h.get("x-mode")
        if mode is not None and mode not in ("chat", "voice"):
            raise CoreError("VALIDATION_ERROR", details={"errors": [{"loc": ["header", "x-mode"],
                                                                     "type": "enum", "msg": "must be chat or voice"}]})
        fault = h.get("x-demo-fault")
        demo_fault = fault if request.app.state.settings.demo_faults and fault in _DEMO_FAULTS else None
        ctx = RequestContext(
            caller=caller, scopes=scopes, request_id=request.state.request_id, trace_id=request.state.trace_id,
            session_id=session_id, mode=mode, turn_id=clean_id(h.get("x-turn-id")),
            idempotency_key=h.get("idempotency-key") or None, demo_fault=demo_fault)
        request.state.ctx = ctx
        return ctx

    dependency.scope = scope  # type: ignore[attr-defined]
    return dependency


def get_ctx(request: Request) -> RequestContext:
    """What: the RequestContext the route's scope dependency stored. Use when: a handler needs the caller.
    Inputs: request. Output: RequestContext. Side effects: AttributeError if the route is not scoped.
    Role: handler access to the authenticated context without a second dependency run."""
    return request.state.ctx


def scoped_route(router: FastAPI | APIRouter, method: str, path: str, scope: str, handler: Callable[..., Any],
                 **kwargs: Any) -> None:
    """What: register a route whose scope is recorded in ROUTE_SCOPES and enforced by `scoped`.
    Use when: adding any core route. Inputs: router, HTTP method, full path template, scope,
    handler (it receives `ctx: RequestContext = Depends(scoped(scope))` through the route), extra route kwargs.
    Output: none. Side effects: mutates router and ROUTE_SCOPES.
    Role: makes an unscoped route impossible to add by accident and lets tests enumerate every route."""
    ROUTE_SCOPES[(method.upper(), path)] = scope
    router.add_api_route(path, handler, methods=[method.upper()], dependencies=[Depends(scoped(scope))], **kwargs)
