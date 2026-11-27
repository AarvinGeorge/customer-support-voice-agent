"""What: the shared HTTP call for the agent's internal clients: doc 07 s1.2 headers, the s1.3 error envelope
mapped to ToolCallError, transport failures mapped to timeout or unavailable.
Use when: CoreAgentClient and KnowledgeClient make a request inside a turn.
Inputs: an httpx.AsyncClient, base URL, bearer token, the turn's TurnControl, method, path, body or params.
Output: ToolCallError, call_json, call_json_within.
Side effects: one HTTP request per call.
Role: one place that knows headers and error classes. Holds no token itself: the caller passes the one it owns.
"""
from __future__ import annotations

import asyncio
import uuid
from typing import Any, Literal

import httpx

from agent.turn_control import TurnControl

ErrorKind = Literal["timeout", "unavailable", "client_error"]


class ToolCallError(Exception):
    """What: a failed internal call, classified for ToolGuard's retry rules (doc 07 s5.1, s8.1).
    Use when: raised by clients and tools; ToolGuard catches it.
    Inputs: kind (timeout and unavailable retry; client_error never does), HTTP status, error code, customer-safe
    message, details, changed (core's flag), fault_injected.
    Output: exception. Side effects: none. Role: error taxonomy of a tool call."""

    def __init__(self, kind: ErrorKind, *, status: int | None = None, code: str | None = None,
                 message: str = "", details: dict[str, Any] | None = None, changed: bool | None = None,
                 fault_injected: bool = False) -> None:
        """What: store the fields. Use when: raising. Inputs: see class. Output: none. Side effects: none.
        Role: constructor."""
        super().__init__(f"{kind}:{status}:{code}")
        self.kind, self.status, self.code, self.message = kind, status, code, message
        self.details, self.changed, self.fault_injected = details or {}, changed, fault_injected

    @property
    def retryable(self) -> bool:
        """What: true for a timeout or an unavailable service. Use when: ToolGuard's retry decision.
        Inputs: none. Output: bool. Side effects: none. Role: "a timeout or core_unavailable retries; 4xx never"."""
        return self.kind != "client_error"


def base_headers(token: str, control: TurnControl) -> dict[str, str]:
    """What: Authorization, X-Request-Id (fresh uuid4), X-Trace-Id (the turn id), X-Session-Id, X-Mode.
    Use when: every agent call to core or knowledge. Inputs: token, control. Output: header dict.
    Side effects: none. Role: doc 07 s1.2 rows sent on every call."""
    return {"Authorization": f"Bearer {token}", "X-Request-Id": str(uuid.uuid4()), "X-Trace-Id": control.turn_id,
            "X-Session-Id": control.session_id, "X-Mode": control.mode}


def _error_from(resp: httpx.Response) -> ToolCallError:
    """What: map a non-2xx response (doc 07 s1.3 envelope) to a ToolCallError.
    Use when: call_json. Inputs: response. Output: error (5xx or retryable: unavailable; other 4xx: client_error).
    Side effects: none. Role: error mapping (an unparseable body keeps the status and an empty code)."""
    try:
        err = (resp.json() or {}).get("error") or {}
    except ValueError:
        err = {}
    retryable = bool(err.get("retryable")) or resp.status_code >= 500
    return ToolCallError("unavailable" if retryable else "client_error", status=resp.status_code,
                         code=err.get("code"), message=str(err.get("message") or ""), details=err.get("details"),
                         changed=err.get("changed"), fault_injected=bool(err.get("fault_injected")))


async def call_json(http: httpx.AsyncClient, method: str, url: str, headers: dict[str, str], *,
                    params: dict[str, Any] | None = None, body: Any = None) -> Any:
    """What: one request; the JSON body on 2xx, otherwise ToolCallError.
    Use when: client methods. Inputs: client, method, absolute URL, headers, query params, JSON body.
    Output: decoded JSON (a non-JSON 2xx raises unavailable). Side effects: the request. Role: transport and error mapping (no retry here: the
    one retry layer for tools is ToolGuard)."""
    try:
        resp = await http.request(method, url, headers=headers, params=params, json=body)
    except httpx.TimeoutException as exc:
        raise ToolCallError("timeout", message="The service did not answer in time.") from exc
    except httpx.TransportError as exc:
        raise ToolCallError("unavailable", message="The service could not be reached.") from exc
    if resp.status_code >= 300:
        raise _error_from(resp)
    try:
        return resp.json()
    except ValueError as exc:  # a 2xx that is not JSON: the service is not answering as contracted
        raise ToolCallError("unavailable", status=resp.status_code,
                            message="The service returned an unexpected answer.") from exc


async def call_json_within(timeout_s: float, http: httpx.AsyncClient, method: str, url: str, headers: dict[str, str],
                           *, params: dict[str, Any] | None = None, body: Any = None) -> Any:
    """What: call_json bounded by a clamped timeout; the timeout becomes ToolCallError("timeout").
    Use when: ingress code calls core itself (memory refresh, verify, commit, status, cancel) and owns the clamp.
    Inputs: timeout seconds, then as call_json. Output: decoded JSON. Side effects: the request.
    Role: doc 07 s8.2 "ingress for core calls it makes itself"."""
    try:
        return await asyncio.wait_for(call_json(http, method, url, headers, params=params, body=body), timeout_s)
    except (TimeoutError, asyncio.TimeoutError) as exc:
        raise ToolCallError("timeout", message="The service did not answer in time.") from exc
