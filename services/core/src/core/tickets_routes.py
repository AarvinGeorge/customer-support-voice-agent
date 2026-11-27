"""What: the HTTP handler for POST /v1/tickets.
Use when: the app is assembled (routes.register_routes plugs it in for the route name tickets_create).
Inputs: the request (scope `tickets.create`, X-Session-Id, Idempotency-Key, TicketCreate body).
Output: tickets_create, the 200 body `{ticket, merged, created}`.
Side effects: one database transaction that holds the idempotency record, the ticket writes and the audit row;
a refusal (REQUESTER_UNKNOWN, TICKET_LIMIT_REACHED) gets a ticket_create_refused audit row in its own transaction.
Role: doc 07 s3.4. Business refusals (the 429 limit) are CoreErrors in the envelope; nothing is stored for them."""
from __future__ import annotations

from typing import Any

from contracts.core_api import TicketCreate, TicketCreateResponse
from contracts.errors import CORE_ERROR_CODES
from fastapi import Request
from fastapi.responses import JSONResponse

from core.audit import write_refusal
from core.auth import get_ctx
from core.db import Database
from core.errors import CoreError
from core.idempotency import run_idempotent
from core.tickets import create_ticket

ENDPOINT = "POST /v1/tickets"
MAX_KEY_LENGTH = 200


def tickets_create(request: Request, body: TicketCreate) -> JSONResponse:
    """What: create or merge a ticket for the caller's session, once per Idempotency-Key.
    Use when: the model tool create_ticket or agent code escalation calls core. Inputs: request, validated body.
    Output: 200 JSON `{ticket, merged, created}`; a replay returns the stored response.
    Side effects: one transaction (lock, writes, idempotency row). Raises CoreError VALIDATION_ERROR (no session or key,
    or key reused for another body), REQUESTER_UNKNOWN (403, D-070), TICKET_LIMIT_REACHED (429), TOOL_ERROR (no
    database); each refusal after validation is audited.
    Role: the route; the rules live in core.tickets."""
    ctx = get_ctx(request)
    session_id = ctx.require_session()
    key = ctx.idempotency_key
    if key is None or len(key) > MAX_KEY_LENGTH:
        raise CoreError("VALIDATION_ERROR", details={"errors": [{"loc": ["header", "idempotency-key"],
                                                                 "type": "missing" if key is None else "too_long",
                                                                 "msg": "required, at most 200 characters"}]})
    db: Database | None = request.app.state.db
    if db is None:
        raise CoreError("TOOL_ERROR", retry_after_s=5, klass="db_unavailable")
    policy, clock = request.app.state.policy, request.app.state.clock
    try:
        return _create(db, policy, clock, ctx, session_id, key, body)
    except CoreError as exc:  # REQUESTER_UNKNOWN, TICKET_LIMIT_REACHED: the transaction rolled back, audit it apart
        write_refusal(db, event="ticket_create_refused", code=exc.code, status=CORE_ERROR_CODES[exc.code][0],
                      session_id=session_id, clock=clock, policy_version=policy.policy_version, trace_id=ctx.trace_id,
                      mode=ctx.mode, idempotency_key=key)
        raise


def _create(db: Database, policy: Any, clock: Any, ctx: Any, session_id: str, key: str,
            body: TicketCreate) -> JSONResponse:
    """What: the ticket create transaction (idempotent effect). Use when: tickets_create. Inputs: database, policy,
    clock, request context, session, key, body. Output: 200 JSON. Side effects: one transaction.
    Raises CoreError from create_ticket. Role: helper, so the route can audit a refusal after the rollback."""
    with db.tx() as conn:
        def effect() -> tuple[int, dict]:
            """What: create or merge, then shape the response. Use when: run_idempotent calls it once. Inputs: none.
            Output: (200, body dict). Side effects: the ticket writes. Role: the effect stored with the key."""
            out = create_ticket(conn, policy=policy, clock=clock, session_id=session_id, body=body, actor="agent",
                                trace_id=ctx.trace_id, mode=ctx.mode, idempotency_key=key)
            return 200, TicketCreateResponse(ticket=out.ref, merged=out.merged,
                                             created=out.created).model_dump(mode="json")

        result = run_idempotent(conn, caller=ctx.caller, key=key, endpoint=ENDPOINT,
                                request={"session_id": session_id, "body": body.model_dump(mode="json")}, fn=effect)
    return JSONResponse(result.body, status_code=result.status_code)
