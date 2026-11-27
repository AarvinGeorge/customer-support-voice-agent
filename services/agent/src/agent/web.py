"""What: HTTP plumbing for the agent app: request ids, trace ids, X-Served-By, error envelope handlers.
Use when: create_app installs them once; routes call error_response for any non-2xx.
Inputs: Settings (served_by).
Output: install_http(app, settings), error_response(...).
Side effects: adds response headers; logs unhandled exceptions (no body or secret in the response).
Role: doc 07 s1.2 headers (X-Request-Id, X-Trace-Id, X-Served-By) and s1.3 envelope on every non-2xx.
"""
from __future__ import annotations

import logging
import uuid
from typing import Any

from contracts.errors import ErrorBody, ErrorEnvelope
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from langchain_core.utils.uuid import uuid7
from starlette.exceptions import HTTPException as StarletteHTTPException

log = logging.getLogger("agent.web")


def _valid_uuid(value: str | None) -> str | None:
    """What: return the canonical uuid string or None. Use when: accepting X-Request-Id.
    Inputs: header value. Output: str or None. Side effects: none. Role: header hygiene."""
    if not value:
        return None
    try:
        return str(uuid.UUID(value))
    except ValueError:
        return None


def error_response(request: Request, status: int, class_: str, code: str, message: str, *,
                   retryable: bool = False, changed: bool | None = False, retry_after_s: int | None = None,
                   fault_injected: bool = False, details: dict[str, Any] | None = None) -> JSONResponse:
    """What: a doc 07 s1.3 envelope response carrying the request's trace id.
    Use when: any agent non-2xx. Inputs: request, HTTP status, class, code, customer-safe message, flags.
    Output: JSONResponse (Retry-After set when retry_after_s is given). Side effects: none.
    Role: error builder."""
    body = ErrorBody.model_validate({"class": class_, "code": code, "message": message, "retryable": retryable,
                                     "changed": changed, "retry_after_s": retry_after_s,
                                     "fault_injected": fault_injected,
                                     "trace_id": getattr(request.state, "trace_id", None),
                                     "details": details or {}})
    headers = {"Retry-After": str(retry_after_s)} if retry_after_s is not None else None
    return JSONResponse(ErrorEnvelope(error=body).model_dump(mode="json"), status_code=status, headers=headers)


def install_http(app: FastAPI, served_by: str) -> None:
    """What: add the id and header middleware plus the envelope exception handlers.
    Use when: once in create_app. Inputs: app, replica name.
    Output: none. Side effects: mutates app. Role: s1.2 and s1.3 wiring.
    Outside a turn X-Trace-Id is a fresh uuid7; turn routes overwrite request.state.trace_id with turn_id."""

    @app.middleware("http")
    async def ids_and_served_by(request: Request, call_next: Any) -> Any:
        """What: mint or accept ids, run the request, stamp headers (also on errors).
        Use when: every request. Inputs: request. Output: response. Side effects: logs unhandled errors.
        Role: header middleware; an unhandled exception becomes a 500 envelope here, inside the headers."""
        request.state.request_id = _valid_uuid(request.headers.get("x-request-id")) or str(uuid.uuid4())
        request.state.trace_id = str(uuid7())
        try:
            response = await call_next(request)
        except Exception:  # noqa: BLE001 - last line of defence; never leak internals
            log.exception("unhandled error request_id=%s trace_id=%s", request.state.request_id,
                          request.state.trace_id)
            response = error_response(request, 500, "provider_5xx", "INTERNAL",
                                      "Something went wrong on our side. Please try again.",
                                      retryable=True, changed=None)
        response.headers["X-Request-Id"] = request.state.request_id
        response.headers["X-Trace-Id"] = request.state.trace_id
        response.headers["X-Served-By"] = served_by
        return response

    @app.exception_handler(StarletteHTTPException)
    async def http_error(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        """What: 404 and 405 (and any other routing HTTPException) as envelopes.
        Use when: routing fails. Inputs: request, exc. Output: response. Side effects: none. Role: s1.3."""
        if exc.status_code == 404:
            return error_response(request, 404, "not_found", "NOT_FOUND", "Not found.")
        if exc.status_code == 405:
            return error_response(request, 405, "validation", "METHOD_NOT_ALLOWED", "Method not allowed.")
        return error_response(request, exc.status_code, "validation", "HTTP_ERROR", "The request was not accepted.")

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        """What: 422 VALIDATION_ERROR envelope (messages only, no input echo).
        Use when: body or query validation fails. Inputs: request, exc. Output: response.
        Side effects: none. Role: s1.3."""
        return error_response(request, 422, "validation", "VALIDATION_ERROR", "The request was not valid.",
                              details={"errors": [str(e.get("msg")) for e in exc.errors()]})
