"""What: the core error type, the envelope builder and the exception handlers (doc 07 s1.3).
Use when: a handler or dependency must fail (raise CoreError) or the app is assembled (install_handlers).
Inputs: a contract error code, optional message, details, retry_after_s.
Output: CoreError, envelope_response, core_error_response, install_handlers, STUB_NOT_IMPLEMENTED.
Side effects: none beyond building responses.
Role: every non-2xx body from core is a contracts ErrorEnvelope; status, retryable and changed come from
contracts CORE_ERROR_CODES, so they cannot drift from doc 07."""
from __future__ import annotations

from typing import Any

from contracts.errors import CORE_ERROR_CODES, ErrorBody, ErrorEnvelope
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

_CLASS = {
    "AUTH_INVALID": "access_denied", "SCOPE_DENIED": "access_denied", "SESSION_NOT_VERIFIED": "access_denied",
    "VERIFY_FAILED": "access_denied", "VERIFY_LOCKED": "access_denied", "STEP_UP_REQUIRED": "access_denied",
    "ROLE_NOT_PERMITTED": "access_denied", "NOT_FOUND": "not_found", "ACTION_DISABLED": "not_found",
    "IDENTITY_CONFLICT": "core_conflict", "AMBIGUOUS_REFERENCE": "core_conflict",
    "ACTION_IN_PROGRESS": "core_conflict", "PENDING_NOT_ACTIVE": "core_conflict",
    "CONFIRMATION_REQUIRED": "core_conflict", "RULES_CHANGED": "core_conflict",
    "INVALID_TRANSITION": "core_conflict", "PENDING_EXPIRED": "core_conflict",
    "VALIDATION_ERROR": "validation", "TICKET_LIMIT_REACHED": "rate_limited",
    "TOOL_ERROR": "core_unavailable", "INTERNAL": "core_unavailable",
}
_MESSAGE = {
    "AUTH_INVALID": "The request is not authorised.",
    "SCOPE_DENIED": "This caller may not use that endpoint.",
    "NOT_FOUND": "That does not exist.",
    "VALIDATION_ERROR": "The request is not valid.",
    "INTERNAL": "Something went wrong on our side.",
    "TOOL_ERROR": "The service is temporarily unavailable.",
}
# Temporary: stub handlers answer 501 with this code until the task that owns the route fills it.
STUB_NOT_IMPLEMENTED = "NOT_IMPLEMENTED"


class CoreError(Exception):
    """What: an error with a contract code. Use when: a handler or dependency must fail with a core code.
    Inputs: code (key of CORE_ERROR_CODES), message, details, retry_after_s, klass (override of the error class).
    Output: exception carrying everything the envelope needs. Side effects: none.
    Role: the only way core code signals a non-2xx outcome."""

    def __init__(self, code: str, message: str | None = None, details: dict[str, Any] | None = None,
                 retry_after_s: int | None = None, klass: str | None = None) -> None:
        """What: store the error facts. Use when: raising. Inputs: see class. Output: none.
        Side effects: raises KeyError for a code that is not in the contract table. Role: typo guard."""
        if code not in CORE_ERROR_CODES:
            raise KeyError(f"{code} is not a core error code in doc 07 s1.3")
        super().__init__(code)
        self.code = code
        self.message = message or _MESSAGE.get(code, "The request could not be completed.")
        self.details = details or {}
        self.retry_after_s = retry_after_s
        self.klass = klass or _CLASS[code]


def _ids(request: Request) -> tuple[str, str]:
    """What: read the request and trace ids the middleware stored. Use when: building a response.
    Inputs: request. Output: (request_id, trace_id), empty strings if the middleware has not run.
    Side effects: none. Role: lets error responses carry ids even on early failures."""
    st = request.state
    return getattr(st, "request_id", ""), getattr(st, "trace_id", "")


def envelope_response(request: Request, status: int, body: ErrorBody) -> JSONResponse:
    """What: wrap an ErrorBody in the envelope response with the correlation headers.
    Use when: any handler returns an error. Inputs: request, HTTP status, body. Output: JSONResponse.
    Side effects: none. Role: one place that serialises errors."""
    rid, tid = _ids(request)
    headers = {"X-Request-Id": rid, "X-Trace-Id": tid} if rid else {}
    if body.retry_after_s is not None:
        headers["Retry-After"] = str(body.retry_after_s)
    return JSONResponse(ErrorEnvelope(error=body).model_dump(mode="json"), status_code=status, headers=headers)


def core_error_response(request: Request, exc: CoreError) -> JSONResponse:
    """What: turn a CoreError into the envelope response using CORE_ERROR_CODES.
    Use when: the CoreError handler runs. Inputs: request, exception. Output: JSONResponse.
    Side effects: none. Role: status, retryable and changed come from the contract table."""
    status, retryable, changed = CORE_ERROR_CODES[exc.code]
    body = ErrorBody.model_validate({
        "class": exc.klass, "code": exc.code, "message": exc.message, "retryable": retryable, "changed": changed,
        "retry_after_s": exc.retry_after_s, "fault_injected": False, "trace_id": _ids(request)[1] or None,
        "details": exc.details})
    return envelope_response(request, status, body)


def install_handlers(app: FastAPI) -> None:
    """What: register handlers so every error leaves as an envelope. Use when: building the app.
    Inputs: app. Output: none. Side effects: mutates app. Role: CoreError, unknown route or method,
    request validation (never echoing input, which could hold a PIN) and any unhandled exception (INTERNAL)."""

    @app.exception_handler(CoreError)
    async def _core(request: Request, exc: CoreError) -> JSONResponse:
        """What: handle CoreError. Use when: raised. Inputs: request, exc. Output: envelope.
        Side effects: none. Role: see install_handlers."""
        return core_error_response(request, exc)

    @app.exception_handler(StarletteHTTPException)
    async def _http(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        """What: handle router-level 404 and 405 as NOT_FOUND. Use when: no route matches. Inputs: request, exc.
        Output: envelope. Side effects: none. Role: no FastAPI default bodies leak out."""
        code = "NOT_FOUND" if exc.status_code in (404, 405) else "INTERNAL"
        return core_error_response(request, CoreError(code))

    @app.exception_handler(RequestValidationError)
    async def _validation(request: Request, exc: RequestValidationError) -> JSONResponse:
        """What: handle body, query and path validation failures. Use when: pydantic rejects input.
        Inputs: request, exc. Output: 422 envelope with only loc, type and msg per error (no input value).
        Side effects: none. Role: doc 07 s1.3 plus s11, a rejected PIN must not be echoed."""
        errors = [{"loc": list(e.get("loc", ())), "type": e.get("type", ""), "msg": e.get("msg", "")}
                  for e in exc.errors()]
        return core_error_response(request, CoreError("VALIDATION_ERROR", details={"errors": errors}))

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception) -> JSONResponse:
        """What: handle any unexpected exception. Use when: a handler crashes. Inputs: request, exc.
        Output: 500 INTERNAL envelope without the exception text. Side effects: none.
        Role: changed is null (unknown)."""
        return core_error_response(request, CoreError("INTERNAL"))
