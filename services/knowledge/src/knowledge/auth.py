"""What: bearer-token authentication and per-route scopes for knowledge (doc 07 s1.4).
Use when: a route declares `Depends(require_scope("search"))`.
Inputs: Authorization header; env KNOWLEDGE_TOKEN_AGENT. Output: the caller name, or an UNAUTHORIZED envelope.
Side effects: none. Role: knowledge holds one token (caller `agent`, scopes search, chunks.read, corpus.read);
compared in constant time. A caller lacking the scope gets the same 401 so a probe learns nothing."""
from __future__ import annotations

import hmac
import os

from fastapi import Request

from knowledge.errors import ApiError

# caller name -> (env var holding its token, scopes), doc 07 s1.4
CALLERS: dict[str, tuple[str, frozenset[str]]] = {
    "agent": ("KNOWLEDGE_TOKEN_AGENT", frozenset({"search", "chunks.read", "corpus.read"})),
}


def check_configured() -> None:
    """What: fail startup when a caller token is unset or empty. Use when: app lifespan start.
    Inputs: env. Output: none, or RuntimeError naming the variable. Side effects: none.
    Role: an unset token must never mean an open service."""
    for env, _ in CALLERS.values():
        if not os.environ.get(env):
            raise RuntimeError(f"{env} is not set")


def authenticate(header: str | None) -> tuple[str, frozenset[str]] | None:
    """What: map an Authorization header to (caller, scopes). Use when: any protected request.
    Inputs: header value or None. Output: tuple or None when absent, malformed or unknown.
    Side effects: none. Role: constant-time compare against every configured token (no early exit)."""
    if not header or not header.startswith("Bearer "):
        return None
    presented = header[len("Bearer "):].strip().encode()
    found: tuple[str, frozenset[str]] | None = None
    for name, (env, scopes) in CALLERS.items():
        expected = os.environ.get(env, "").encode()
        if expected and hmac.compare_digest(presented, expected):
            found = (name, scopes)
    return found


def require_scope(scope: str):
    """What: build a FastAPI dependency that demands a scope. Use when: declaring a protected route.
    Inputs: scope name. Output: dependency returning the caller name. Side effects: sets request.state.caller.
    Role: 401 UNAUTHORIZED envelope on a bad token or a missing scope."""

    def dependency(request: Request) -> str:
        """What: authenticate this request. Use when: FastAPI resolves it. Inputs: request.
        Output: caller name. Side effects: sets request.state.caller. Role: the check itself."""
        who = authenticate(request.headers.get("Authorization"))
        if who is None or scope not in who[1]:
            raise ApiError(401, "UNAUTHORIZED", "access_denied", "Missing or invalid credentials.",
                           headers={"WWW-Authenticate": "Bearer"})
        request.state.caller = who[0]
        return who[0]

    return dependency
