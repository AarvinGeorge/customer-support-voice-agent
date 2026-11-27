"""What: the account reads behind GET /v1/me/context, /v1/me/charges and /v1/me/tickets: profile, ticket lists
filtered by role, action history, the open pending action and the charge resolver.
Use when: a read route runs. Inputs: a connection, PolicyConfig, the clock and the verified Claims (or a session id for
unverified ticket reads). Output: money, charge_label, action_label, build_profile, list_tickets, recent_actions,
open_pending, build_context, find_charges.
Side effects: SELECTs only.
Role: doc 07 s3.1 MeContext and s3.2. Who the caller is comes only from Claims (session_claims), never from a request
field. Visibility: owner and billing_admin see the account, a member only their own tickets and actions (no amounts),
an unverified session only its own session's tickets. Profile and preference reads are designed only (doc 09 s1.2);
Profile is built here only because MeContext embeds it."""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Literal
from zoneinfo import ZoneInfo

from contracts.core_api import (ActionHistoryItem, ChargeView, Claims, MeContext, PendingPublic, Profile, TicketRef,
                                TicketView)
from contracts.policy import PolicyConfig
from contracts.redact import mask_email

from core.clock import Clock
from core.tickets import REF_COLUMNS, ticket_ref

HISTORY_DAYS = 90
MAX_OPEN_TICKETS = 10
MAX_ACTIONS = 20
MAX_LIST_TICKETS = 20
BILLING_NOTICE = "Only an account owner or billing admin can look at billing."


def money(cents: int) -> str:
    """What: format cents as dollars ("$96.00", "$11,520.00"). Use when: building history labels. Inputs: cents.
    Output: text. Side effects: none. Role: one money format for customer text."""
    return f"${cents / 100:,.2f}"


def charge_label(invoice_number: str | None, charge_id: str) -> str:
    """What: the speakable charge label: "invoice ending 0412" or "charge ending 0355". Use when: building ChargeView.
    Inputs: invoice number or None, charge id. Output: text. Side effects: none. Role: doc 07 s3.2 ChargeView.label."""
    return f"invoice ending {invoice_number[-4:]}" if invoice_number else f"charge ending {charge_id[-4:]}"


def action_label(action_type: str, target_key: str, amount_cents: int | None) -> str:
    """What: a plain-language label for one committed action. Use when: building ActionHistoryItem.
    Inputs: action type, target key ("refund:CH-00000412"), amount in cents (refunds). Output: text.
    Side effects: none. Role: doc 07 s3.1 ActionHistoryItem.label."""
    if action_type == "refund":
        amount = money(amount_cents) if amount_cents is not None else "a charge"
        return f"Refunded {amount} for charge ending {target_key[-4:]}"
    return {"billing_email": "Changed the billing email", "grace_extension": "Extended the payment grace period",
            "notification_prefs": "Updated notification preferences"}[action_type]


def _iso(value: datetime, now: datetime) -> str:
    """What: format a timestamp in the clock's zone. Use when: building views. Inputs: value, now. Output: ISO text.
    Side effects: none. Role: one time format."""
    return value.astimezone(now.tzinfo).isoformat()


def build_profile(conn: Any, claims: Claims) -> Profile:
    """What: the caller's profile. Use when: MeContext is built. Inputs: connection, Claims. Output: Profile with the
    billing email masked for billing roles only and the caller's own preferences. Side effects: one SELECT.
    Role: doc 07 s3.2 Profile."""
    seats, email, prefs = conn.execute(
        "SELECT a.seats, a.billing_email, u.notification_prefs FROM accounts a, users u "
        "WHERE a.id = %s AND u.id = %s", (claims.account_id, claims.user_id)).fetchone()
    return Profile(account_id=claims.account_id, account_name=claims.account_name, display_name=claims.display_name,
                   role=claims.role, tier=claims.tier, contract_status=claims.contract_status,
                   grace_until=claims.grace_until, seats=seats,
                   billing_email_masked=mask_email(email) if (email and claims.permissions.billing_read) else None,
                   notification_prefs=dict(prefs or {}))


_VIEW_COLS = f"{REF_COLUMNS}, category, summary, outcome, outcome_note, created_at, updated_at"


def list_tickets(conn: Any, now: datetime, *, claims: Claims | None, session_id: uuid.UUID | str,
                 status: Literal["open", "all"] = "open", ending: str | None = None,
                 limit: int = MAX_LIST_TICKETS) -> list[TicketView]:
    """What: the tickets the caller may see, newest first. Use when: GET /v1/me/tickets and the context.
    Inputs: connection, now, Claims (None for an unverified session), session id, status (open means open and
    pending_approval), optional last four digits, limit. Output: list of TicketView. Side effects: one SELECT.
    Role: verified owner and billing_admin see the account, a verified member their own, an unverified session the
    session's own unverified tickets."""
    if claims is None:
        scope, args = "session_id = %s AND verified = false", [session_id]
    elif claims.permissions.billing_read:
        scope, args = "account_id = %s", [claims.account_id]
    else:
        scope, args = "account_id = %s AND user_id = %s", [claims.account_id, claims.user_id]
    where = [scope]
    if status == "open":
        where.append("stage IN ('open','pending_approval')")
    if ending:
        where.append("right(id, 4) = %s")
        args.append(ending)
    rows = conn.execute(f"SELECT {_VIEW_COLS} FROM tickets WHERE {' AND '.join(where)} "
                        "ORDER BY created_at DESC, id DESC LIMIT %s", (*args, limit)).fetchall()
    return [TicketView(**ticket_ref(r[:6]).model_dump(), category=r[6], summary=r[7], outcome=r[8], outcome_note=r[9],
                       created_at=_iso(r[10], now), updated_at=_iso(r[11], now)) for r in rows]


def recent_actions(conn: Any, now: datetime, claims: Claims) -> list[ActionHistoryItem]:
    """What: committed actions of the last 90 days, newest first, at most 20. Use when: MeContext is built.
    Inputs: connection, now, Claims. Output: items; billing roles see the account's with amounts, a member only their
    own without amounts. Side effects: one SELECT. Role: doc 07 s3.1 'role-filtered'."""
    billing = claims.permissions.billing_read
    where, args = ("account_id = %s", [claims.account_id]) if billing else \
        ("account_id = %s AND user_id = %s", [claims.account_id, claims.user_id])
    rows = conn.execute(f"SELECT action_type, target_key, amount_cents, effective_date, committed_by FROM action_history "
                        f"WHERE {where} AND committed_at > %s ORDER BY committed_at DESC, id DESC LIMIT %s",
                        (*args, now - timedelta(days=HISTORY_DAYS), MAX_ACTIONS)).fetchall()
    return [ActionHistoryItem(action_type=r[0], label=action_label(r[0], r[1], r[2] if billing else None),
                              effective_date=r[3].isoformat(), committed_by=r[4], amount_cents=r[2] if billing else None)
            for r in rows]


def open_pending(conn: Any, now: datetime, claims: Claims, session_id: uuid.UUID | str) -> PendingPublic | None:
    """What: the session's open pending action (prepared or awaiting_customer and not past its expiry), or None.
    Use when: MeContext is built. Inputs: connection, now, Claims, session id. Output: PendingPublic or None.
    Side effects: one SELECT. Role: doc 07 s3.1; awaiting_approval is never returned (it shows as a ticket). The card
    text comes from params["public"], which the prepare step writes (a missing key reads as empty text)."""
    row = conn.execute(
        "SELECT id, action_type, pending_kind, state, requires_step_up, expires_at, reason_codes, params "
        "FROM pending_actions WHERE session_id = %s AND account_id = %s AND state IN ('prepared','awaiting_customer') "
        "AND (expires_at IS NULL OR expires_at > %s) ORDER BY created_at DESC LIMIT 1",
        (session_id, claims.account_id, now)).fetchone()
    if row is None:
        return None
    pub = (row[7] or {}).get("public", {})
    return PendingPublic(
        pending_action_id=row[0], action_type=row[1], kind=row[2], state=row[3],
        readback_text=pub.get("readback_text", ""), readback_say=pub.get("readback_say", ""),
        amount_cents=pub.get("amount_cents"), title=pub.get("title", ""), invoice_ref=pub.get("invoice_ref"),
        charge_date=pub.get("charge_date"), reason_code=row[6][0] if row[6] else "", requires_step_up=row[4],
        expires_at=_iso(row[5], now) if row[5] else None, ticket=None)


def build_context(conn: Any, clock: Clock, claims: Claims, session_id: uuid.UUID | str) -> MeContext:
    """What: assemble MeContext for a verified session. Use when: GET /v1/me/context. Inputs: connection, clock, Claims,
    session id. Output: MeContext. Side effects: SELECTs. Role: the memory the agent loads once at verify and at each
    turn start (doc 07 s3.1)."""
    now = clock.now()
    tickets = list_tickets(conn, now, claims=claims, session_id=session_id, status="open", limit=MAX_OPEN_TICKETS)
    return MeContext(claims=claims, profile=build_profile(conn, claims),
                     open_tickets=[TicketRef.model_validate(t.model_dump(include=set(TicketRef.model_fields))) for t in tickets],
                     recent_actions=recent_actions(conn, now, claims),
                     pending_action=open_pending(conn, now, claims, session_id), generated_at=now.isoformat())


@dataclass(frozen=True)
class ChargeMatches:
    """What: the result of find_charges. Use when: the route shapes its response. Inputs: charges, ambiguous.
    Output: frozen value. Side effects: none. Role: lets the route decide 404 versus 200 on an empty list."""

    charges: list[ChargeView]
    ambiguous: bool


def find_charges(conn: Any, policy: PolicyConfig, now: datetime, claims: Claims, *, ending: str | None, on: date | None,
                 since_days: int, limit: int) -> ChargeMatches:
    """What: the caller's account charges matching the filters, newest first. Use when: GET /v1/me/charges (the role
    check has already passed). Inputs: connection, policy, now, Claims, ending (last four digits of the charge id or
    invoice number), on (business date), since_days (ignored when `on` is given), limit. Output: ChargeMatches;
    ambiguous is true when a filter was given and more than one charge matched. Side effects: one SELECT.
    Role: doc 07 s3.2 resolver, also what prepare uses to pick a charge."""
    where: list[str] = ["account_id = %s"]
    args: list[Any] = [claims.account_id]
    if ending:
        where.append("(right(id, 4) = %s OR right(invoice_number, 4) = %s)")
        args += [ending, ending]
    if on is not None:
        where.append("(charged_at AT TIME ZONE %s)::date = %s")
        args += [policy.timezone, on]
    else:
        where.append("charged_at >= %s")
        args.append(now - timedelta(days=since_days))
    rows = conn.execute(
        "SELECT id, invoice_number, charged_at, amount_cents, currency, description, status, refunded_at, "
        f"count(*) OVER () FROM charges WHERE {' AND '.join(where)} ORDER BY charged_at DESC, id DESC LIMIT %s",
        (*args, limit)).fetchall()
    tz = ZoneInfo(policy.timezone)
    views = [ChargeView(charge_id=r[0], invoice_number=r[1], label=charge_label(r[1], r[0]), charged_at=_iso(r[2], now),
                        charge_date=r[2].astimezone(tz).date().isoformat(), amount_cents=r[3], currency=r[4],
                        description=r[5], status=r[6], refunded_at=_iso(r[7], now) if r[7] else None) for r in rows]
    total = rows[0][8] if rows else 0
    return ChargeMatches(views, ambiguous=bool(ending or on) and total > 1)
