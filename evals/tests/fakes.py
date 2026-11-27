"""What: a FastAPI test double of the agent's public API and an in-memory store, driven by scripted turns.
Use when: runner and CLI tests (the real agent turn route is built elsewhere). Inputs: scripted event lists per turn and core rows.
Output: an app (use it with starlette's TestClient, a sync httpx.Client), a FakeStore, request recordings.
Side effects: none (no sockets, no providers). Role: emits contract-valid SSE (every event is parsed by contracts.events)."""
from __future__ import annotations

import uuid
from typing import Any

from contracts.agent_api import TurnRequest, VoiceAskRequest
from contracts.events import format_sse, parse_event
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import ValidationError

from evals.model import Event, RunData
from evals.store import DataUnavailable

ACCESS_CODE = "open-sesame"


class Script:
    """What: one case's script: SSE events per turn and the core rows the stores will return. Use when: a test sets up a case.
    Inputs: turns (list of event lists), rows. Output: the record. Side effects: none. Role: test data."""

    def __init__(self, turns: list[list[Event]], pending: list[dict[str, Any]] | None = None, refunds: list[dict[str, Any]] | None = None,
                 tickets: list[dict[str, Any]] | None = None) -> None:
        """What: store the script. Use when: building one. Inputs: see class. Output: none. Side effects: none. Role: init."""
        self.turns = turns
        self.pending = pending or []
        self.refunds = refunds or []
        self.tickets = tickets or []


class FakeWorld:
    """What: shared state of the double and its store. Use when: a test wires the app and the store together.
    Inputs: none. Output: the state. Side effects: none. Role: the fake agent plus fake core in one object."""

    def __init__(self) -> None:
        """What: empty world. Use when: a test starts. Inputs: none. Output: none. Side effects: none. Role: init."""
        self.next: Script | None = None
        self.scripts: dict[str, Script] = {}
        self.cursor: dict[str, int] = {}
        self.requests: list[dict[str, Any]] = []
        self.sessions_created = 0
        self.store_down = False
        self.fail_turn_with: int | None = None


def build_app(world: FakeWorld) -> FastAPI:
    """What: the agent double. Use when: tests need /api/access, /api/sessions, /turns and /voice/ask. Inputs: world.
    Output: FastAPI app. Side effects: records requests in world. Role: 07 s2.2 shapes only; no rules."""
    app = FastAPI()

    def denied() -> JSONResponse:
        return JSONResponse({"code": "ACCESS_DENIED"}, status_code=401)

    @app.post("/api/access")
    async def access(req: Request) -> Response:
        body = await req.json()
        if body.get("code") != ACCESS_CODE:
            return denied()
        r = Response(status_code=204)
        r.set_cookie("qs_access", "signed-cookie")
        return r

    @app.post("/api/sessions")
    async def sessions(req: Request) -> Response:
        if req.cookies.get("qs_access") != "signed-cookie":
            return denied()
        if world.next is None:
            return JSONResponse({"code": "UNAVAILABLE"}, status_code=503)
        sid = str(uuid.uuid4())
        world.scripts[sid] = world.next
        world.next = None
        world.cursor[sid] = 0
        world.sessions_created += 1
        return JSONResponse({"session_id": sid, "created_at": "2026-11-27T13:00:00Z"}, status_code=201)

    async def stream(sid: str, req: Request, kind: str, body: dict[str, Any]) -> Response:
        if req.cookies.get("qs_access") != "signed-cookie":
            return denied()
        script = world.scripts.get(sid)
        if script is None:
            return JSONResponse({"code": "NOT_FOUND"}, status_code=404)
        world.requests.append({"path": kind, "body": body, "fault": req.headers.get("x-demo-fault"), "sid": sid})
        i = world.cursor[sid]
        world.cursor[sid] += 1
        if world.fail_turn_with == i:
            return JSONResponse({"code": "INTERNAL"}, status_code=500)
        if i >= len(script.turns):
            return JSONResponse({"code": "NO_SCRIPT"}, status_code=500)
        events = [{**e, "session_id": sid} for e in script.turns[i]]
        chunks = [format_sse(parse_event(e)) for e in events]
        return StreamingResponse(iter(chunks), media_type="text/event-stream")

    @app.post("/api/sessions/{sid}/turns")
    async def turns(sid: str, req: Request) -> Response:
        body = await req.json()
        try:
            TurnRequest.model_validate(body)
        except ValidationError as e:
            return JSONResponse({"code": "VALIDATION_ERROR", "detail": str(e)[:200]}, status_code=422)
        return await stream(sid, req, "turns", body)

    @app.post("/api/sessions/{sid}/voice/ask")
    async def voice(sid: str, req: Request) -> Response:
        body = await req.json()
        try:
            VoiceAskRequest.model_validate(body)
        except ValidationError as e:
            return JSONResponse({"code": "VALIDATION_ERROR", "detail": str(e)[:200]}, status_code=422)
        return await stream(sid, req, "voice", body)

    return app


class FakeStore:
    """What: an in-memory Store reading the world's scripts. Use when: runner tests. Inputs: world. Output: RunData.
    Side effects: none. Role: stands in for the Postgres reads; store_down makes every read raise DataUnavailable."""

    def __init__(self, world: FakeWorld) -> None:
        """What: bind to a world. Use when: a test wires it. Inputs: world. Output: none. Side effects: none. Role: init."""
        self.world = world

    def collect(self, session_id: str, expected_events: dict[str, int]) -> RunData:
        """What: rows and stored (non-token) events of a session. Use when: the runner grades. Inputs: session id, expected
        stored-event counts per turn id. Output: RunData. Side effects: none. Role: Store interface."""
        if self.world.store_down:
            raise DataUnavailable("fake store is down")
        s = self.world.scripts[session_id]
        events = [{**e, "session_id": session_id} for t in s.turns[: self.world.cursor.get(session_id, 0)] for e in t if e["type"] != "token"]
        return RunData(pending=s.pending, refunds=s.refunds, tickets=s.tickets, events=events)

    def register_voice_session(self, session_id: str) -> str:
        """What: a voice session id the double accepts. Use when: a voice turn is about to be sent. Inputs: session id.
        Output: id. Side effects: none. Role: Store interface (the real one inserts agent.voice_sessions)."""
        return "vs_fake_1"
