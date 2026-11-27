"""What: the visitor access gate: signed qs_access cookie, constant-time code check, Origin check,
the deny-by-default `/api/*` gate and the session ownership helper (doc 07 s2.1).
Use when: create_app installs the gate; the access route signs the cookie; session routes call owned_session.
Inputs: SESSION_SIGNING_KEY, DEMO_ACCESS_CODE and SITE_ORIGIN (via Settings); request headers and cookies.
Output: sign_cookie, verify_cookie, code_matches, origin_ok, install_access_gate, owned_session, NotOwned.
Side effects: the gate sets request.state.visitor_id on gated requests; nothing else is stored here.
Role: every `/api/*` route except access, healthz and readyz needs a valid cookie by default, so a route
added later is protected without remembering to ask.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from typing import Any, Awaitable, Callable, Protocol

from fastapi import FastAPI, Request

from agent.ingress import SessionRow
from agent.web import error_response

COOKIE_NAME = "qs_access"
COOKIE_TTL_S = 24 * 3600
OPEN_PATHS = frozenset({"/api/access", "/api/healthz", "/api/readyz"})
STATE_CHANGING = frozenset({"POST", "PUT", "PATCH", "DELETE"})


def _b64(raw: bytes) -> str:
    """What: base64url without padding. Use when: building the cookie. Inputs: bytes. Output: str.
    Side effects: none. Role: encoder."""
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _unb64(text: str) -> bytes:
    """What: decode base64url without padding. Use when: reading the cookie. Inputs: str.
    Output: bytes (binascii.Error or ValueError on bad input). Side effects: none. Role: decoder."""
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _mac(key: str, body: str) -> str:
    """What: HMAC-SHA256 of the cookie body. Use when: signing and verifying. Inputs: key, body.
    Output: base64url tag. Side effects: none. Role: signature."""
    return _b64(hmac.new(key.encode(), body.encode("ascii"), hashlib.sha256).digest())


def sign_cookie(key: str, now: float | None = None, visitor_id: str | None = None) -> str:
    """What: build the qs_access value `<payload>.<hmac>` with a fresh random 128-bit visitor id.
    Use when: a good access code was entered. Inputs: signing key, optional clock and visitor id (tests).
    Output: cookie string. Side effects: reads the random source. Role: doc 07 s2.1 cookie.
    Raises ValueError when the key is empty (fail loudly, never sign with nothing)."""
    if not key:
        raise ValueError("SESSION_SIGNING_KEY is not set")
    iat = int(now if now is not None else time.time())
    payload = {"visitor_id": visitor_id or _b64(secrets.token_bytes(16)), "iat": iat, "exp": iat + COOKIE_TTL_S}
    body = _b64(json.dumps(payload, separators=(",", ":")).encode())
    return f"{body}.{_mac(key, body)}"


def verify_cookie(key: str, token: str | None, now: float | None = None) -> dict[str, Any] | None:
    """What: check signature (constant time), shape and expiry of a qs_access value.
    Use when: the gate reads the cookie. Inputs: key, cookie string, optional clock.
    Output: the payload dict, or None for anything wrong (never raises). Side effects: none.
    Role: authentication of a visitor; an empty key verifies nothing."""
    if not key or not token or token.count(".") != 1:
        return None
    body, tag = token.split(".")
    try:
        if not hmac.compare_digest(_mac(key, body).encode(), tag.encode("ascii")):
            return None
        payload = json.loads(_unb64(body))
    except (ValueError, UnicodeEncodeError):
        return None
    if not isinstance(payload, dict) or not isinstance(payload.get("visitor_id"), str):
        return None
    exp = payload.get("exp")
    if not isinstance(exp, int) or not isinstance(payload.get("iat"), int):
        return None
    if (now if now is not None else time.time()) >= exp:
        return None
    return payload


def code_matches(given: str, expected: str) -> bool:
    """What: constant-time equality of the access code. Use when: POST /api/access.
    Inputs: the visitor's code, the configured code. Output: bool; false when either is empty.
    Side effects: none. Role: doc 07 s2.1 gate check."""
    if not given or not expected:
        return False
    return hmac.compare_digest(given.encode(), expected.encode())


def origin_ok(origin: str | None, site_origin: str) -> bool:
    """What: the Origin rule: an absent header passes, a present one must equal SITE_ORIGIN.
    Use when: a state-changing request arrives. Inputs: header value, configured origin.
    Output: bool. Side effects: none. Role: doc 07 s2.1 CSRF defence beside SameSite=Lax."""
    return origin is None or origin.rstrip("/") == site_origin.rstrip("/")


def cookie_secure(site_origin: str) -> bool:
    """What: whether the cookie gets the Secure flag. Use when: setting qs_access.
    Inputs: SITE_ORIGIN. Output: True for an https origin. Side effects: none.
    Role: keeps http://localhost working in browsers that refuse Secure cookies there; the VM is https."""
    return site_origin.lower().startswith("https://")


def install_access_gate(app: FastAPI) -> None:
    """What: add the middleware that enforces the Origin rule and the cookie on every `/api/*` route.
    Use when: once in create_app, BEFORE install_http (so request and trace ids are already set).
    Inputs: app (reads app.state.settings). Output: none. Side effects: mutates app; sets
    request.state.visitor_id. Role: deny by default; only OPEN_PATHS skip the cookie."""

    @app.middleware("http")
    async def gate(request: Request, call_next: Callable[[Request], Awaitable[Any]]) -> Any:
        """What: reject a foreign Origin (403 ORIGIN_MISMATCH) then a missing or bad cookie (401 ACCESS_DENIED).
        Use when: every request. Inputs: request. Output: response. Side effects: sets visitor_id.
        Role: the gate; non-`/api` paths are not ours and pass through."""
        cfg = request.app.state.settings
        path = request.url.path
        if not path.startswith("/api/"):
            return await call_next(request)
        if request.method in STATE_CHANGING and not origin_ok(request.headers.get("origin"), cfg.site_origin):
            return error_response(request, 403, "access_denied", "ORIGIN_MISMATCH", "Request origin not allowed.")
        if path in OPEN_PATHS:
            return await call_next(request)
        payload = verify_cookie(cfg.session_signing_key, request.cookies.get(COOKIE_NAME))
        if payload is None:
            return error_response(request, 401, "access_denied", "ACCESS_DENIED", "Access code required.")
        request.state.visitor_id = payload["visitor_id"]
        return await call_next(request)


class NotOwned(Exception):
    """What: the session does not exist or belongs to another visitor. Use when: owned_session fails.
    Inputs: none. Output: exception. Side effects: none. Role: one signal for both cases (no existence hint)."""


class SessionLookup(Protocol):
    """What: the one store method the ownership check needs. Use when: typing owned_session.
    Inputs: session id. Output: SessionRow or None. Side effects: none. Role: structural type."""

    async def get_session(self, session_id: str) -> SessionRow | None: ...


async def owned_session(store: SessionLookup, session_id: str, visitor_id: str) -> SessionRow:
    """What: return the session only if it belongs to this visitor.
    Use when: any route that names a session, turn or trace (doc 07 s2.1 Ownership).
    Inputs: store, session id from the path, visitor id from the cookie.
    Output: SessionRow. Side effects: one read. Role: ownership helper; raises NotOwned, which routes
    turn into 404 NOT_FOUND for unknown and foreign sessions alike."""
    row = await store.get_session(session_id)
    if row is None or not hmac.compare_digest(row.visitor_id.encode(), visitor_id.encode()):
        raise NotOwned(session_id)
    return row
