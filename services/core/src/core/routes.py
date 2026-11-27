"""What: the core /v1 routes the thin slice builds (doc 07 s3), each bound to its scope.
Use when: the app is assembled. Inputs: none. Output: SLICE_ROUTES, register_routes.
Side effects: registers the scopes in auth.ROUTE_SCOPES.
Role: the route table. Handlers are 501 stubs; the task that owns a route replaces its stub here
(tickets S3.T1, identity S1.T1, reads S13.T3 and actions S2.T1, T5 are built). Grace extension prepare is registered only
to answer 404 ACTION_DISABLED (doc 07 s3.3). Designed-only routes (operator, step-up, billing email, preferences,
profile; doc 09 s1.2) are deliberately absent."""
from __future__ import annotations

from collections.abc import Callable
from typing import Any

from contracts.errors import ErrorBody
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from core.actions_routes import (grace_prepare, pending_cancel, pending_commit, pending_status_route, pending_submit,
                                 refund_prepare)
from core.auth import scoped_route
from core.errors import STUB_NOT_IMPLEMENTED, envelope_response
from core.identity_routes import identity_verify, session_claims
from core.reads_routes import me_charges, me_context, me_tickets
from core.tickets_routes import tickets_create


def _stub(name: str) -> Callable[[Request], JSONResponse]:
    """What: build a 501 handler for a route not built yet. Use when: declaring the route table.
    Inputs: route name. Output: handler. Side effects: none.
    Role: lets the scope matrix run on every slice route before its logic exists."""

    def handler(request: Request) -> JSONResponse:
        """What: answer 501 NOT_IMPLEMENTED in the envelope. Use when: a stubbed route is called with the right scope.
        Inputs: request. Output: 501 envelope. Side effects: none. Role: placeholder."""
        body = ErrorBody.model_validate({
            "class": "core_unavailable", "code": STUB_NOT_IMPLEMENTED, "message": f"{name} is not built yet.",
            "retryable": False, "changed": False, "fault_injected": False, "trace_id": request.state.trace_id,
            "details": {}})
        return envelope_response(request, 501, body)

    handler.__name__ = f"stub_{name}"
    return handler


# (method, path, scope, name): doc 07 s3 scope column.
SLICE_ROUTES: list[tuple[str, str, str, str]] = [
    ("POST", "/v1/identity/verify", "identity.verify", "identity_verify"),
    ("GET", "/v1/sessions/{session_id}/claims", "claims.read", "session_claims"),
    ("GET", "/v1/me/context", "context.read", "me_context"),
    ("GET", "/v1/me/charges", "billing.read", "me_charges"),
    ("GET", "/v1/me/tickets", "tickets.read_own", "me_tickets"),
    ("POST", "/v1/actions/refund/prepare", "actions.prepare", "refund_prepare"),
    ("POST", "/v1/actions/grace_extension/prepare", "actions.prepare", "grace_prepare"),
    ("GET", "/v1/actions/pending/{pending_id}", "actions.status", "pending_status"),
    ("POST", "/v1/actions/pending/{pending_id}/commit", "actions.commit", "pending_commit"),
    ("POST", "/v1/actions/pending/{pending_id}/submit", "actions.submit", "pending_submit"),
    ("POST", "/v1/actions/pending/{pending_id}/cancel", "actions.cancel", "pending_cancel"),
    ("POST", "/v1/tickets", "tickets.create", "tickets_create"),
]


def register_routes(app: FastAPI, overrides: dict[str, Callable[..., Any]] | None = None) -> None:
    """What: register every slice route on the app. Use when: create_app. Inputs: the app, optional handlers by
    route name (how later tasks plug in). Output: none. Side effects: adds routes, fills auth.ROUTE_SCOPES.
    Role: single place the route set is defined (routes go on the app itself so tests can list them)."""
    built: dict[str, Callable[..., Any]] = {"tickets_create": tickets_create, "identity_verify": identity_verify,
                                            "session_claims": session_claims, "me_context": me_context, "me_charges": me_charges,
                                            "me_tickets": me_tickets, "refund_prepare": refund_prepare,
                                            "grace_prepare": grace_prepare, "pending_status": pending_status_route,
                                            "pending_commit": pending_commit, "pending_submit": pending_submit,
                                            "pending_cancel": pending_cancel, **(overrides or {})}
    for method, path, scope, name in SLICE_ROUTES:
        handler = built.get(name) or _stub(name)
        scoped_route(app, method, path, scope, handler, name=name, response_model=None)
