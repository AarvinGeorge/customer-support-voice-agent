"""What: the HTTP handlers for POST /v1/identity/verify and GET /v1/sessions/{session_id}/claims.
Use when: the app is assembled (routes.register_routes plugs them in by route name).
Inputs: the request (scopes `identity.verify` and `claims.read`, X-Session-Id for verify, the JSON body).
Output: identity_verify (200 Claims) and session_claims (200 Claims or the unverified answer).
Side effects: verify runs one transaction that holds the counters, the audit row and any lockout ticket; the
transaction commits before a failure is raised, so the counters survive the 401, 409 and 423 answers.
Role: doc 07 s3.1. The PIN is read once, normalised and handed to core.identity; it is never logged, audited or echoed
(the 422 handler shows only locations)."""
from __future__ import annotations

from fastapi import Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from core.auth import get_ctx, parse_uuid
from core.db import Database
from core.errors import CoreError
from core.identity import (VerifyOutcome, load_claims, normalize_account_id, normalize_pin, read_unverified,
                           verify)

_FAILED = "That account ID and PIN did not match."
_LOCKED = "Verification is locked after too many failed attempts."


class VerifyBody(BaseModel):
    """What: the verify request as typed or spoken. Use when: FastAPI parses the body. Inputs: account_id, pin
    (raw text, normalised by the handler). Output: model that rejects unknown fields. Side effects: none.
    Role: looser than contracts VerifyRequest on purpose, so spaces and dashes are normalised, not refused."""

    model_config = ConfigDict(extra="forbid")
    account_id: str = Field(min_length=1, max_length=64)
    pin: str = Field(min_length=1, max_length=16)


def _bad(field: str) -> CoreError:
    """What: a 422 for a field that is not the right shape after normalisation. Use when: normalise returns None.
    Inputs: field name. Output: CoreError (not raised). Side effects: none.
    Role: location only, never the value (it may be a PIN)."""
    return CoreError("VALIDATION_ERROR", details={"errors": [{"loc": ["body", field], "type": "format",
                                                               "msg": "not a valid " + field}]})


def _raise_for(outcome: VerifyOutcome) -> None:
    """What: turn a failed outcome into its envelope error. Use when: the transaction has committed.
    Inputs: outcome. Output: none. Side effects: raises CoreError VERIFY_FAILED (401, attempts_left), VERIFY_LOCKED
    (423, ticket) or IDENTITY_CONFLICT (409). Role: unknown account and wrong PIN reach the same branch and the same text."""
    ticket = outcome.ticket.model_dump(mode="json") if outcome.ticket else None
    if outcome.kind == "failed":
        raise CoreError("VERIFY_FAILED", _FAILED, details={"attempts_left": outcome.attempts_left, "ticket": None})
    if outcome.kind == "locked":
        raise CoreError("VERIFY_LOCKED", _LOCKED, details={"ticket": ticket})
    raise CoreError("IDENTITY_CONFLICT", "This chat is already verified as a different person.")


def identity_verify(request: Request, body: VerifyBody) -> JSONResponse:
    """What: verify an account id and PIN for the request's session. Use when: ingress verification code (card or
    voice rule V) calls core. Inputs: request, body. Output: 200 Claims. Side effects: one transaction (see core.identity).
    Raises CoreError VALIDATION_ERROR (no session header, bad shape), VERIFY_FAILED, VERIFY_LOCKED, IDENTITY_CONFLICT,
    TOOL_ERROR (no database or no pepper configured). Role: the route; the rules live in core.identity."""
    ctx = get_ctx(request)
    session_id = ctx.require_session()
    policy, clock, settings = request.app.state.policy, request.app.state.clock, request.app.state.settings
    account_id = normalize_account_id(body.account_id)
    if account_id is None:
        raise _bad("account_id")
    pin = normalize_pin(body.pin, policy.identity.pin_digits)
    if pin is None:
        raise _bad("pin")
    db: Database | None = request.app.state.db
    if db is None or not settings.pin_pepper:
        raise CoreError("TOOL_ERROR", retry_after_s=5, klass="core_unavailable" if db else "db_unavailable")
    with db.tx() as conn:
        outcome = verify(conn, policy=policy, clock=clock, pepper=settings.pin_pepper, session_id=session_id,
                         account_id=account_id, pin=pin, trace_id=ctx.trace_id, mode=ctx.mode)
    if outcome.kind == "ok" and outcome.claims is not None:
        return JSONResponse(outcome.claims.model_dump(mode="json"))
    _raise_for(outcome)
    raise AssertionError("unreachable")  # _raise_for always raises


def session_claims(request: Request, session_id: str) -> JSONResponse:
    """What: the verified claims of a session, or the unverified answer with its counters.
    Use when: the agent asks who a session is. Inputs: request, the path session id (a uuid, checked by the gate).
    Output: 200 Claims or {verified: false, verify_locked, failed_verifies, attempts_left}. Side effects: reads.
    Role: doc 07 s3.1; an unknown or expired session is `verified: false`, never an error."""
    sid = parse_uuid(session_id, ["path", "session_id"])
    db: Database | None = request.app.state.db
    if db is None:
        raise CoreError("TOOL_ERROR", retry_after_s=5, klass="db_unavailable")
    policy, clock = request.app.state.policy, request.app.state.clock
    with db.tx() as conn:
        claims = load_claims(conn, sid, clock.now(), policy)
        body = claims if claims is not None else read_unverified(conn, sid, policy)
    return JSONResponse(body.model_dump(mode="json"))
