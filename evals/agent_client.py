"""What: an HTTP client for the agent's public API that behaves like the browser: POST /api/access for the cookie, POST
/api/sessions, then POST /api/sessions/{sid}/turns (or /voice/ask) read as an SSE stream (doc 07 s2.2, s6.1).
Use when: the runner sends a turn. Inputs: an httpx.Client (real, or starlette's TestClient for a double).
Output: TurnResult per turn. Side effects: HTTP calls; the cookie jar holds qs_access. Role: the only place that speaks HTTP to the agent.
Every received event is validated against contracts.events, so a malformed stream is an error and never a silent pass."""
from __future__ import annotations

from typing import Any

import httpx
from contracts.events import parse_event
from pydantic import ValidationError

from .model import TurnResult
from .sse import SseError, parse_sse


class AgentError(Exception):
    """What: raised for a failed access or session call. Use when: setup fails. Inputs: message. Output: exception.
    Side effects: none. Role: setup failures grade the case error."""


class AgentClient:
    """What: browser-like client for the agent API. Use when: the runner needs a session and turns. Inputs: httpx client.
    Output: session ids and TurnResults. Side effects: HTTP. Role: wire layer."""

    def __init__(self, http: httpx.Client) -> None:
        """What: wrap an httpx client. Use when: the CLI or a test builds it. Inputs: client (base_url set). Output: none.
        Side effects: none. Role: init."""
        self.http = http

    def access(self, code: str) -> None:
        """What: POST /api/access to get the qs_access cookie. Use when: once per run. Inputs: the access code.
        Output: none; raises AgentError unless 204. Side effects: sets the cookie. Role: the visitor gate (07 s2.1)."""
        r = self.http.post("/api/access", json={"code": code})
        if r.status_code != 204:
            raise AgentError(f"POST /api/access returned HTTP {r.status_code}")

    def create_session(self) -> str:
        """What: POST /api/sessions. Use when: each case starts. Inputs: none. Output: session id; raises AgentError unless 201.
        Side effects: creates a session in the agent. Role: one session per case."""
        r = self.http.post("/api/sessions", json={})
        if r.status_code != 201:
            raise AgentError(f"POST /api/sessions returned HTTP {r.status_code}")
        return str(r.json()["session_id"])

    def turn(self, index: int, kind: str, sid: str, body: dict[str, Any], sent: dict[str, Any], fault: str | None = None,
             voice: bool = False) -> TurnResult:
        """What: send one turn and read its SSE stream. Use when: the runner executes a case turn. Inputs: turn index, case kind,
        session id, JSON body, what to remember about the input, optional X-Demo-Fault, voice flag. Output: TurnResult (error set
        on any HTTP, stream or contract problem). Side effects: one HTTP request. Role: the turn wire call."""
        path = f"/api/sessions/{sid}/" + ("voice/ask" if voice else "turns")
        headers = {"X-Demo-Fault": fault} if fault else {}
        res = TurnResult(index=index, kind=kind, input=sent, status_code=0)
        try:
            with self.http.stream("POST", path, json=body, headers=headers) as r:
                res.status_code = r.status_code
                if r.status_code != 200:
                    r.read()
                    res.error = f"HTTP {r.status_code}: {r.text[:200]}"
                    return res
                for ev in parse_sse(r.iter_lines()):
                    parse_event(ev)
                    res.events.append(ev)
        except SseError as e:
            res.error = f"malformed stream: {e}"
        except ValidationError as e:
            res.error = f"contract violation in an SSE event: {e.errors()[0]['loc']} {e.errors()[0]['msg']}"
        except httpx.HTTPError as e:
            res.error = f"transport error: {type(e).__name__}"
        if res.error is None and res.completed is None:
            res.error = "stream ended without turn.completed"
        if res.events:
            res.turn_id = res.events[0].get("turn_id")
        return res
