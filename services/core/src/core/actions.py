"""What: the refund action engine: prepare (expire, supersede, evaluate, insert), commit, submit, cancel and the status
read, plus the pure helpers they share (the precondition gate, read-time expiry, card fields, outcome wording).
Use when: the /v1/actions routes run (core.actions_routes), or a test drives the engine on its own connection.
Inputs: a connection inside the caller's transaction, PolicyConfig, the injected clock, the verified Claims or the
session id, the turn id and the request body.
Output: PrepareResult, CommitResult, SubmitResult, CancelResult, PendingStatus; commit, submit and cancel return a
CoreError (instead of raising it) when the error must keep a state change (expired, rules_changed), so the route can
commit the transaction first and raise after.
Side effects: SELECT ... FOR UPDATE and UPDATE on pending_actions, UPDATE on charges, INSERT on pending_actions,
action_history, tickets (through core.tickets), ticket_events and audit_events; transaction advisory locks.
Role: doc 07 s3.3 and s7.1, 06 R-3. Rules live in core.rules.refund and are run on live rows at prepare and again at
commit or submit. Core enforces "later turn" (X-Turn-Id differs from the prepare turn); the agent enforces "explicit
yes". Tickets: core opens the past-due follow-up (inside prepare) and the Pending approval ticket (inside submit) and
nothing else; cancel never opens one. Step-up (A2) is designed only: the gate checks the stored flag, nothing sets it."""
from __future__ import annotations

import secrets
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Literal
from zoneinfo import ZoneInfo

import psycopg
from contracts.core_api import (CancelResult, ChargeRefEnding, ChargeRefId, Claims, CommitResult, PendingPublic,
                                PendingStatus, PolicyRef, PrepareResult, RefundPrepareRequest, SubmitResult, TicketRef)
from contracts.policy import PolicyConfig, Readback
from contracts.reason_codes import ReasonCode
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from core.audit import AuditEvent, write_audit
from core.clock import Clock
from core.errors import CoreError
from core.identity import load_claims
from core.reads import charge_label, find_charges, money
from core.rules.refund import ChargeFacts, Outcome, PriorRefund, RefundEvaluation, evaluate
from core.tickets import REF_COLUMNS, insert_ticket, ticket_ref

OPEN_STATES = ("prepared", "awaiting_customer")
LIVE_STATES = (*OPEN_STATES, "awaiting_approval")
RESOLVE_SINCE_DAYS = 400          # an explicit reference may name any charge on record; the rules judge its age
NULL_REF_SINCE_DAYS = 90          # a null reference lists the recent charges as candidates (doc 07 s3.2 default)
NULL_REF_LIMIT = 10
RULES_CHANGED_MESSAGE = "The rules for this request have changed since I read it back, so nothing was done."
_ID_ATTEMPTS = 5
_PENDING_COLS = ("id, session_id, account_id, user_id, action_type, pending_kind, target_key, params, decision, "
                 "reason_codes, policy_version, state, prepare_turn_id, requires_step_up, step_up_at, ticket_id, result, "
                 "expires_at")


# ---------------------------------------------------------------- pure helpers

def section_title(slug: str) -> str:
    """What: a readable section title from its slug ("pro-plan-refunds" to "Pro plan refunds").
    Use when: building PolicyRef.title. Inputs: slug. Output: text. Side effects: none.
    Role: doc 07 s13.1 says core adds the title; policy.yaml holds slugs only, and the slugs are the article headings."""
    words = slug.replace("-", " ").strip()
    return words[:1].upper() + words[1:]


def policy_refs(policy: PolicyConfig, codes: Sequence[ReasonCode | str]) -> list[PolicyRef]:
    """What: the policy refs of a list of reason codes, deduplicated in code order, with titles.
    Use when: PrepareResult and RULES_CHANGED details. Inputs: policy, codes. Output: list of PolicyRef.
    Side effects: none. Role: one place that turns codes into citations."""
    refs: dict[tuple[str, str], PolicyRef] = {}
    for code in codes:
        cfg = policy.reason_codes[ReasonCode(code)].policy_ref
        refs.setdefault((cfg.article, cfg.section),
                        PolicyRef(article=cfg.article, section=cfg.section, title=section_title(cfg.section)))
    return list(refs.values())


def card_public(*, amount_cents: int, charge_label: str, charge_date: date, readback: Readback,
                reason_code: str) -> dict[str, Any]:
    """What: the confirmation card fields stored in params.public. Use when: prepare inserts a pending row.
    Inputs: amount, the speakable charge label, the charge's business date, the read-back, the primary reason code.
    Output: dict with readback_text, readback_say, amount_cents, title, invoice_ref, charge_date, reason_code.
    Side effects: none. Role: doc 07 s3.3 PendingPublic (r4); core.reads.open_pending reads these keys."""
    return {"readback_text": readback.chat, "readback_say": readback.voice, "amount_cents": amount_cents,
            "title": f"Refund {money(amount_cents)} for the charge on {charge_date.day} {charge_date:%B}",
            "invoice_ref": charge_label, "charge_date": charge_date.isoformat(), "reason_code": reason_code}


def new_pending_id(rng: Callable[[int], int] | None = None) -> str:
    """What: mint a pending action id "PA-" plus 8 upper-case hex digits. Use when: prepare inserts a row.
    Inputs: optional rng(n) returning an int below n (tests). Output: id. Side effects: reads os randomness.
    Role: pending_actions.id CHECK (^PA-[0-9A-F]{8}$)."""
    draw = rng or secrets.randbelow
    return f"PA-{draw(1 << 32):08X}"


def is_expired(state: str, expires_at: datetime | None, now: datetime) -> bool:
    """What: an open row whose expiry has passed. Use when: reading or acting on a row. Inputs: state, expiry, now.
    Output: bool. Side effects: none. Role: doc 07 s7.1 'now >= expires_at'; awaiting_approval never expires."""
    return state in OPEN_STATES and expires_at is not None and now >= expires_at


def read_state(state: str, expires_at: datetime | None, now: datetime) -> str:
    """What: the state as a reader must see it (an expired open row reads expired). Use when: the status read.
    Inputs: stored state, expiry, now. Output: state. Side effects: none. Role: doc 07 s7.1 read-time expiry."""
    return "expired" if is_expired(state, expires_at, now) else state


def gate(*, state: str, expected: str, expires_at: datetime | None, now: datetime, prepare_turn_id: str,
         turn_id: str, requires_step_up: bool, step_up_at: datetime | None) -> str | None:
    """What: the commit and submit preconditions, in order: expiry, state, later turn, step-up.
    Use when: commit (expected prepared) and submit (expected awaiting_customer) hold the row lock.
    Inputs: stored state, the expected state, expiry, now, the prepare turn id, this request's turn id, the step-up
    flag and time. Output: the error code (PENDING_EXPIRED, PENDING_NOT_ACTIVE, CONFIRMATION_REQUIRED,
    STEP_UP_REQUIRED) or None when the action may proceed; the turn compare ignores case. Side effects: none.
    Role: doc 07 s3.3; pure so the table is unit-tested. Step-up is designed only (A2): no refund code requires it."""
    if state == "expired" or is_expired(state, expires_at, now):
        return "PENDING_EXPIRED"
    if state != expected:
        return "PENDING_NOT_ACTIVE"
    if turn_id.lower() == prepare_turn_id.lower():  # turn ids are uuid text: compare case-insensitively (review M1)
        return "CONFIRMATION_REQUIRED"
    if requires_step_up and step_up_at is None:
        return "STEP_UP_REQUIRED"
    return None


def rules_still_hold(evaluation: RefundEvaluation, expected: str, stored: Mapping[str, Any]) -> bool:
    """What: the re-check result: same wire decision and the same frozen params (charge id and amount).
    Use when: commit (expected allowed) and submit (expected needs_approval). Inputs: the new evaluation, the expected
    decision, the stored params. Output: bool. Side effects: none.
    Role: doc 07 s3.3 'rules re-evaluated on locked live rows and params unchanged'."""
    if evaluation.policy_decision != expected:
        return False
    return all(stored.get(k) == v for k, v in evaluation.params.items())


def commit_summary(amount_cents: int, label: str) -> tuple[str, str]:
    """What: the chat and voice text of a committed refund. Use when: commit. Inputs: amount, charge label.
    Output: (summary, summary_say). Side effects: none. Role: CommitResult text; timing wording follows the refund
    article ("goes back to the original payment method", bank-dependent)."""
    amount = money(amount_cents)
    return (f"Refunded {amount} for {label}. It goes back to the original payment method; how long it takes to appear "
            "depends on your bank.",
            f"Done. I've refunded {amount} for {label}. It goes back to your original payment method.")


def submit_summary(short_id: str) -> tuple[str, str]:
    """What: the chat and voice text after a request is sent for approval. Use when: submit. Inputs: the ticket's last
    four digits. Output: (summary, summary_say). Side effects: none. Role: SubmitResult text."""
    return (f"I've sent this to our team for approval: ticket ending {short_id}. You'll see the outcome when you ask "
            "about your ticket.",
            f"I've sent this to our team for approval, ticket ending {short_id}. You'll hear the outcome when you ask "
            "about your ticket.")


# ---------------------------------------------------------------- database helpers

def _lock(conn: Any, name: str) -> None:
    """What: take a transaction advisory lock. Use when: serialising prepares per session or per target.
    Inputs: connection, lock name. Output: none. Side effects: lock until the transaction ends. Role: no races."""
    conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (name,))


def _iso(value: datetime | None, now: datetime) -> str | None:
    """What: format a timestamp in the clock's zone. Use when: building responses. Inputs: value, now.
    Output: ISO text or None. Side effects: none. Role: one time format."""
    return value.astimezone(now.tzinfo).isoformat() if value else None


@dataclass(frozen=True)
class Live:
    """What: the live facts a refund evaluation used. Use when: prepare, commit and submit.
    Inputs: tier, evaluation, charge facts and its business date. Output: frozen value. Side effects: none.
    Role: lets the caller reuse the tier and charge label it already read."""

    tier: str
    evaluation: RefundEvaluation
    charge: ChargeFacts
    charge_date: date


def _evaluate_live(conn: Any, *, policy: PolicyConfig, now: datetime, account_id: str, user_id: str,
                   charge_id: str) -> Live:
    """What: run the refund rules on locked live rows (account FOR SHARE, charge FOR UPDATE, completed refunds).
    Use when: prepare after the charge is resolved, and the commit or submit re-check. Inputs: connection, policy, now,
    account, user, charge. Output: Live. Side effects: row locks until the transaction ends; raises CoreError NOT_FOUND
    when the account, the user (in that account) or the charge is gone.
    Role: the same function and the same reads at prepare and at commit, so they cannot disagree."""
    account = conn.execute("SELECT tier, contract_status FROM accounts WHERE id = %s FOR SHARE", (account_id,)).fetchone()
    user = conn.execute("SELECT role FROM users WHERE id = %s AND account_id = %s", (user_id, account_id)).fetchone()
    charge = conn.execute(
        "SELECT id, invoice_number, charged_at, amount_cents, status FROM charges WHERE id = %s AND account_id = %s "
        "FOR UPDATE", (charge_id, account_id)).fetchone()
    if account is None or user is None or charge is None:  # review M5: an honest 404, never a 500 on a missing row
        raise CoreError("NOT_FOUND", "That charge or account is no longer on record.")
    (tier, status), role = account, user[0]
    cid, invoice, charged_at, cents, cstatus = charge
    prior = tuple(PriorRefund(charge_id=r[0].split(":", 1)[1], effective_date=r[1]) for r in conn.execute(
        "SELECT target_key, effective_date FROM action_history WHERE account_id = %s AND action_type = 'refund'",
        (account_id,)).fetchall())
    facts = ChargeFacts(charge_id=cid, amount_cents=cents, charged_at=charged_at, status=cstatus,
                        label=charge_label(invoice, cid))
    evaluation = evaluate(tier=tier, contract_status=status, role=role, charge=facts, prior_refunds=prior,
                          policy=policy, now=now)
    return Live(tier, evaluation, facts, charged_at.astimezone(ZoneInfo(policy.timezone)).date())


def _resolve_charge(conn: Any, *, policy: PolicyConfig, now: datetime, claims: Claims,
                    ref: ChargeRefId | ChargeRefEnding | None) -> str:
    """What: turn the charge reference into one charge id of the caller's account.
    Use when: prepare, after the account-level rules passed (so a member never reaches a lookup).
    Inputs: connection, policy, now, Claims, the reference. Output: charge id.
    Side effects: SELECTs. Raises CoreError NOT_FOUND (no match, or another account's charge), AMBIGUOUS_REFERENCE
    {candidates} (more than one match, or a null reference with charges on record), VALIDATION_ERROR (bad date).
    Role: doc 07 s3.3 prepare errors; the resolver is the same one GET /v1/me/charges uses."""
    if isinstance(ref, ChargeRefId):
        row = conn.execute("SELECT id FROM charges WHERE id = %s AND account_id = %s",
                           (ref.id, claims.account_id)).fetchone()
        if row is None:
            raise CoreError("NOT_FOUND", "No charge matches that.")
        return row[0]
    if ref is None:
        found = find_charges(conn, policy, now, claims, ending=None, on=None, since_days=NULL_REF_SINCE_DAYS,
                             limit=NULL_REF_LIMIT)
    else:
        try:
            on = date.fromisoformat(ref.on) if ref.on else None
        except ValueError:
            raise CoreError("VALIDATION_ERROR", details={"errors": [{"loc": ["body", "charge_ref", "on"],
                                                                     "type": "date", "msg": "must be YYYY-MM-DD"}]}) from None
        found = find_charges(conn, policy, now, claims, ending=ref.ending, on=on, since_days=RESOLVE_SINCE_DAYS,
                             limit=20)
    if not found.charges:
        raise CoreError("NOT_FOUND", "No charge matches that.")
    if ref is None or len(found.charges) > 1 or found.ambiguous:
        raise CoreError("AMBIGUOUS_REFERENCE", "More than one charge could match. Which one do you mean?",
                        details={"candidates": [c.model_dump(mode="json") for c in found.charges]})
    return found.charges[0].charge_id


def _followup_ticket(conn: Any, *, policy: PolicyConfig, clock: Clock, claims: Claims, session_id: str, queue: str,
                     reason: str, trace_id: str | None, mode: str | None, key: str | None) -> TicketRef:
    """What: the past-due billing follow-up ticket: reuse the account's open billing ticket, else open one.
    Use when: prepare declines with PAST_DUE. Inputs: connection, policy, clock, Claims, session, queue from policy,
    refund reason, trace id, mode, idempotency key. Output: TicketRef.
    Side effects: the account ticket lock, then a merged event and an audit row, or a new ticket (core.tickets).
    Role: doc 07 s3.4 owners row 2 (core, inside prepare, never the agent); merging keeps it to exactly one open
    follow-up per account, and core's own ticket is not counted against the daily limit."""
    _lock(conn, f"tickets:{claims.account_id}")
    summary = f"Refund requested ({reason}) while the account is past due. Billing to follow up with the customer."
    row = conn.execute(f"SELECT {REF_COLUMNS} FROM tickets WHERE account_id = %s AND category = 'billing' "
                       "AND stage = 'open' ORDER BY created_at DESC, id DESC LIMIT 1 FOR UPDATE",
                       (claims.account_id,)).fetchone()
    if row is not None:
        now = clock.now()
        conn.execute("INSERT INTO ticket_events (ticket_id, kind, actor, actor_id, body, at) "
                     "VALUES (%s,'merged','system',NULL,%s,%s)", (row[0], summary, now))
        conn.execute("UPDATE tickets SET updated_at = %s WHERE id = %s", (now, row[0]))
        write_audit(conn, AuditEvent(event="ticket_merge", actor="system", trace_id=trace_id,
                                     session_id=uuid.UUID(session_id), mode=mode, account_id=claims.account_id,
                                     ticket_id=row[0], policy_version=policy.policy_version,
                                     args={"category": "billing", "origin": "core"}, idempotency_key=key), clock=clock)
        return ticket_ref(row)
    return insert_ticket(conn, policy=policy, clock=clock, session_id=session_id, account_id=claims.account_id,
                         user_id=claims.user_id, tier=claims.tier, stage="open", queue=queue,  # pyright: ignore[reportArgumentType]
                         category="billing", summary=summary, origin="core", actor="system", actor_id=None,
                         trace_id=trace_id, mode=mode, idempotency_key=key)


def _audit(conn: Any, *, policy: PolicyConfig, clock: Clock, event: str, session_id: str, account_id: str,
           user_id: str | None, pending_id: str | None, decision: str | None, codes: Sequence[str] | None,
           args: dict[str, Any], status: int, trace_id: str | None, mode: str | None, ticket_id: str | None = None,
           key: str | None = None) -> None:
    """What: one action audit row. Use when: every prepare, commit, submit, cancel and state flip.
    Inputs: the row's facts. Output: none. Side effects: one INSERT. Role: doc 07 s11 audit, through write_audit."""
    write_audit(conn, AuditEvent(
        event=event, actor="agent", trace_id=trace_id, session_id=uuid.UUID(session_id), mode=mode, actor_id=user_id,
        account_id=account_id, action_type="refund", pending_action_id=pending_id, ticket_id=ticket_id,
        decision=decision, reason_codes=list(codes) if codes is not None else None,
        policy_version=policy.policy_version, args=args, result_status=status, idempotency_key=key), clock=clock)


# ---------------------------------------------------------------- prepare

def prepare_refund(conn: Any, *, policy: PolicyConfig, clock: Clock, claims: Claims, session_id: str, turn_id: str,
                   body: RefundPrepareRequest, trace_id: str | None = None, mode: str | None = None,
                   idempotency_key: str | None = None) -> PrepareResult:
    """What: prepare a refund in one transaction: account rules, charge resolution, expire stale rows, supersede the
    session's open row, evaluate on live rows, insert the pending row (or decline, opening the past-due ticket).
    Use when: POST /v1/actions/refund/prepare (inside run_idempotent). Inputs: connection, policy, clock, verified
    Claims, session id, turn id (stored as prepare_turn_id), body, trace id, mode, idempotency key.
    Output: PrepareResult (changed false; `card` is the r4 PendingPublic of the new row, the same fields params.public
    stores for MeContext). Side effects: locks, UPDATEs, one INSERT, maybe a ticket, an audit row.
    Raises CoreError NOT_FOUND, AMBIGUOUS_REFERENCE, ACTION_IN_PROGRESS, VALIDATION_ERROR (nothing persists).
    Role: doc 07 s3.3 prepare and s7.1 rows 1, 2, 6, 7. A member, Free, trialing or past_due account is declined
    before any charge lookup; the supersede happens even when the new prepare is declined."""
    now = clock.now()
    _lock(conn, f"pa:session:{session_id}")
    account_check = evaluate(tier=claims.tier, contract_status=claims.contract_status, role=claims.role, charge=None,
                             prior_refunds=(), policy=policy, now=now)
    account_decline = account_check if account_check.reason_codes[0] is not ReasonCode.CHARGE_NOT_REFUNDABLE else None
    charge_id = target = None
    if account_decline is None:
        charge_id = _resolve_charge(conn, policy=policy, now=now, claims=claims, ref=body.charge_ref)
        target = f"refund:{charge_id}"
        _lock(conn, f"pa:target:{target}")
    conn.execute("UPDATE pending_actions SET state = 'expired', decided_at = %s WHERE state IN ('prepared','awaiting_customer') "
                 "AND expires_at <= %s AND (session_id = %s OR target_key = %s)", (now, now, session_id, target))
    old = conn.execute("UPDATE pending_actions SET state = 'superseded', decided_at = %s WHERE session_id = %s "
                       "AND state IN ('prepared','awaiting_customer') RETURNING id", (now, session_id)).fetchone()
    details: dict[str, Any] = {"superseded_pending_action_id": old[0]} if old else {}
    live = None
    if account_decline is None:
        assert charge_id is not None
        live = _evaluate_live(conn, policy=policy, now=now, account_id=claims.account_id, user_id=claims.user_id,
                              charge_id=charge_id)
        details |= {"charge_id": charge_id, "amount_cents": live.charge.amount_cents, "charge_label": live.charge.label,
                    "charge_date": live.charge_date.isoformat()}
    ev = account_decline or live.evaluation  # pyright: ignore[reportOptionalMemberAccess]
    codes = [str(c) for c in ev.reason_codes]
    common = {"decision": ev.policy_decision, "reason_codes": codes, "policy_refs": policy_refs(policy, codes),
              "policy_version": policy.policy_version, "changed": False}
    audit_args = {"charge_id": charge_id, "reason": body.reason, "superseded": old[0] if old else None}
    if ev.outcome is Outcome.DECLINED:
        ticket = None
        if ev.followup_ticket_queue:
            ticket = _followup_ticket(conn, policy=policy, clock=clock, claims=claims, session_id=session_id,
                                      queue=ev.followup_ticket_queue, reason=body.reason, trace_id=trace_id, mode=mode,
                                      key=idempotency_key)
        _audit(conn, policy=policy, clock=clock, event="action_prepare", session_id=session_id,
               account_id=claims.account_id, user_id=claims.user_id, pending_id=None, decision="declined", codes=codes,
               args=audit_args, status=200, trace_id=trace_id, mode=mode,
               ticket_id=ticket.ticket_id if ticket else None, key=idempotency_key)
        return PrepareResult(**common, message=ev.message, requires_step_up=False, details=details, ticket=ticket)

    assert live is not None and target is not None and ev.readback is not None
    if conn.execute("SELECT 1 FROM pending_actions WHERE target_key = %s AND state IN "
                    "('prepared','awaiting_customer','awaiting_approval')", (target,)).fetchone():
        raise CoreError("ACTION_IN_PROGRESS", "A request for that charge is already in progress.")
    allowed = ev.policy_decision == "allowed"
    state: Literal["prepared", "awaiting_customer"] = "prepared" if allowed else "awaiting_customer"
    kind: Literal["commit", "submit_for_approval"] = "commit" if allowed else "submit_for_approval"
    step_up = any(policy.reason_codes[c].requires_step_up for c in ev.reason_codes)
    expires = now + timedelta(seconds=policy.actions.refund.pending_ttl_seconds)
    params = {**ev.params, "reason": body.reason, "note": body.note,
              "public": card_public(amount_cents=live.charge.amount_cents, charge_label=live.charge.label,
                                    charge_date=live.charge_date, readback=ev.readback, reason_code=codes[0])}
    pid = _insert_pending(conn, session_id=session_id, claims=claims, kind=kind, target=target, params=params,
                          decision=ev.policy_decision, codes=codes, policy=policy, state=state, turn_id=turn_id,
                          step_up=step_up, now=now, expires=expires)
    _audit(conn, policy=policy, clock=clock, event="action_prepare", session_id=session_id, account_id=claims.account_id,
           user_id=claims.user_id, pending_id=pid, decision=ev.policy_decision, codes=codes, args=audit_args,
           status=200, trace_id=trace_id, mode=mode, key=idempotency_key)
    pub = params["public"]
    card = PendingPublic(pending_action_id=pid, action_type="refund", kind=kind, state=state,
                         readback_text=pub["readback_text"], readback_say=pub["readback_say"],
                         amount_cents=pub["amount_cents"], title=pub["title"], invoice_ref=pub["invoice_ref"],
                         charge_date=pub["charge_date"], reason_code=pub["reason_code"], requires_step_up=step_up,
                         expires_at=_iso(expires, now), ticket=None)
    return PrepareResult(**common, message=None, pending_action_id=pid, pending_kind=kind, state=state,
                         expires_at=_iso(expires, now), requires_step_up=step_up,
                         readback=ev.readback.model_dump(), details=details, ticket=None,  # pyright: ignore[reportArgumentType]
                         card=card)


def _insert_pending(conn: Any, *, session_id: str, claims: Claims, kind: str, target: str, params: dict[str, Any],
                    decision: str, codes: list[str], policy: PolicyConfig, state: str, turn_id: str, step_up: bool,
                    now: datetime, expires: datetime) -> str:
    """What: insert the pending row with a fresh id, retrying an id clash. Use when: prepare allows or needs approval.
    Inputs: the row's fields. Output: the new id. Side effects: one INSERT. Raises CoreError ACTION_IN_PROGRESS if a
    unique index still fires (a race the advisory locks should already prevent), INTERNAL if no id is free.
    Role: the only writer of new pending_actions rows."""
    for _ in range(_ID_ATTEMPTS):
        pid = new_pending_id()
        try:
            row = conn.execute(
                "INSERT INTO pending_actions (id, session_id, account_id, user_id, action_type, pending_kind, target_key, "
                "params, decision, reason_codes, policy_version, state, prepare_turn_id, requires_step_up, created_at, "
                "expires_at) VALUES (%s,%s,%s,%s,'refund',%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
                "ON CONFLICT (id) DO NOTHING RETURNING id",
                (pid, session_id, claims.account_id, claims.user_id, kind, target, Jsonb(params), decision, codes,
                 policy.policy_version, state, turn_id, step_up, now, expires)).fetchone()
        except psycopg.errors.UniqueViolation:
            raise CoreError("ACTION_IN_PROGRESS", "A request for that charge is already in progress.") from None
        if row:
            return pid
    raise CoreError("INTERNAL", "Could not allocate a pending action id.")


# ---------------------------------------------------------------- commit, submit, cancel, status

def _locked_row(conn: Any, pending_id: str, session_id: str, *, lock: bool = True) -> dict[str, Any]:
    """What: read one pending row (FOR UPDATE unless lock is false) and check it belongs to the session.
    Use when: commit, submit, cancel (locked) and status (not locked). Inputs: connection, id, session, lock flag.
    Output: the row as a dict. Side effects: row lock until the transaction ends.
    Raises CoreError NOT_FOUND for an unknown id or another session's row (no existence leak).
    Role: the person comes only from the session; one read shape for every pending path."""
    sql = f"SELECT {_PENDING_COLS} FROM pending_actions WHERE id = %s" + (" FOR UPDATE" if lock else "")
    with conn.cursor(row_factory=dict_row) as cur:
        row = cur.execute(sql, (pending_id,)).fetchone()
    if row is None or str(row["session_id"]) != session_id:
        raise CoreError("NOT_FOUND", "That request does not exist.")
    return row


def _require_verified(conn: Any, row: dict[str, Any], *, policy: PolicyConfig, now: datetime) -> None:
    """What: the session's verification must be current for the row's person: unexpired claims for the same user and
    no session lock. Use when: commit and submit, after the replay check and before any other check.
    Inputs: connection, the locked row, policy, now. Output: none. Side effects: two SELECTs; raises CoreError
    SESSION_NOT_VERIFIED (401). The route rolls back (the row keeps its state) and audits the refusal.
    Role: D-055 (review PR #37 I1): authority must hold at the irreversible step, not only at prepare."""
    sid = str(row["session_id"])
    claims = load_claims(conn, sid, now, policy)
    locked = conn.execute("SELECT verify_locked FROM session_claims WHERE session_id = %s", (sid,)).fetchone()
    if claims is None or claims.user_id != row["user_id"] or (locked is not None and locked[0]):
        raise CoreError("SESSION_NOT_VERIFIED", "This chat needs to be verified again before I can do that.")


def _set_state(conn: Any, pending_id: str, state: str, now: datetime) -> None:
    """What: move a row to a terminal state. Use when: expired, rules_changed or cancelled. Inputs: connection, id,
    state, now. Output: none. Side effects: one UPDATE. Role: doc 07 s7.1 transitions."""
    conn.execute("UPDATE pending_actions SET state = %s, decided_at = %s WHERE id = %s", (state, now, pending_id))


def _settle(conn: Any, row: dict[str, Any], *, expected: str, turn_id: str, policy: PolicyConfig, clock: Clock,
            trace_id: str | None, mode: str | None, event: str) -> CoreError | Live:
    """What: run the gate and the re-check for commit or submit; persist expired or rules_changed when they fire.
    Use when: commit and submit, holding the row lock. Inputs: connection, the locked row, the expected state, turn id,
    policy, clock, trace id, mode, audit event name. Output: the Live facts when the action may proceed, or a CoreError
    to raise after the transaction commits (PENDING_EXPIRED, RULES_CHANGED). Side effects: maybe an UPDATE and an audit
    row. Raises CoreError PENDING_NOT_ACTIVE, CONFIRMATION_REQUIRED, STEP_UP_REQUIRED directly (nothing to keep).
    Role: doc 07 s3.3 'same checks' for commit and submit."""
    now = clock.now()
    sid, pid = str(row["session_id"]), row["id"]
    code = gate(state=row["state"], expected=expected, expires_at=row["expires_at"], now=now,
                prepare_turn_id=row["prepare_turn_id"], turn_id=turn_id, requires_step_up=row["requires_step_up"],
                step_up_at=row["step_up_at"])
    if code == "PENDING_EXPIRED":
        if row["state"] in OPEN_STATES:
            _set_state(conn, pid, "expired", now)
            _audit(conn, policy=policy, clock=clock, event="action_expired", session_id=sid,
                   account_id=row["account_id"], user_id=row["user_id"], pending_id=pid, decision=None, codes=None,
                   args={"via": event}, status=410, trace_id=trace_id, mode=mode)
        return CoreError("PENDING_EXPIRED", "That request has expired. I can prepare it again.")
    if code:
        raise CoreError(code, details={"state": read_state(row["state"], row["expires_at"], now)})
    live = _evaluate_live(conn, policy=policy, now=now, account_id=row["account_id"], user_id=row["user_id"],
                          charge_id=row["params"]["charge_id"])
    want = "allowed" if expected == "prepared" else "needs_approval"
    if not rules_still_hold(live.evaluation, want, row["params"]):
        _set_state(conn, pid, "rules_changed", now)
        codes = [str(c) for c in live.evaluation.reason_codes]
        message = live.evaluation.message or RULES_CHANGED_MESSAGE
        _audit(conn, policy=policy, clock=clock, event=event, session_id=sid, account_id=row["account_id"],
               user_id=row["user_id"], pending_id=pid, decision="error", codes=codes,
               args={"result": "rules_changed"}, status=409, trace_id=trace_id, mode=mode)
        return CoreError("RULES_CHANGED", message, details={
            "reason_codes": codes, "policy_refs": [r.model_dump() for r in policy_refs(policy, codes)],
            "message": message})
    return live


def commit_pending(conn: Any, *, policy: PolicyConfig, clock: Clock, pending_id: str, session_id: str, turn_id: str,
                   trace_id: str | None = None, mode: str | None = None) -> CommitResult | CoreError:
    """What: commit a prepared refund: lock the row, replay a stored result, check expiry, state, later turn and
    step-up, re-run the rules on live rows, then refund the charge and write one action_history row.
    Use when: POST /v1/actions/pending/{id}/commit (CORE_TOKEN_COMMIT only). Inputs: connection, policy, clock, id,
    session, this turn's id, trace id, mode. Output: CommitResult (replayed true on a repeat) or a CoreError to raise
    after the transaction commits. Side effects: FOR UPDATE lock, UPDATE charges and pending_actions, INSERT
    action_history and audit. Role: doc 07 s3.3 commit, s7.1 rows 3, 7, 8; a second concurrent commit waits on the
    lock, then sees committed and replays, so there is one history row (ah_refund_once also forbids a second).
    A repeat of a committed row replays even if verification has since lapsed (it changes nothing and tells the agent
    the truth); any new commit needs current verification (D-055, raises SESSION_NOT_VERIFIED)."""
    row = _locked_row(conn, pending_id, session_id)
    if row["state"] == "committed" and row["pending_kind"] == "commit" and row["result"]:
        return CommitResult.model_validate({**row["result"], "replayed": True})
    _require_verified(conn, row, policy=policy, now=clock.now())
    settled = _settle(conn, row, expected="prepared", turn_id=turn_id, policy=policy, clock=clock, trace_id=trace_id,
                      mode=mode, event="action_commit")
    if isinstance(settled, CoreError):
        return settled
    now = clock.now()
    charge = settled.charge
    conn.execute("UPDATE charges SET status = 'refunded', refunded_at = %s WHERE id = %s AND status = 'paid'",
                 (now, charge.charge_id))
    conn.execute(
        "INSERT INTO action_history (pending_action_id, account_id, user_id, action_type, target_key, amount_cents, "
        "before_value, after_value, effective_date, committed_at, committed_by) "
        "VALUES (%s,%s,%s,'refund',%s,%s,%s,%s,%s,%s,'customer')",
        (pending_id, row["account_id"], row["user_id"], row["target_key"], charge.amount_cents,
         Jsonb({"status": "paid"}), Jsonb({"status": "refunded"}), clock.today(), now))
    summary, say = commit_summary(charge.amount_cents, charge.label)
    result = CommitResult(status="committed", pending_action_id=pending_id, replayed=False, changed=True,
                          summary=summary, summary_say=say,
                          details={"charge_id": charge.charge_id, "amount_cents": charge.amount_cents})
    conn.execute("UPDATE pending_actions SET state = 'committed', decided_at = %s, result = %s WHERE id = %s",
                 (now, Jsonb(result.model_dump(mode="json")), pending_id))
    _audit(conn, policy=policy, clock=clock, event="action_commit", session_id=session_id, account_id=row["account_id"],
           user_id=row["user_id"], pending_id=pending_id, decision="allowed", codes=row["reason_codes"],
           args={"charge_id": charge.charge_id, "amount_cents": charge.amount_cents}, status=200, trace_id=trace_id,
           mode=mode, key=pending_id)
    return result


def submit_pending(conn: Any, *, policy: PolicyConfig, clock: Clock, pending_id: str, session_id: str, turn_id: str,
                   trace_id: str | None = None, mode: str | None = None) -> SubmitResult | CoreError:
    """What: send an awaiting_customer refund for approval on the customer's yes: same checks as commit, the rules must
    still give needs_approval with the same params, then open the Pending approval ticket and set awaiting_approval.
    Use when: POST /v1/actions/pending/{id}/submit (CORE_TOKEN_COMMIT only). Inputs: as commit_pending.
    Output: SubmitResult (replayed true on a repeat) or a CoreError to raise after the transaction commits.
    Side effects: FOR UPDATE lock, one ticket (core.tickets.insert_ticket), UPDATE pending_actions, audit.
    Role: doc 07 s3.3 submit, s7.1 row 4, s3.4 owners row 3, R-3 (no ticket before the yes). tickets.pending_action_id
    is UNIQUE, so one action can never own two approval tickets. Needs current verification (D-055). The ticket and
    the audit row carry the reason codes of the fresh re-check (review M4), not the stored ones."""
    row = _locked_row(conn, pending_id, session_id)
    if row["state"] == "awaiting_approval" and row["pending_kind"] == "submit_for_approval" and row["result"]:
        return SubmitResult.model_validate({**row["result"], "replayed": True})
    _require_verified(conn, row, policy=policy, now=clock.now())
    settled = _settle(conn, row, expected="awaiting_customer", turn_id=turn_id, policy=policy, clock=clock,
                      trace_id=trace_id, mode=mode, event="action_submit")
    if isinstance(settled, CoreError):
        return settled
    charge, params = settled.charge, row["params"]
    codes = [str(c) for c in settled.evaluation.reason_codes]
    summary_text = (f"Refund approval: {money(charge.amount_cents)} for {charge.label} (reason: {params.get('reason')}). "
                    f"Rule: {', '.join(codes)}.")
    ticket = insert_ticket(conn, policy=policy, clock=clock, session_id=session_id, account_id=row["account_id"],
                           user_id=row["user_id"], tier=settled.tier, stage="pending_approval",  # pyright: ignore[reportArgumentType]
                           queue="approvals", category="refund", summary=summary_text, origin="core", actor="customer",
                           actor_id=row["user_id"], pending_action_id=pending_id, trace_id=trace_id, mode=mode,
                           idempotency_key=f"{pending_id}:submit")
    summary, say = submit_summary(ticket.short_id)
    result = SubmitResult(status="awaiting_approval", pending_action_id=pending_id, replayed=False, changed=True,
                          ticket=ticket, summary=summary, summary_say=say)
    conn.execute("UPDATE pending_actions SET state = 'awaiting_approval', ticket_id = %s, submit_turn_id = %s, "
                 "expires_at = NULL, result = %s WHERE id = %s",
                 (ticket.ticket_id, turn_id, Jsonb(result.model_dump(mode="json")), pending_id))
    _audit(conn, policy=policy, clock=clock, event="action_submit", session_id=session_id, account_id=row["account_id"],
           user_id=row["user_id"], pending_id=pending_id, decision="needs_approval", codes=codes,
           args={"charge_id": charge.charge_id, "amount_cents": charge.amount_cents}, status=200, trace_id=trace_id,
           mode=mode, ticket_id=ticket.ticket_id, key=f"{pending_id}:submit")
    return result


def cancel_pending(conn: Any, *, policy: PolicyConfig, clock: Clock, pending_id: str, session_id: str,
                   trace_id: str | None = None, mode: str | None = None) -> CancelResult | CoreError:
    """What: cancel an open pending action (strict no or Cancel press). Use when: POST /v1/actions/pending/{id}/cancel
    (CORE_TOKEN_COMMIT only). Inputs: connection, policy, clock, id, session, trace id, mode.
    Output: CancelResult (also for a row already cancelled, so a retried cancel is harmless), or a CoreError
    PENDING_NOT_ACTIVE to raise after the transaction commits when the row had just expired (the flip is kept).
    Side effects: FOR UPDATE lock, one UPDATE, audit. Raises CoreError PENDING_NOT_ACTIVE for any other state.
    Role: doc 07 s7.1 row 5; never opens a ticket (R-3)."""
    row = _locked_row(conn, pending_id, session_id)
    now = clock.now()
    done = CancelResult(pending_action_id=pending_id, state="cancelled", changed=False)
    if row["state"] == "cancelled":
        return done
    if is_expired(row["state"], row["expires_at"], now):
        _set_state(conn, pending_id, "expired", now)
        _audit(conn, policy=policy, clock=clock, event="action_expired", session_id=session_id,
               account_id=row["account_id"], user_id=row["user_id"], pending_id=pending_id, decision=None, codes=None,
               args={"via": "action_cancel"}, status=409, trace_id=trace_id, mode=mode)
        return CoreError("PENDING_NOT_ACTIVE", details={"state": "expired"})
    if row["state"] not in OPEN_STATES:
        raise CoreError("PENDING_NOT_ACTIVE", details={"state": row["state"]})
    _set_state(conn, pending_id, "cancelled", now)
    _audit(conn, policy=policy, clock=clock, event="action_cancel", session_id=session_id, account_id=row["account_id"],
           user_id=row["user_id"], pending_id=pending_id, decision=None, codes=row["reason_codes"], args={},
           status=200, trace_id=trace_id, mode=mode)
    return done


def pending_status(conn: Any, *, clock: Clock, pending_id: str, session_id: str) -> PendingStatus:
    """What: the state of one pending action as the session may see it, with read-time expiry, its ticket and the
    stored commit result. Use when: GET /v1/actions/pending/{id} (agent tools and the confirm module's status check).
    Inputs: connection, clock, id, session. Output: PendingStatus. Side effects: SELECTs only (no expiry write).
    Raises CoreError NOT_FOUND. Role: doc 07 s3.3 PendingStatus."""
    row = _locked_row(conn, pending_id, session_id, lock=False)
    now = clock.now()
    ticket = None
    if row["ticket_id"]:
        t = conn.execute(f"SELECT {REF_COLUMNS} FROM tickets WHERE id = %s", (row["ticket_id"],)).fetchone()
        ticket = ticket_ref(t) if t else None
    result = None
    if row["state"] == "committed" and row["pending_kind"] == "commit" and row["result"]:
        result = CommitResult.model_validate(row["result"])
    return PendingStatus(pending_action_id=pending_id, action_type=row["action_type"],
                         state=read_state(row["state"], row["expires_at"], now),  # pyright: ignore[reportArgumentType]
                         decision=row["decision"], expires_at=_iso(row["expires_at"], now), ticket=ticket, result=result)
