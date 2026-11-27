"""What: POST /api/sessions/{sid}/turns (doc 07 s2.2, s2.3 TurnRequest): the chat turn route.
Use when: create_app calls install_turns once.
Inputs: app.state.ingress (the shared Ingress from agent.composition; None: 503 BUSY), app.state.session_store.
Output: install_turns.
Side effects: a full turn through Ingress (rows, lock, agent loop) behind a text/event-stream response.
Role: thin adapter. The access gate already checked the cookie and Origin (doc 07 s2.4 step 1); this route checks
ownership before reading the body (404 before 422), validates the body (step 2) and hands the rest to Ingress.
"""
from __future__ import annotations

import json
from typing import Any

from fastapi import FastAPI, Request

from agent.access import owned_session
from agent.ingress import Ingress, IngressError, IngressInput, input_kind_of, parse_turn_request, to_response
from agent.limits.ratelimit import canonical_sid, turn_counter
from agent.web import error_response


def request_demo_fault(request: Request) -> str | None:
    """What: HOOK for the X-Demo-Fault header (doc 07 s10). Returns None until CS-E-07.S1.T2 (PR #44) merges; then it
    returns demo_fault_from_header(request.headers.get("x-demo-fault"), enabled=settings.demo_faults_enabled) and the
    route passes it as IngressInput(demo_fault=...).
    Use when: post_turn. Inputs: request. Output: the gated fault or None. Side effects: none. Role: seam."""
    return None


def install_turns(app: FastAPI) -> None:
    """What: register the turn route. Use when: once in create_app. Inputs: app. Output: none.
    Side effects: mutates app. Role: route wiring."""

    @app.post("/api/sessions/{sid}/turns")
    async def post_turn(sid: str, request: Request) -> Any:
        """What: run one chat turn and stream its events.
        Use when: the console sends text or a card press. Inputs: session id, TurnRequest JSON.
        Output: text/event-stream (doc 07 s6), or an envelope: 404, 409, 422, 503. Side effects: see module.
        Role: doc 07 s2.2 row POST /turns."""
        sid = canonical_sid(sid)  # one spelling per session (rate-limit key, lock, thread); invalid is 404
        await owned_session(request.app.state.session_store, sid, request.state.visitor_id)
        ingress: Ingress | None = getattr(request.app.state, "ingress", None)
        if ingress is None:
            return error_response(request, 503, "admission_rejected", "BUSY",
                                  "We're having a brief problem. Please try again in a moment.",
                                  retryable=True, retry_after_s=3)
        try:
            raw = json.loads(await request.body())
        except ValueError:
            raw = None
        req = parse_turn_request(raw)
        if isinstance(req, IngressError):
            return to_response(request, req)
        _ = request_demo_fault(request)  # wired into IngressInput.demo_fault with PR #44
        result = await ingress.handle(IngressInput(
            session_id=sid, visitor_id=request.state.visitor_id, client_turn_id=str(req.client_turn_id),
            mode="chat", input_kind=input_kind_of(req), text=req.text, body=req,
            count_turn=turn_counter(request, sid)))
        return to_response(request, result)
