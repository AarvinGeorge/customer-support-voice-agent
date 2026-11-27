"""What: a small FastAPI core stub that answers the thin-slice core paths (doc 07 s3) with the
example bodies, checks for a bearer token, records every call, and honours X-Demo-Fault tool_error.
Use when: agent unit and integration tests, or a local agent run before lane A's core exists.
Run (from packages/contracts): uv run --extra stub uvicorn contracts.stubs.core_stub:app --app-dir src --port 8001
Inputs: HTTP requests with Authorization and the doc 07 s1.2 headers.
Output: contract-valid JSON bodies; errors in the doc 07 s1.3 envelope.
Side effects: appends a RecordedCall to app.state.calls per request (in memory only).
Role: test double for core; it applies no business rules (one fixed persona, fixed decisions).
Paths for step-up, billing email, grace extension, preferences and operators are designed only."""
import json
import re
from dataclasses import dataclass, field
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

from contracts import VERSION
from contracts.core_api import RefundPrepareRequest, TicketCreate, VerifyRequest
from contracts.errors import CORE_ERROR_CODES, ErrorBody, ErrorEnvelope
from contracts.stubs import examples as ex

STUB_PENDING_ID = ex.PENDING_ID
RECORDED_HEADERS = ("x-session-id", "x-trace-id", "x-request-id", "x-mode", "x-turn-id",
                    "idempotency-key", "x-demo-fault")
_FAULT_PATH_SUFFIXES = ("/prepare", "/commit")


@dataclass
class RecordedCall:
    """What: one request the stub received. Use when: a test asserts token and headers per tool.
    Inputs: method, path, token, headers, body. Output: dataclass. Side effects: none.
    Role: call log entry."""

    method: str
    path: str
    token: str | None
    headers: dict[str, str] = field(default_factory=dict)
    body: Any = None


_ERROR_CLASS = {401: "access_denied", 403: "access_denied", 404: "not_found", 409: "core_conflict",
                410: "core_conflict", 422: "validation", 423: "access_denied", 429: "rate_limited",
                500: "core_unavailable", 503: "core_unavailable"}


def error_response(code: str, message: str, details: dict[str, Any] | None = None,
                   fault_injected: bool = False, trace_id: str | None = None) -> JSONResponse:
    """What: a doc 07 s1.3 error envelope response for a core code.
    Use when: any stub error. Inputs: code, message, details, fault flag, trace id.
    Output: JSONResponse with the code's HTTP status. Side effects: none. Role: error builder."""
    status, retryable, changed = CORE_ERROR_CODES[code]
    body = ErrorBody.model_validate({"class": _ERROR_CLASS[status], "code": code, "message": message,
                                     "retryable": retryable, "changed": changed,
                                     "fault_injected": fault_injected, "trace_id": trace_id,
                                     "details": details or {}})
    return JSONResponse(ErrorEnvelope(error=body).model_dump(mode="json", by_alias=True),
                        status_code=status)


class _AuthAndRecord(BaseHTTPMiddleware):
    """What: records each call, rejects calls without a bearer token, applies the tool_error fault.
    Use when: installed by create_app. Inputs: request. Output: response. Side effects: appends to
    app.state.calls. Role: the stub's cross-cutting behaviour."""

    async def dispatch(self, request: Request, call_next):  # type: ignore[override]
        """What: middleware body. Use when: every request. Inputs: request, next handler.
        Output: response. Side effects: records the call. Role: see class."""
        auth = request.headers.get("authorization", "")
        token = auth[7:] if auth.lower().startswith("bearer ") and len(auth) > 7 else None
        raw = await request.body()
        try:
            body = json.loads(raw) if raw else None
        except ValueError:
            body = raw.decode(errors="replace")
        request.app.state.calls.append(RecordedCall(
            method=request.method, path=request.url.path, token=token,
            headers={h: request.headers[h] for h in RECORDED_HEADERS if h in request.headers}, body=body))
        trace = request.headers.get("x-trace-id")
        if request.url.path.startswith("/v1/") and token is None:
            return error_response("AUTH_INVALID", "Missing or invalid token.", trace_id=trace)
        if (request.method == "POST" and request.headers.get("x-demo-fault") == "tool_error"
                and request.url.path.endswith(_FAULT_PATH_SUFFIXES)):
            return error_response("TOOL_ERROR", "Our billing system didn't respond.",
                                  fault_injected=True, trace_id=trace)
        return await call_next(request)


def _dump(model: Any) -> dict[str, Any]:
    """What: JSON-mode dump with aliases. Use when: returning a body. Inputs: model.
    Output: dict. Side effects: none. Role: helper."""
    return model.model_dump(mode="json", by_alias=True)


def create_app() -> FastAPI:
    """What: build a fresh stub app with an empty call log.
    Use when: each test (fresh state) or the module-level app for uvicorn. Inputs: none.
    Output: FastAPI app; app.state.calls is the call log. Side effects: none. Role: factory."""
    app = FastAPI(title="Quillstack core stub", version=VERSION)
    app.state.calls = []
    app.add_middleware(_AuthAndRecord)

    @app.exception_handler(RequestValidationError)
    async def _validation(request: Request, exc: RequestValidationError) -> JSONResponse:
        """What: 422 VALIDATION_ERROR envelope. Use when: a body fails validation.
        Inputs: request, error. Output: response. Side effects: none. Role: error shape."""
        return error_response("VALIDATION_ERROR", "The request was not valid.",
                              details={"errors": [str(e.get("msg")) for e in exc.errors()]})

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        """What: liveness. Use when: health checks. Inputs: none. Output: {status}. Side effects: none.
        Role: doc 07 s3.6."""
        return {"status": "alive"}

    @app.get("/readyz")
    def readyz() -> dict[str, Any]:
        """What: readiness with versions. Use when: health checks. Inputs: none. Output: CoreReady.
        Side effects: none. Role: doc 07 s3.6."""
        return {"status": "ready", "contract_version": VERSION, "policy_version": ex.POLICY_VERSION,
                "demo_faults": True, "seed_date": "2026-11-27"}

    @app.post("/v1/identity/verify", response_model=None)
    def verify(body: VerifyRequest) -> Any:
        """What: verify by account id and PIN (QS-10423 and 4821 succeed). Use when: ingress verify.
        Inputs: VerifyRequest. Output: Claims or 401 VERIFY_FAILED. Side effects: none. Role: s3.1."""
        normalized = re.sub(r"^QS-?(\d{5})$", r"QS-\1", re.sub(r"\s+", "", body.account_id.upper()))
        if normalized == ex.ACCOUNT_ID and body.pin == ex.STUB_PIN:
            return _dump(ex.CLAIMS)
        return error_response("VERIFY_FAILED", "That account ID and PIN didn't match.",
                              details={"attempts_left": 2, "ticket": None})

    @app.get("/v1/sessions/{session_id}/claims")
    def claims(session_id: str) -> dict[str, Any]:
        """What: claims for the session (always the verified persona). Use when: ingress.
        Inputs: session id. Output: Claims. Side effects: none. Role: s3.1."""
        return _dump(ex.CLAIMS.model_copy(update={"session_id": session_id}))

    @app.get("/v1/me/context")
    def me_context() -> dict[str, Any]:
        """What: MeContext. Use when: memory refresh. Inputs: none. Output: MeContext.
        Side effects: none. Role: s3.1."""
        return _dump(ex.ME_CONTEXT)

    @app.get("/v1/me/profile")
    def me_profile() -> dict[str, Any]:
        """What: Profile. Use when: get_account_summary. Inputs: none. Output: Profile.
        Side effects: none. Role: s3.2."""
        return _dump(ex.PROFILE)

    @app.get("/v1/me/charges")
    def me_charges(ending: str | None = None, on: str | None = None, since_days: int = 90,
                   limit: int = 10) -> dict[str, Any]:
        """What: charges filtered by ending and date. Use when: list_charges.
        Inputs: query params. Output: ChargesResponse. Side effects: none. Role: s3.2."""
        rows = [c for c in ex.CHARGES
                if (ending is None or (c.invoice_number or "").endswith(ending))
                and (on is None or c.charge_date == on)][:limit]
        return {"charges": [_dump(c) for c in rows], "ambiguous": len(rows) > 1}

    @app.get("/v1/me/tickets")
    def me_tickets(status: str = "open", ending: str | None = None) -> dict[str, Any]:
        """What: the persona's tickets. Use when: list_tickets. Inputs: query params.
        Output: TicketsResponse. Side effects: none. Role: s3.2."""
        rows = [ex.TICKET_VIEW] if ending in (None, ex.TICKET_VIEW.short_id) else []
        return {"tickets": [_dump(t) for t in rows]}

    @app.post("/v1/actions/refund/prepare")
    def refund_prepare(body: RefundPrepareRequest) -> dict[str, Any]:
        """What: always "allowed" (Pro within limits). Use when: prepare_refund.
        Inputs: RefundPrepareRequest. Output: PrepareResult. Side effects: none. Role: s3.3."""
        return _dump(ex.PREPARE_ALLOWED)

    @app.get("/v1/actions/pending/{pending_id}")
    def pending_status(pending_id: str) -> Any:
        """What: pending status (prepared) for the stub id. Use when: status check.
        Inputs: id. Output: PendingStatus or 404. Side effects: none. Role: s3.3."""
        if pending_id != ex.PENDING_ID:
            return error_response("NOT_FOUND", "Not found.")
        return _dump(ex.PENDING_STATUS)

    @app.post("/v1/actions/pending/{pending_id}/commit")
    def commit(pending_id: str) -> Any:
        """What: commit result. Use when: ingress confirm. Inputs: id. Output: CommitResult or 404.
        Side effects: none. Role: s3.3."""
        if pending_id != ex.PENDING_ID:
            return error_response("NOT_FOUND", "Not found.")
        return _dump(ex.COMMIT_RESULT)

    @app.post("/v1/actions/pending/{pending_id}/submit")
    def submit(pending_id: str) -> Any:
        """What: submit result. Use when: ingress confirm for needs_approval. Inputs: id.
        Output: SubmitResult or 404. Side effects: none. Role: s3.3."""
        if pending_id != ex.PENDING_ID:
            return error_response("NOT_FOUND", "Not found.")
        return _dump(ex.SUBMIT_RESULT)

    @app.post("/v1/actions/pending/{pending_id}/cancel")
    def cancel(pending_id: str) -> Any:
        """What: cancel result. Use when: strict no or Cancel. Inputs: id. Output: CancelResult or 404.
        Side effects: none. Role: s3.3."""
        if pending_id != ex.PENDING_ID:
            return error_response("NOT_FOUND", "Not found.")
        return _dump(ex.CANCEL_RESULT)

    @app.post("/v1/tickets")
    def create_ticket(body: TicketCreate) -> dict[str, Any]:
        """What: ticket created (fixed ticket ending 8213). Use when: create_ticket or escalation.
        Inputs: TicketCreate. Output: TicketCreateResponse. Side effects: none. Role: s3.4."""
        return _dump(ex.TICKET_CREATED)

    return app


app = create_app()
