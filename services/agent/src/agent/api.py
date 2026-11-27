"""What: the visitor-facing routes: POST /api/access, GET /api/me, GET /api/demo/personas, POST /api/sessions
(doc 07 s2.2). The cookie gate itself lives in agent.access.
Use when: create_app calls install_api once.
Inputs: Settings (code, signing key, origin, personas path), app.state.session_store.
Output: routes with the contract bodies (Me, Persona list, SessionCreated).
Side effects: access sets the qs_access cookie; sessions inserts a row.
Role: the web app's landing page calls exactly these (apps/web/src/lib/api/access.ts).
Rate limits (ACCESS_ATTEMPTS, IP_SESSIONS) are route dependencies from agent.limits.ratelimit (CS-E-07.S2.T1).
"""
from __future__ import annotations

import hashlib
from typing import Any

from contracts.agent_api import AccessRequest, Me, SessionCreated
from contracts.base import StrictModel
from fastapi import Depends, FastAPI, Request, Response
from fastapi.responses import JSONResponse

from agent import access
from agent.limits.ratelimit import limit_access, limit_session_create
from agent.personas import load_personas
from agent.web import error_response


class EmptyBody(StrictModel):
    """What: the `{}` body of POST /api/sessions. Use when: validating it. Inputs: none.
    Output: model that rejects any field. Side effects: none. Role: doc 07 s2.2 request shape."""


def _ip_hash(request: Request, key: str) -> str | None:
    """What: salted hash of the client IP (uvicorn already resolved X-Forwarded-For from the edge).
    Use when: creating a session. Inputs: request, signing key as salt. Output: 16 hex chars or None.
    Side effects: none. Role: abuse correlation without storing the address."""
    host = request.client.host if request.client else None
    return hashlib.sha256(f"{key}:{host}".encode()).hexdigest()[:16] if host else None


def install_api(app: FastAPI) -> None:
    """What: register the four visitor routes. Use when: once in create_app.
    Inputs: app. Output: none. Side effects: mutates app. Role: route wiring."""

    @app.post("/api/access", status_code=204, dependencies=[Depends(limit_access)])
    async def post_access(body: AccessRequest, request: Request) -> Response:
        """What: trade the demo code for a signed qs_access cookie.
        Use when: the visitor presses Continue on the gate. Inputs: AccessRequest.
        Output: 204 with Set-Cookie, or 401 ACCESS_DENIED. Side effects: none stored. Role: doc 07 s2.1."""
        cfg = request.app.state.settings
        if not access.code_matches(body.code, cfg.demo_access_code):
            return error_response(request, 401, "access_denied", "ACCESS_DENIED", "That code did not work.")
        resp = Response(status_code=204)
        resp.set_cookie(access.COOKIE_NAME, access.sign_cookie(cfg.session_signing_key),
                        max_age=access.COOKIE_TTL_S, path="/", httponly=True, samesite="lax",
                        secure=access.cookie_secure(cfg.site_origin))
        return resp

    @app.get("/api/me")
    async def get_me(request: Request) -> Any:
        """What: who the cookie says the caller is. Use when: the web app boots.
        Inputs: request. Output: Me. Side effects: none. Role: doc 07 s2.2 (operator is designed, not built)."""
        me = Me(visitor=True, operator=False, demo_faults_enabled=request.app.state.settings.demo_faults_enabled)
        return JSONResponse(me.model_dump(mode="json"))

    @app.get("/api/demo/personas")
    async def get_personas(request: Request) -> Any:
        """What: the demo personas with their public PINs. Use when: the landing page loads.
        Inputs: request. Output: list of Persona (empty while the seeds file is missing).
        Side effects: reads the personas file. Role: doc 07 s2.2."""
        people = load_personas(request.app.state.settings.personas_path)
        return JSONResponse([p.model_dump(mode="json") for p in people])

    @app.post("/api/sessions", status_code=201, dependencies=[Depends(limit_session_create)])
    async def post_sessions(_: EmptyBody, request: Request) -> Any:
        """What: start a conversation bound to the caller's visitor id. Use when: Start on a persona row.
        Inputs: `{}`. Output: 201 SessionCreated. Side effects: INSERT into agent.sessions. Role: doc 07 s2.2."""
        cfg = request.app.state.settings
        sid, created = await request.app.state.session_store.create_session(
            request.state.visitor_id, _ip_hash(request, cfg.session_signing_key))
        out = SessionCreated(session_id=sid, created_at=created.isoformat())
        return JSONResponse(out.model_dump(mode="json"), status_code=201)
