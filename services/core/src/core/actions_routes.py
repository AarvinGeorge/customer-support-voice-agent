"""What: the HTTP handlers for refund prepare, grace extension prepare (disabled), pending status, commit, submit and
cancel.
Use when: the app is assembled (routes.register_routes plugs them in by route name).
Inputs: the request (scopes actions.prepare, actions.status, actions.commit, actions.submit, actions.cancel;
X-Session-Id, X-Turn-Id, Idempotency-Key), the path pending id, the prepare body.
Output: refund_prepare, grace_prepare, pending_status_route, pending_commit, pending_submit, pending_cancel.
Side effects: one database transaction per request (the engine's writes, the idempotency row for prepare).
Role: doc 07 s3.3. Only CORE_TOKEN_COMMIT holds actions.commit, actions.submit and actions.cancel, so the scope gate
(core.auth) is what keeps every tool wrapper away from them. Errors that must keep a state change (expired,
rules_changed) are returned by the engine and raised here after the transaction has committed."""
from __future__ import annotations

import logging
import re
from collections.abc import Callable
from typing import Any

from contracts.core_api import RefundPrepareRequest
from fastapi import Request
from fastapi.responses import JSONResponse

from contracts.errors import CORE_ERROR_CODES

from core.actions import cancel_pending, commit_pending, pending_status, prepare_refund, submit_pending
from core.audit import write_refusal
from core.auth import RequestContext, get_ctx
from core.db import Database
from core.errors import CoreError
from core.identity import require_claims
from core.idempotency import run_idempotent

log = logging.getLogger("core.actions")
PREPARE_ENDPOINT = "POST /v1/actions/refund/prepare"
MAX_KEY_LENGTH = 200
_PENDING_ID = re.compile(r"^PA-[0-9A-F]{8}$")


def _db(request: Request) -> Database:
    """What: the database handle or a 503. Use when: a handler starts. Inputs: request. Output: Database.
    Side effects: raises CoreError TOOL_ERROR when none is configured. Role: no 500s without a database."""
    db: Database | None = request.app.state.db
    if db is None:
        raise CoreError("TOOL_ERROR", retry_after_s=5, klass="db_unavailable")
    return db


def _missing(header: str, kind: str = "missing") -> CoreError:
    """What: a 422 for a missing or bad header. Use when: X-Turn-Id or Idempotency-Key is absent.
    Inputs: header name, error type. Output: CoreError (not raised). Side effects: none. Role: one shape."""
    return CoreError("VALIDATION_ERROR", details={"errors": [{"loc": ["header", header], "type": kind,
                                                             "msg": "required"}]})


def _turn(ctx: RequestContext) -> str:
    """What: the X-Turn-Id or a 422. Use when: prepare, commit, submit. Inputs: context. Output: turn id.
    Side effects: raises CoreError VALIDATION_ERROR. Role: doc 07 s1.2 (core refuses a commit in the prepare turn,
    so it needs both turn ids)."""
    if ctx.turn_id is None:
        raise _missing("x-turn-id")
    return ctx.turn_id


def _pending_id(raw: str) -> str:
    """What: accept only well-formed pending ids. Use when: a path carries one. Inputs: raw path value.
    Output: id. Side effects: raises CoreError NOT_FOUND otherwise. Role: an id that cannot exist is not found."""
    if not _PENDING_ID.match(raw):
        raise CoreError("NOT_FOUND", "That request does not exist.")
    return raw


def _audit_refusal(request: Request, ctx: RequestContext, session_id: str, event: str, pending_id: str | None,
                   exc: CoreError) -> None:
    """What: the refusal audit row for an action route (core.audit.write_refusal, action type refund).
    Use when: a prepare, commit, submit or cancel raised a CoreError. Inputs: request, context, session, event name,
    pending id or None, the error. Output: none. Side effects: one INSERT in its own transaction (never raises).
    Role: adapter from the request to the shared writer."""
    write_refusal(_db(request), event=event, code=exc.code, status=CORE_ERROR_CODES[exc.code][0],
                  session_id=session_id, clock=request.app.state.clock,
                  policy_version=request.app.state.policy.policy_version, trace_id=ctx.trace_id, mode=ctx.mode,
                  action_type="refund", pending_action_id=pending_id, idempotency_key=ctx.idempotency_key)


def refund_prepare(request: Request, body: RefundPrepareRequest) -> JSONResponse:
    """What: prepare a refund once per Idempotency-Key. Use when: the prepare_refund tool calls core.
    Inputs: request (X-Session-Id, X-Turn-Id, Idempotency-Key), validated body. Output: 200 PrepareResult; a retry with
    the same key returns the stored result. Side effects: one transaction. Raises CoreError VALIDATION_ERROR (missing
    session, turn or key, key reused), SESSION_NOT_VERIFIED, NOT_FOUND, AMBIGUOUS_REFERENCE, ACTION_IN_PROGRESS,
    TOOL_ERROR. Role: doc 07 s3.3 prepare row."""
    ctx = get_ctx(request)
    session_id = ctx.require_session()
    turn_id = _turn(ctx)
    key = ctx.idempotency_key
    if key is None or len(key) > MAX_KEY_LENGTH:
        raise _missing("idempotency-key", "missing" if key is None else "too_long")
    policy, clock = request.app.state.policy, request.app.state.clock
    try:
        with _db(request).tx() as conn:
            claims = require_claims(conn, session_id, clock.now(), policy)

            def effect() -> tuple[int, dict[str, Any]]:
                """What: run the prepare engine. Use when: run_idempotent calls it once. Inputs: none.
                Output: (200, PrepareResult dict). Side effects: the engine's writes. Role: the stored effect."""
                out = prepare_refund(conn, policy=policy, clock=clock, claims=claims, session_id=session_id,
                                     turn_id=turn_id, body=body, trace_id=ctx.trace_id, mode=ctx.mode,
                                     idempotency_key=key)
                return 200, out.model_dump(mode="json")

            result = run_idempotent(conn, caller=ctx.caller, key=key, endpoint=PREPARE_ENDPOINT,
                                    request={"session_id": session_id, "body": body.model_dump(mode="json")}, fn=effect)
    except CoreError as exc:
        _audit_refusal(request, ctx, session_id, "action_prepare_refused", None, exc)
        raise
    return JSONResponse(result.body, status_code=result.status_code)


def grace_prepare(request: Request) -> JSONResponse:
    """What: grace extension prepare, which is not available in this build. Use when: the agent calls it.
    Inputs: request. Output: never returns. Side effects: raises CoreError ACTION_DISABLED (404).
    Role: doc 07 s3.3 grace row; A4 is switched off in policy.yaml (enabled: false) and its engine is designed only,
    so the answer is 404 whatever the flag says."""
    raise CoreError("ACTION_DISABLED", "Payment grace extensions are not available here.")


def _run(request: Request, pending_id: str, fn: Callable[..., Any], *, verb: str, needs_turn: bool) -> JSONResponse:
    """What: run one commit, submit or cancel engine call in a transaction; raise a returned CoreError after commit.
    Use when: the three confirm routes. Inputs: request, raw pending id, engine function, verb (commit, submit, cancel;
    names the refusal audit event), whether X-Turn-Id is needed. Output: 200 JSON of the engine result.
    Side effects: one transaction; a raised CoreError rolls it back and gets a refusal audit row
    (action_<verb>_refused). Role: the persist-then-fail pattern."""
    ctx = get_ctx(request)
    session_id = ctx.require_session()
    pid = _pending_id(pending_id)
    extra = {"turn_id": _turn(ctx)} if needs_turn else {}
    policy, clock = request.app.state.policy, request.app.state.clock
    try:
        with _db(request).tx() as conn:
            out = fn(conn, policy=policy, clock=clock, pending_id=pid, session_id=session_id, trace_id=ctx.trace_id,
                     mode=ctx.mode, **extra)
    except CoreError as exc:
        _audit_refusal(request, ctx, session_id, f"action_{verb}_refused", pid, exc)
        raise
    if isinstance(out, CoreError):
        raise out
    return JSONResponse(out.model_dump(mode="json"))


def pending_commit(request: Request, pending_id: str) -> JSONResponse:
    """What: commit a prepared action. Use when: the confirm module sends a strict yes or Confirm press.
    Inputs: request (X-Session-Id, X-Turn-Id), pending id. Output: 200 CommitResult. Side effects: the refund.
    Raises CoreError 401 SESSION_NOT_VERIFIED (D-055), 404, 409 PENDING_NOT_ACTIVE, CONFIRMATION_REQUIRED, RULES_CHANGED, 403 STEP_UP_REQUIRED,
    410 PENDING_EXPIRED, 422. Role: doc 07 s3.3 commit row. The Idempotency-Key ({id}) is accepted but not needed:
    the row lock plus the stored result make every repeat a replay."""
    return _run(request, pending_id, commit_pending, verb="commit", needs_turn=True)


def pending_submit(request: Request, pending_id: str) -> JSONResponse:
    """What: send an awaiting_customer action for approval. Use when: the confirm module sends a strict yes or
    "Send for approval" press. Inputs: request (X-Session-Id, X-Turn-Id), pending id. Output: 200 SubmitResult.
    Side effects: one Pending approval ticket. Raises as commit. Role: doc 07 s3.3 submit row."""
    return _run(request, pending_id, submit_pending, verb="submit", needs_turn=True)


def pending_cancel(request: Request, pending_id: str) -> JSONResponse:
    """What: cancel an open action. Use when: strict no or Cancel press. Inputs: request (X-Session-Id), pending id.
    Output: 200 {pending_action_id, state: cancelled, changed: false}. Side effects: one UPDATE, no ticket.
    Raises CoreError 404, 409 PENDING_NOT_ACTIVE. Role: doc 07 s3.3 cancel row."""
    return _run(request, pending_id, cancel_pending, verb="cancel", needs_turn=False)


def pending_status_route(request: Request, pending_id: str) -> JSONResponse:
    """What: the status of one pending action. Use when: tools or the confirm module check a row.
    Inputs: request (X-Session-Id), pending id. Output: 200 PendingStatus. Side effects: reads only.
    Raises CoreError 404. Role: doc 07 s3.3 status row."""
    ctx = get_ctx(request)
    session_id = ctx.require_session()
    pid = _pending_id(pending_id)
    with _db(request).tx() as conn:
        out = pending_status(conn, clock=request.app.state.clock, pending_id=pid, session_id=session_id)
    return JSONResponse(out.model_dump(mode="json"))
