"""What: knowledge errors in the contract envelope and the FastAPI handlers that emit them.
Use when: raising ApiError in a route or dependency; create_app installs the handlers.
Inputs: status, code, class, message. Output: JSON {"error": {...}} bodies per doc 07 s1.3.
Side effects: none. Role: guarantees every non-2xx from knowledge, including 404, 405, 422 and 500, is an ErrorEnvelope
with the trace id. Codes: UNAUTHORIZED 401, NOT_FOUND 404, INVALID_REQUEST 422, RETRIEVAL_UNAVAILABLE 503 (doc 07 s4.1)."""
from __future__ import annotations

from fastapi import Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from contracts.errors import ErrorBody, ErrorEnvelope
from knowledge.ids import uuid7


class ApiError(Exception):
    """What: an error to return as an envelope. Use when: a route, dependency or check must fail.
    Inputs: status, code, class_, message, retryable, retry_after_s, headers. Output: exception.
    Side effects: none. Role: the one way knowledge code signals a non-2xx."""

    def __init__(self, status: int, code: str, class_: str, message: str, retryable: bool = False,
                 retry_after_s: int | None = None, headers: dict[str, str] | None = None,
                 details: dict | None = None):
        self.details = details or {}
        self.status, self.code, self.class_, self.message = status, code, class_, message
        self.retryable, self.retry_after_s, self.headers = retryable, retry_after_s, headers or {}


def _respond(request: Request, status: int, code: str, class_: str, message: str, retryable: bool = False,
             retry_after_s: int | None = None, headers: dict[str, str] | None = None,
             details: dict | None = None) -> JSONResponse:
    """What: build the envelope response with trace headers. Use when: any handler. Inputs: error parts.
    Output: JSONResponse. Side effects: none. Role: single place that shapes the body (changed is false:
    knowledge never mutates on a request)."""
    trace_id = getattr(request.state, "trace_id", None) or uuid7()
    body = ErrorEnvelope(error=ErrorBody(**{"class": class_}, code=code, message=message, retryable=retryable,
                                         changed=False, retry_after_s=retry_after_s, fault_injected=False,
                                         trace_id=trace_id, details=details or {}))
    out = {"X-Trace-Id": trace_id, "X-Request-Id": getattr(request.state, "request_id", "") or uuid7(), **(headers or {})}
    return JSONResponse(body.model_dump(mode="json"), status_code=status, headers=out)


async def _api_error(request: Request, exc: ApiError) -> JSONResponse:
    """What: handler for ApiError. Use when: FastAPI dispatches it. Inputs: request, exc.
    Output: envelope response. Side effects: none. Role: ApiError to envelope."""
    return _respond(request, exc.status, exc.code, exc.class_, exc.message, exc.retryable, exc.retry_after_s, exc.headers, exc.details)


async def _http_error(request: Request, exc: StarletteHTTPException) -> JSONResponse:
    """What: handler for framework 404 and 405 (and any other HTTPException). Use when: no route or wrong method.
    Inputs: request, exc. Output: envelope response. Side effects: none. Role: no raw FastAPI error bodies."""
    if exc.status_code == 404:
        return _respond(request, 404, "NOT_FOUND", "not_found", "That resource does not exist.")
    return _respond(request, exc.status_code, "INVALID_REQUEST", "validation", "That request is not allowed here.",
                    headers=dict(exc.headers or {}))


async def _validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
    """What: handler for request validation failures. Use when: a body or query does not match its model.
    Inputs: request, exc. Output: 422 envelope INVALID_REQUEST. Side effects: none. Role: doc 07 s4.1."""
    return _respond(request, 422, "INVALID_REQUEST", "validation", "The request was not valid.")


async def _unhandled(request: Request, exc: Exception) -> JSONResponse:
    """What: handler for unexpected exceptions. Use when: anything not handled above is raised.
    Inputs: request, exc. Output: 500 envelope INTERNAL without the exception text. Side effects: none.
    Role: never leak internals; the trace id lets the log line be found."""
    return _respond(request, 500, "INTERNAL", "retrieval_failed", "Something went wrong on our side.", retryable=True)


def install(app) -> None:
    """What: register all handlers on a FastAPI app. Use when: create_app. Inputs: app. Output: none.
    Side effects: mutates app. Role: wiring."""
    app.add_exception_handler(ApiError, _api_error)
    app.add_exception_handler(StarletteHTTPException, _http_error)
    app.add_exception_handler(RequestValidationError, _validation_error)
    app.add_exception_handler(Exception, _unhandled)
