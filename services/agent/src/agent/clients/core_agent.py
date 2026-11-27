"""What: the core client for tool wrappers, holding ONLY the agent token (CORE_TOKEN_AGENT, caller agent_tools).
Use when: read tools, prepare_refund and create_ticket call core (doc 07 s3.2 to s3.4); ingress memory refresh
and card verification (s3.1, scopes context.read and identity.verify on the same token).
Inputs: base URL, the agent token, an httpx.AsyncClient, whether demo faults are enabled.
Output: CoreAgentClient with get_context, verify, get_profile, list_charges, list_tickets, prepare_refund,
create_ticket.
Side effects: HTTP requests to core.
Role: least privilege per module (doc 07 s1.4): this client cannot commit, submit, cancel or step up because its
token lacks those scopes; the commit client lives in agent/structured/confirm.py and the CI import guard keeps it out
of every tool module. X-Demo-Fault: tool_error is forwarded on every core call of a faulted request only when
DEMO_FAULTS_ENABLED is true (doc 07 s10).
"""
from __future__ import annotations

from typing import Any

import httpx

from agent.clients.http import base_headers, call_json, call_json_within
from agent.turn_control import TurnControl


class CoreAgentClient:
    """What: core calls on the agent token. Use when: tool wrappers. Inputs: see module.
    Output: JSON bodies. Side effects: HTTP. Role: the tools' only route to core."""

    def __init__(self, base_url: str, token: str, *, http: httpx.AsyncClient, demo_faults_enabled: bool = False) -> None:
        """What: keep the settings. Use when: app start or tests. Inputs: base URL, token, client, fault flag.
        Output: none. Side effects: none. Role: constructor."""
        self._base, self._token, self._http = base_url.rstrip("/"), token, http
        self._faults = demo_faults_enabled

    def _headers(self, control: TurnControl, *, turn: bool = False, idempotency_key: str | None = None) -> dict[str, str]:
        """What: s1.2 headers for core plus X-Turn-Id, Idempotency-Key and the forwarded fault when they apply.
        Use when: every call. Inputs: control, whether X-Turn-Id is sent, key. Output: headers.
        Side effects: none. Role: header policy."""
        h = base_headers(self._token, control)
        if turn:
            h["X-Turn-Id"] = control.turn_id
        if idempotency_key:
            h["Idempotency-Key"] = idempotency_key
        if self._faults and control.demo_fault == "tool_error":
            h["X-Demo-Fault"] = "tool_error"
        return h

    async def get_context(self, control: TurnControl, timeout_s: float) -> Any:
        """What: GET /v1/me/context (scope context.read). Use when: ingress step 11 memory refresh and right after a
        successful verify. Inputs: control, clamped timeout. Output: MeContext JSON. Side effects: HTTP.
        Role: s3.1 (the retry and the clamp are the caller's, agent.memory)."""
        return await call_json_within(timeout_s, self._http, "GET", f"{self._base}/v1/me/context",
                                      self._headers(control))

    async def verify(self, control: TurnControl, account_id: str, pin: str, timeout_s: float) -> Any:
        """What: POST /v1/identity/verify (scope identity.verify on the agent token; no tool calls it).
        Use when: the masked verification card body at ingress step 13. Inputs: control, account id, PIN,
        clamped timeout. Output: Claims JSON. Side effects: HTTP; core counts a failure. Role: s3.1, s6.3 step 4
        (0 retries: a retry could double-count a failure)."""
        return await call_json_within(timeout_s, self._http, "POST", f"{self._base}/v1/identity/verify",
                                      self._headers(control), body={"account_id": account_id, "pin": pin})

    async def get_profile(self, control: TurnControl) -> Any:
        """What: GET /v1/me/profile. Use when: get_account_summary. Inputs: control. Output: Profile JSON.
        Side effects: HTTP. Role: s3.2."""
        return await call_json(self._http, "GET", f"{self._base}/v1/me/profile", self._headers(control))

    async def list_charges(self, control: TurnControl, params: dict[str, Any]) -> Any:
        """What: GET /v1/me/charges with ending, on, since_days, limit. Use when: list_charges.
        Inputs: control, query params (None values dropped). Output: {charges, ambiguous}. Side effects: HTTP.
        Role: s3.2."""
        q = {k: v for k, v in params.items() if v is not None}
        return await call_json(self._http, "GET", f"{self._base}/v1/me/charges", self._headers(control), params=q)

    async def list_tickets(self, control: TurnControl, params: dict[str, Any]) -> Any:
        """What: GET /v1/me/tickets with status and ending. Use when: list_tickets. Inputs: control, params.
        Output: {tickets}. Side effects: HTTP. Role: s3.2."""
        q = {k: v for k, v in params.items() if v is not None}
        return await call_json(self._http, "GET", f"{self._base}/v1/me/tickets", self._headers(control), params=q)

    async def prepare_refund(self, control: TurnControl, body: dict[str, Any], idempotency_key: str) -> Any:
        """What: POST /v1/actions/refund/prepare with X-Turn-Id and the prepare key. Use when: prepare_refund.
        Inputs: control, RefundPrepareRequest JSON, key "{session_id}:{tool_call_id}". Output: PrepareResult JSON.
        Side effects: HTTP (core inserts a pending row; nothing is changed). Role: s3.3."""
        return await call_json(self._http, "POST", f"{self._base}/v1/actions/refund/prepare",
                               self._headers(control, turn=True, idempotency_key=idempotency_key), body=body)

    async def create_ticket(self, control: TurnControl, body: dict[str, Any], idempotency_key: str) -> Any:
        """What: POST /v1/tickets with the ticket key. Use when: create_ticket (origin model).
        Inputs: control, TicketCreate JSON, key. Output: {ticket, merged, created}. Side effects: HTTP (a ticket).
        Role: s3.4."""
        return await call_json(self._http, "POST", f"{self._base}/v1/tickets",
                               self._headers(control, idempotency_key=idempotency_key), body=body)
