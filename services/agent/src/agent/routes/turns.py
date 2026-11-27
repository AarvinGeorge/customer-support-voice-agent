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
from agent.middleware.deadline_fault import demo_fault_from_header
from agent.limits.ratelimit import canonical_sid, turn_counter
from agent.web import error_response


def request_demo_fault(request: Request) -> str | None:
    """What: the request's X-Demo-Fault after the DEMO_FAULTS_ENABLED gate (doc 07 s10).
    Use when: post_turn and voice/ask, once per request, before IngressInput is built.
    Inputs: request (header, app settings). Output: "llm_429", "tool_error" or None.
    Side effects: demo_fault_from_header logs an ignored header. Role: the one call site of the gate for routes."""
    return demo_fault_from_header(request.headers.get("x-demo-fault"),
                                  enabled=request.app.state.settings.demo_faults_enabled)


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
        result = await ingress.handle(IngressInput(
            session_id=sid, visitor_id=request.state.visitor_id, client_turn_id=str(req.client_turn_id),
            mode="chat", input_kind=input_kind_of(req), text=req.text, body=req,
            demo_fault=request_demo_fault(request),
            count_turn=turn_counter(request, sid)))
        return to_response(request, result)
