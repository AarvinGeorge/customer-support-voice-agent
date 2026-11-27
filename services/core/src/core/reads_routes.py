"""What: the HTTP handlers for GET /v1/me/context, /v1/me/charges and /v1/me/tickets.
Use when: the app is assembled (routes.register_routes plugs them in by route name).
Inputs: the request (scopes `context.read`, `billing.read`, `tickets.read_own`; X-Session-Id), query parameters.
Output: me_context (MeContext), me_charges ({charges, ambiguous}), me_tickets ({tickets}).
Side effects: read-only transactions.
Role: doc 07 s3.1 and s3.2. Order on /me/charges: session header, verified (401), role (403 ROLE_NOT_PERMITTED), and only
then any lookup, so a member learns nothing about whether a charge exists."""
from __future__ import annotations

from datetime import date
from typing import Annotated, Literal

from contracts.core_api import ChargesResponse, TicketsResponse
from fastapi import Query, Request
from fastapi.responses import JSONResponse

from core.auth import get_ctx
from core.db import Database
from core.errors import CoreError
from core.identity import load_claims, require_claims
from core.reads import BILLING_NOTICE, build_context, find_charges, list_tickets

Ending = Annotated[str | None, Query(pattern=r"^[0-9]{4}$")]


def _db(request: Request) -> Database:
    """What: the database handle or a 503. Use when: a handler starts. Inputs: request. Output: Database.
    Side effects: raises CoreError TOOL_ERROR when none is configured. Role: no 500s without a database."""
    db: Database | None = request.app.state.db
    if db is None:
        raise CoreError("TOOL_ERROR", retry_after_s=5, klass="db_unavailable")
    return db


def me_context(request: Request) -> JSONResponse:
    """What: the verified person's MeContext. Use when: the agent refreshes memory. Inputs: request.
    Output: 200 MeContext. Side effects: reads. Raises CoreError SESSION_NOT_VERIFIED (401). Role: doc 07 s3.1."""
    session_id = get_ctx(request).require_session()
    policy, clock = request.app.state.policy, request.app.state.clock
    with _db(request).tx() as conn:
        claims = require_claims(conn, session_id, clock.now(), policy)
        body = build_context(conn, clock, claims, session_id)
    return JSONResponse(body.model_dump(mode="json"))


def me_charges(request: Request, ending: Ending = None, on: date | None = None,
               since_days: Annotated[int, Query(ge=1, le=400)] = 90,
               limit: Annotated[int, Query(ge=1, le=20)] = 10) -> JSONResponse:
    """What: resolve charges of the caller's account. Use when: the list_charges tool. Inputs: request, ending, on,
    since_days (1..400, default 90), limit (1..20, default 10). Output: 200 {charges, ambiguous}. Side effects: reads.
    Raises CoreError SESSION_NOT_VERIFIED (401), ROLE_NOT_PERMITTED (403, before any lookup), NOT_FOUND (404) when a
    filter matches nothing. Role: doc 07 s3.2."""
    session_id = get_ctx(request).require_session()
    policy, clock = request.app.state.policy, request.app.state.clock
    with _db(request).tx() as conn:
        claims = require_claims(conn, session_id, clock.now(), policy)
        if not claims.permissions.billing_read:
            raise CoreError("ROLE_NOT_PERMITTED", BILLING_NOTICE)
        found = find_charges(conn, policy, clock.now(), claims, ending=ending, on=on, since_days=since_days, limit=limit)
    if (ending or on) and not found.charges:
        raise CoreError("NOT_FOUND", "No charge matches that.")
    return JSONResponse(ChargesResponse(charges=found.charges, ambiguous=found.ambiguous).model_dump(mode="json"))


def me_tickets(request: Request, status: Literal["open", "all"] = "open", ending: Ending = None) -> JSONResponse:
    """What: the tickets the caller may see. Use when: the list_tickets tool. Inputs: request, status (open or all),
    ending. Output: 200 {tickets}. Side effects: reads. Role: doc 07 s3.2; an unverified session sees its own session's
    tickets, so there is no 401 here."""
    session_id = get_ctx(request).require_session()
    policy, clock = request.app.state.policy, request.app.state.clock
    with _db(request).tx() as conn:
        claims = load_claims(conn, session_id, clock.now(), policy)
        tickets = list_tickets(conn, clock.now(), claims=claims, session_id=session_id, status=status, ending=ending)
    return JSONResponse(TicketsResponse(tickets=tickets).model_dump(mode="json"))
