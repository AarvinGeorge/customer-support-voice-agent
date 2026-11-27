"""What: the demo `tool_error` fault for core: a route dependency that fails a call before any work starts.
Use when: the route table registers a faultable route (prepare and commit; core.routes.FAULTABLE).
Inputs: the RequestContext (core.auth already dropped X-Demo-Fault unless DEMO_FAULTS_ENABLED is true) and the app state.
Output: tool_error_fault (a FastAPI dependency), FAULT_EVENT.
Side effects: when it fires, one audit row (event fault_injected) written in its own short transaction, one log line,
then it raises 503 TOOL_ERROR (retryable true, changed false, fault_injected true).
Role: doc 07 s10. It runs after the scope check and before the handler, so before any business transaction opens, any
idempotency row, any body validation. It adds no code path to a commit: an injected fault only makes a call fail
before any effect, so a faulted commit leaves the row `prepared`."""
from __future__ import annotations

import logging
import uuid

from fastapi import Request

from core.audit import AuditEvent, write_audit
from core.auth import get_ctx
from core.errors import CoreError

log = logging.getLogger("core.faults")
FAULT_EVENT = "fault_injected"


def tool_error_fault(request: Request) -> None:
    """What: raise the injected TOOL_ERROR when this request carries a honoured `tool_error` fault.
    Use when: as an extra dependency of the prepare and commit routes. Inputs: request (its RequestContext).
    Output: none when there is no fault. Side effects: on a fault, one audit_events INSERT in a separate transaction
    (a failure to write it is logged and does not change the answer) and a log line; raises CoreError TOOL_ERROR with
    fault_injected true. Role: the only place core injects a fault; other routes never look at the header."""
    ctx = get_ctx(request)
    if ctx.demo_fault != "tool_error":
        return
    route = request.scope.get("route")
    endpoint = f"{request.method} {getattr(route, 'path', request.url.path)}"
    db = request.app.state.db
    if db is not None:
        try:
            with db.tx() as conn:
                write_audit(conn, AuditEvent(
                    event=FAULT_EVENT, actor="agent", trace_id=ctx.trace_id,
                    session_id=uuid.UUID(ctx.session_id) if ctx.session_id else None, mode=ctx.mode,
                    decision="error", args={"fault": "tool_error", "endpoint": endpoint}, result_status=503,
                    idempotency_key=ctx.idempotency_key), clock=request.app.state.clock)
        except Exception:  # noqa: BLE001  a fault must only fail the call, never change its kind
            log.exception("fault audit row could not be written trace=%s", ctx.trace_id)
    log.warning("fault_injected fault=tool_error endpoint=%s caller=%s trace=%s", endpoint, ctx.caller, ctx.trace_id)
    raise CoreError("TOOL_ERROR", fault_injected=True)
