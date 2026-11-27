"""What: a small FastAPI core stub that answers the thin-slice core paths (doc 07 s3) with the
example bodies, checks for a bearer token, records every call, and honours X-Demo-Fault tool_error.
Use when: agent unit and integration tests, or a local agent run before lane A's core exists.
Run (from packages/contracts): uv run --extra stub uvicorn contracts.stubs.core_stub:app --app-dir src --port 8001
Inputs: HTTP requests with Authorization and the doc 07 s1.2 headers.
Output: contract-valid JSON bodies; errors in the doc 07 s1.3 envelope.
Side effects: appends a RecordedCall to app.state.calls per request (in memory only; the raw verify
body, PIN included, is kept: acceptable for a test double, never copy this log to a real sink).
Session state (in memory, per app): a session is unverified until a successful verify with its
X-Session-Id; three failures lock it; a commit is remembered so a second commit replays and the
status read says committed. A prepare opens the stub pending for the session: me/context reports it
(PendingPublic) until a commit or cancel, a commit or submit with the prepare's X-Turn-Id is 409
CONFIRMATION_REQUIRED, and a second cancel is 409 PENDING_NOT_ACTIVE.
Role: test double for core; it applies no business rules (one fixed persona, fixed decisions).
Paths for step-up, billing email, grace extension, preferences, profile and operators are designed only."""
import json
import re
from dataclasses import dataclass, field
from typing import Any

from fastapi import FastAPI, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.base import BaseHTTPMiddleware

from contracts import VERSION
from contracts.core_api import RefundPrepareRequest, TicketCreate, UnverifiedClaims, VerifyRequest
from contracts.errors import CORE_ERROR_CODES, ErrorBody, ErrorEnvelope
from contracts.limits import VERIFY_FAILURES_PER_SESSION
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
    Output: FastAPI app; app.state.calls is the call log, app.state.verified the verified session
    ids, app.state.failed failures per session, app.state.committed committed pending ids,
    app.state.open the prepare turn id per session with an open pending, app.state.cancelled cancelled ids.
    Side effects: none. Role: factory."""
    app = FastAPI(title="Quillstack core stub", version=VERSION)
    app.state.calls = []
    app.state.verified = set()
    app.state.failed = {}
    app.state.committed = set()
    app.state.open = {}
    app.state.cancelled = set()
    app.add_middleware(_AuthAndRecord)

    def _sid(request: Request) -> str:
        """What: the caller's X-Session-Id ("" if absent). Use when: session state lookups.
        Inputs: request. Output: str. Side effects: none. Role: helper."""
        return request.headers.get("x-session-id", "")

    def _not_verified(request: Request) -> JSONResponse | None:
        """What: 401 SESSION_NOT_VERIFIED unless the caller's session verified. Use when: account
        reads and prepare. Inputs: request. Output: error response or None. Side effects: none.
        Role: doc 07 s3.1 me/context error row."""
        if _sid(request) in app.state.verified:
            return None
        return error_response("SESSION_NOT_VERIFIED", "Please verify first.",
                              trace_id=request.headers.get("x-trace-id"))

    @app.exception_handler(RequestValidationError)
    async def _validation(request: Request, exc: RequestValidationError) -> JSONResponse:
        """What: 422 VALIDATION_ERROR envelope with the caller's trace id. Use when: a body or query
        fails validation. Inputs: request, error. Output: response. Side effects: none. Role: error shape."""
        return error_response("VALIDATION_ERROR", "The request was not valid.",
                              details={"errors": [str(e.get("msg")) for e in exc.errors()]},
                              trace_id=request.headers.get("x-trace-id"))

    @app.exception_handler(StarletteHTTPException)
    async def _http(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        """What: unknown path (404) or wrong method (405) in the error envelope. Use when: routing
        fails. Inputs: request, error. Output: response. Side effects: none. Role: s1.3 every non-2xx."""
        trace = request.headers.get("x-trace-id")
        if exc.status_code == 405:
            body = ErrorBody.model_validate({"class": "validation", "code": "METHOD_NOT_ALLOWED",
                                             "message": "Method not allowed.", "retryable": False,
                                             "changed": False, "fault_injected": False,
                                             "trace_id": trace, "details": {}})
            return JSONResponse(ErrorEnvelope(error=body).model_dump(mode="json"), status_code=405)
        return error_response("NOT_FOUND", "Not found.", trace_id=trace)

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
    def verify(body: VerifyRequest, request: Request) -> Any:
        """What: verify by account id and PIN (QS-10423 and 4821 succeed) for the caller's session.
        Use when: ingress verify. Inputs: VerifyRequest, X-Session-Id.
        Output: Claims, 401 VERIFY_FAILED {attempts_left}, or 423 VERIFY_LOCKED on the third failure.
        Side effects: marks the session verified, or counts a failure. Role: s3.1."""
        sid = _sid(request)
        failed = app.state.failed.get(sid, 0)
        if failed >= VERIFY_FAILURES_PER_SESSION:
            return error_response("VERIFY_LOCKED", "Verification is locked for this conversation.",
                                  details={"ticket": None})
        normalized = re.sub(r"^QS-?([0-9]{5})$", r"QS-\1", re.sub(r"\s+", "", body.account_id.upper()))
        if normalized == ex.ACCOUNT_ID and body.pin == ex.STUB_PIN:
            app.state.verified.add(sid)
            return _dump(ex.CLAIMS.model_copy(update={"session_id": sid or ex.SESSION_ID}))
        failed += 1
        app.state.failed[sid] = failed
        if failed >= VERIFY_FAILURES_PER_SESSION:
            return error_response("VERIFY_LOCKED", "Verification is locked for this conversation.",
                                  details={"ticket": None})
        return error_response("VERIFY_FAILED", "That account ID and PIN didn't match.",
                              details={"attempts_left": VERIFY_FAILURES_PER_SESSION - failed, "ticket": None})

    @app.get("/v1/sessions/{session_id}/claims")
    def claims(session_id: str) -> dict[str, Any]:
        """What: claims for the session: the persona if verified, else the unverified shape.
        Use when: ingress. Inputs: session id. Output: Claims or UnverifiedClaims.
        Side effects: none. Role: s3.1 (unknown session returns verified false)."""
        if session_id in app.state.verified:
            return _dump(ex.CLAIMS.model_copy(update={"session_id": session_id}))
        failed = app.state.failed.get(session_id, 0)
        return _dump(UnverifiedClaims(verified=False, verify_locked=failed >= VERIFY_FAILURES_PER_SESSION,
                                      failed_verifies=failed,
                                      attempts_left=max(0, VERIFY_FAILURES_PER_SESSION - failed)))

    @app.get("/v1/me/context", response_model=None)
    def me_context(request: Request) -> Any:
        """What: MeContext, or 401 SESSION_NOT_VERIFIED. Use when: memory refresh. Inputs: request.
        Output: MeContext or error. Side effects: none. Role: s3.1."""
        pending = ex.PENDING_PUBLIC if _sid(request) in app.state.open else None
        return _not_verified(request) or _dump(ex.ME_CONTEXT.model_copy(
            update={"claims": ex.CLAIMS.model_copy(update={"session_id": _sid(request)}), "pending_action": pending}))

    @app.get("/v1/me/charges", response_model=None)
    def me_charges(request: Request, ending: str | None = Query(default=None, pattern=r"^[0-9]{4}$"),
                   on: str | None = None, since_days: int = Query(default=90, ge=1, le=400),
                   limit: int = Query(default=10, ge=1, le=20)) -> Any:
        """What: charges filtered by ending and date, or 401 SESSION_NOT_VERIFIED.
        Use when: list_charges. Inputs: query params (s3.2 bounds). Output: ChargesResponse or error.
        Side effects: none. Role: s3.2."""
        if (err := _not_verified(request)) is not None:
            return err
        rows = [c for c in ex.CHARGES
                if (ending is None or (c.invoice_number or "").endswith(ending))
                and (on is None or c.charge_date == on)][:limit]
        return {"charges": [_dump(c) for c in rows], "ambiguous": len(rows) > 1}

    @app.get("/v1/me/tickets")
    def me_tickets(request: Request, status: str = "open", ending: str | None = None) -> dict[str, Any]:
        """What: the persona's tickets when verified; none for an unverified session (it has no
        tickets in the stub). Use when: list_tickets. Inputs: query params.
        Output: TicketsResponse. Side effects: none. Role: s3.2."""
        verified = _sid(request) in app.state.verified
        rows = [ex.TICKET_VIEW] if verified and ending in (None, ex.TICKET_VIEW.short_id) else []
        return {"tickets": [_dump(t) for t in rows]}

    @app.post("/v1/actions/refund/prepare", response_model=None)
    def refund_prepare(body: RefundPrepareRequest, request: Request) -> Any:
        """What: always "allowed" (Pro within limits) for a verified session. Use when: prepare_refund.
        Inputs: RefundPrepareRequest. Output: PrepareResult or 401. Side effects: none. Role: s3.3."""
        if (err := _not_verified(request)) is not None:
            return err
        app.state.open[_sid(request)] = request.headers.get("x-turn-id", "")
        app.state.committed.discard(ex.PENDING_ID)
        app.state.cancelled.discard(ex.PENDING_ID)
        return _dump(ex.PREPARE_ALLOWED)

    @app.get("/v1/actions/pending/{pending_id}")
    def pending_status(pending_id: str) -> Any:
        """What: pending status for the stub id: prepared, or committed with its result after a commit.
        Use when: status check. Inputs: id. Output: PendingStatus or 404. Side effects: none. Role: s3.3."""
        if pending_id != ex.PENDING_ID:
            return error_response("NOT_FOUND", "Not found.")
        if pending_id in app.state.committed:
            return _dump(ex.PENDING_STATUS.model_copy(update={"state": "committed", "result": ex.COMMIT_RESULT}))
        return _dump(ex.PENDING_STATUS)

    def _same_turn(request: Request) -> JSONResponse | None:
        """What: 409 CONFIRMATION_REQUIRED when the caller's X-Turn-Id is the prepare's turn. Use when: commit,
        submit. Inputs: request. Output: error or None. Side effects: none. Role: s3.3 "later turn" check."""
        prepared_in = app.state.open.get(_sid(request))
        if prepared_in and prepared_in == request.headers.get("x-turn-id"):
            return error_response("CONFIRMATION_REQUIRED", "Confirm in a later turn.")
        return None

    @app.post("/v1/actions/pending/{pending_id}/commit")
    def commit(pending_id: str, request: Request) -> Any:
        """What: commit result; a repeat returns the stored result with replayed true.
        Use when: ingress confirm. Inputs: id. Output: CommitResult or 404.
        Side effects: remembers the commit. Role: s3.3 replay rule."""
        if pending_id != ex.PENDING_ID:
            return error_response("NOT_FOUND", "Not found.")
        if (err := _same_turn(request)) is not None:
            return err
        replayed = pending_id in app.state.committed
        app.state.committed.add(pending_id)
        app.state.open.pop(_sid(request), None)
        return _dump(ex.COMMIT_RESULT.model_copy(update={"replayed": replayed}))

    @app.post("/v1/actions/pending/{pending_id}/submit")
    def submit(pending_id: str, request: Request) -> Any:
        """What: submit result. Use when: ingress confirm for needs_approval. Inputs: id.
        Output: SubmitResult or 404. Side effects: none. Role: s3.3."""
        if pending_id != ex.PENDING_ID:
            return error_response("NOT_FOUND", "Not found.")
        if (err := _same_turn(request)) is not None:
            return err
        return _dump(ex.SUBMIT_RESULT)

    @app.post("/v1/actions/pending/{pending_id}/cancel")
    def cancel(pending_id: str, request: Request) -> Any:
        """What: cancel result; a second cancel is 409 PENDING_NOT_ACTIVE. Use when: strict no or Cancel.
        Inputs: id. Output: CancelResult, 404 or 409. Side effects: closes the session's pending. Role: s3.3."""
        if pending_id != ex.PENDING_ID:
            return error_response("NOT_FOUND", "Not found.")
        if pending_id in app.state.cancelled:
            return error_response("PENDING_NOT_ACTIVE", "That request is no longer active.")
        app.state.cancelled.add(pending_id)
        app.state.open.pop(_sid(request), None)
        return _dump(ex.CANCEL_RESULT)

    @app.post("/v1/tickets")
    def create_ticket(body: TicketCreate) -> dict[str, Any]:
        """What: ticket created (fixed ticket ending 8213). Use when: create_ticket or escalation.
        Inputs: TicketCreate. Output: TicketCreateResponse. Side effects: none. Role: s3.4."""
        return _dump(ex.TICKET_CREATED)

    return app


app = create_app()
