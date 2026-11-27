"""What: ticket create (verified merge, daily and per-session limits), the low-level ticket insert, the stage
`transition()` and the pure policy helpers (priority, queue, target label, id format, transition table).
Use when: POST /v1/tickets runs, or another core path (verify lockout, past-due decline, refund submit, operator
approve) needs a ticket row or a stage change.
Inputs: a connection inside the caller's transaction, PolicyConfig, the injected clock, ids and a TicketCreate.
Output: TicketOutcome (a TicketRef plus merged and created flags), TicketRef from insert_ticket and transition.
Side effects: INSERT and UPDATE on tickets, INSERT on ticket_events and audit_events, a transaction advisory lock per
account (verified) or per session (unverified).
Role: doc 07 s3.4 and s7.2, L1 s5 A3. Queue, priority and target label come from policy.yaml; the account tier is read
from the accounts table, never from the caller. Every change writes a ticket event and an audit row in the same
transaction as the change."""
from __future__ import annotations

import secrets
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

from contracts.core_api import Tier, TicketCreate, TicketRef
from contracts.policy import PolicyConfig, Priority, Queue

from core.audit import AuditEvent, write_audit
from core.clock import Clock
from core.errors import CoreError

Stage = Literal["open", "pending_approval", "resolved", "closed"]
Actor = Literal["customer", "agent", "operator", "system"]
# doc 07 s7.2: the only stage changes. Creation into open or pending_approval is not a transition.
TRANSITIONS: frozenset[tuple[str, str]] = frozenset({("open", "resolved"), ("pending_approval", "resolved"),
                                                     ("resolved", "closed")})
# Outcomes a resolve may carry, by the stage it starts from (doc 07 s7.2, s3.5: an operator resolve is for open
# tickets; Pending approval tickets end through approve or reject).
OUTCOMES_BY_STAGE: dict[str, frozenset[str]] = {
    "open": frozenset({"answered", "resolved_by_operator"}),
    "pending_approval": frozenset({"approved_executed", "rejected", "rules_changed"}),
}
_ID_ATTEMPTS = 5


@dataclass(frozen=True)
class TicketOutcome:
    """What: the result of create_ticket. Use when: the route builds its 200 body. Inputs: ref, merged, created.
    Output: frozen value. Side effects: none. Role: doc 07 s3.4 `{ticket, merged, created}`."""

    ref: TicketRef
    merged: bool
    created: bool


@dataclass(frozen=True)
class Caller:
    """What: who a session is for ticket purposes. Use when: create_ticket decides verified or not.
    Inputs: verified flag, user id, account id, tier. Output: frozen value. Side effects: none.
    Role: unverified callers carry no account, user or tier (never linked to a claimed account)."""

    verified: bool
    user_id: str | None = None
    account_id: str | None = None
    tier: Tier | None = None


def priority_for(policy: PolicyConfig, *, tier: Tier | None, verified: bool, blocking: bool) -> Priority:
    """What: the ticket priority. Use when: inserting a ticket. Inputs: policy, account tier, verified flag, the
    blocking flag. Output: P1 to P4. Side effects: none. Role: unverified is always P4; Enterprise blocking is the
    policy's blocking priority (P1); otherwise priority_by_tier (L1 s5 A3)."""
    cfg = policy.tickets
    if not verified or tier is None:
        return cfg.priority_by_tier["unverified"]
    if tier == "enterprise" and blocking:
        return cfg.enterprise_blocking_priority
    return cfg.priority_by_tier[tier]


def queue_for(policy: PolicyConfig, category: str) -> Queue:
    """What: the queue for a category. Use when: inserting a ticket. Inputs: policy, category.
    Output: queue name. Side effects: KeyError for an unknown category (the request model rejects those first).
    Role: policy.yaml tickets.category_queue."""
    return policy.tickets.category_queue[category]  # pyright: ignore[reportArgumentType]


def target_label_for(policy: PolicyConfig, priority: str, tier: Tier | None) -> str:
    """What: the first-response target label (text only, no timer). Use when: inserting a ticket.
    Inputs: policy, priority, account tier. Output: e.g. "4 hours". Side effects: none.
    Role: L1 s5 A3; P2 reads the Enterprise label for Enterprise accounts and the standard one otherwise."""
    key = "P2_enterprise" if priority == "P2" and tier == "enterprise" else priority
    return policy.tickets.target_label[key]  # pyright: ignore[reportArgumentType]


def new_ticket_id(clock: Clock, rng: Callable[[int], int] | None = None) -> str:
    """What: mint a ticket id "TCK-" + two-digit business year + six random digits (TCK-26118213).
    Use when: inserting a ticket. Inputs: clock, optional rng(n) returning an int below n (tests).
    Output: id text. Side effects: reads os randomness. Role: doc 07 s3.2 and D-043 format; the short id is the last four."""
    draw = rng or secrets.randbelow
    return f"TCK-{clock.today().year % 100:02d}{draw(1_000_000):06d}"


def check_transition(from_stage: str, to_stage: str) -> None:
    """What: accept only the doc 07 s7.2 stage changes. Use when: transition() runs. Inputs: two stages.
    Output: none. Side effects: raises CoreError INVALID_TRANSITION otherwise. Role: pure, unit-tested table."""
    if (from_stage, to_stage) not in TRANSITIONS:
        raise CoreError("INVALID_TRANSITION", "That ticket cannot move to that stage.",
                        details={"from_stage": from_stage, "to_stage": to_stage})


def _ref(row: Sequence[Any]) -> TicketRef:
    """What: build a TicketRef from (id, stage, priority, queue, target_label, verified). Use when: any read or insert.
    Inputs: a row. Output: TicketRef. Side effects: none. Role: one place for the display text."""
    tid = str(row[0])
    short = tid[-4:]
    return TicketRef(ticket_id=tid, short_id=short, display=f"ticket ending {short}", stage=row[1], priority=row[2],
                     queue=row[3], target_label=row[4], verified=bool(row[5]))


_REF_COLS = "id, stage, priority, queue, target_label, verified"


def _event(conn: Any, ticket_id: str, kind: str, *, actor: str, actor_id: str | None, at: datetime,
           from_stage: str | None = None, to_stage: str | None = None, body: str | None = None) -> None:
    """What: append one ticket_events row. Use when: a ticket is created, merged or changes stage.
    Inputs: connection, ticket id, kind, actor, actor id, time (injected clock), stages, body. Output: none.
    Side effects: one INSERT. Role: doc 07 s7.2 'every change writes ticket_events'."""
    conn.execute("INSERT INTO ticket_events (ticket_id, kind, from_stage, to_stage, actor, actor_id, body, at) "
                 "VALUES (%s,%s,%s,%s,%s,%s,%s,%s)", (ticket_id, kind, from_stage, to_stage, actor, actor_id, body, at))


def load_caller(conn: Any, session_id: uuid.UUID | str, now: datetime) -> Caller:
    """What: read the session's claims and the account tier. Use when: create_ticket starts. Inputs: connection,
    session id, now. Output: Caller; verified only if the claims name a user and have not expired.
    Side effects: one SELECT. Role: a locked, failed or expired session is unverified."""
    row = conn.execute("SELECT c.user_id, c.account_id, c.expires_at, a.tier FROM session_claims c "
                       "LEFT JOIN accounts a ON a.id = c.account_id WHERE c.session_id = %s", (session_id,)).fetchone()
    if row and row[0] and row[1] and row[2] and row[2] > now:
        return Caller(True, row[0], row[1], row[3])
    return Caller(False)


def insert_ticket(conn: Any, *, policy: PolicyConfig, clock: Clock, session_id: uuid.UUID | str,
                  account_id: str | None, user_id: str | None, tier: Tier | None, stage: Literal["open", "pending_approval"],
                  queue: Queue, category: str, summary: str, origin: Literal["model", "code", "core"], actor: Actor,
                  actor_id: str | None, blocking: bool = False, steps_tried: Sequence[str] = (),
                  confidence: float | None = None, transcript_ref: str | None = None,
                  escalation_reason: str | None = None, pending_action_id: str | None = None,
                  rng: Callable[[int], int] | None = None, trace_id: str | None = None, mode: str | None = None,
                  idempotency_key: str | None = None) -> TicketRef:
    """What: insert one ticket row with a fresh id, its created event and an audit row, retrying an id clash.
    Use when: create_ticket makes an Open ticket, or core makes a lockout, past-due or Pending approval ticket (that
    caller bypasses the limits on purpose). Inputs: connection, policy, clock, session, account and user (both None for
    unverified), account tier, stage (open or pending_approval), queue, category, summary, origin, actor, optional
    blocking, steps, confidence, transcript ref, escalation reason, pending action id (required for pending_approval),
    rng, trace id, mode, idempotency key. Output: TicketRef. Side effects: three INSERTs.
    Role: the one writer of tickets rows; priority and target label are computed here from policy. It takes no
    account lock and never merges: a caller that makes an Open ticket (past-due follow-up) must take the account
    lock `tickets:<account_id>` and merge by category itself; Pending approval tickets are exempt (one per action)."""
    verified = account_id is not None
    priority = priority_for(policy, tier=tier, verified=verified, blocking=blocking)
    label = target_label_for(policy, priority, tier if verified else None)
    now = clock.now()
    columns = ("id, session_id, verified, account_id, user_id, stage, queue, category, priority, target_label, summary, "
               "steps_tried, confidence, transcript_ref, origin, escalation_reason, pending_action_id, created_at, "
               "created_date, updated_at")
    for _ in range(_ID_ATTEMPTS):
        tid = new_ticket_id(clock, rng)
        row = conn.execute(
            f"INSERT INTO tickets ({columns}) VALUES ({', '.join(['%s'] * 20)}) ON CONFLICT (id) DO NOTHING RETURNING id",
            (tid, session_id, verified, account_id, user_id, stage, queue, category, priority, label, summary,
             list(steps_tried), confidence, transcript_ref, origin, escalation_reason, pending_action_id, now,
             clock.today(), now)).fetchone()
        if row:
            break
    else:
        raise CoreError("INTERNAL", "Could not allocate a ticket id.")
    _event(conn, tid, "created", actor=actor, actor_id=actor_id, at=now, to_stage=stage)
    write_audit(conn, AuditEvent(
        event="ticket_create", actor=actor, trace_id=trace_id, session_id=uuid.UUID(str(session_id)), mode=mode,
        actor_id=actor_id, account_id=account_id, ticket_id=tid, policy_version=policy.policy_version,
        args={"category": category, "origin": origin, "queue": queue, "priority": priority, "stage": stage,
              "blocking": blocking, "escalation_reason": escalation_reason},
        idempotency_key=idempotency_key), clock=clock)
    return TicketRef(ticket_id=tid, short_id=tid[-4:], display=f"ticket ending {tid[-4:]}", stage=stage,
                     priority=priority, queue=queue, target_label=label, verified=verified)


def _limit_error(conn: Any, where: str, args: tuple[Any, ...], verified: bool) -> CoreError:
    """What: build the 429 TICKET_LIMIT_REACHED error with `existing`. Use when: a count reaches its limit.
    Inputs: connection, a WHERE clause over tickets, its args, verified flag. Output: CoreError (not raised).
    Side effects: one SELECT. Role: `existing` is the newest ticket in the counted set, so the agent can say
    "you already have ticket ending 8213"."""
    row = conn.execute(f"SELECT {_REF_COLS} FROM tickets WHERE {where} ORDER BY created_at DESC, id DESC LIMIT 1",
                       args).fetchone()
    message = ("This account has reached today's limit for support tickets." if verified
               else "This chat has already opened the maximum number of support tickets.")
    return CoreError("TICKET_LIMIT_REACHED", message,
                     details={"existing": _ref(row).model_dump(mode="json") if row else None})


def create_ticket(conn: Any, *, policy: PolicyConfig, clock: Clock, session_id: uuid.UUID | str, body: TicketCreate,
                  actor: Actor = "agent", rng: Callable[[int], int] | None = None, trace_id: str | None = None,
                  mode: str | None = None, idempotency_key: str | None = None) -> TicketOutcome:
    """What: create a ticket for a session, or merge into the account's open ticket of the same category.
    Use when: POST /v1/tickets (the model tool and agent code escalations). Inputs: connection (inside the request
    transaction), policy, clock, session id, TicketCreate, actor, optional rng, trace id, mode, idempotency key.
    Output: TicketOutcome. Side effects: an advisory lock (account or session), INSERTs or one merge event, audit row.
    Raises CoreError TICKET_LIMIT_REACHED (details.existing) at the per-account daily limit (verified) or the
    per-session limit (unverified). Role: doc 07 s3.4. Verified: merge first (a merge makes no ticket, so it is never
    limited), then the daily limit counts every ticket of the account created on the business date, then insert.
    Unverified: always P4, never linked to an account, never merged, limited per session."""
    caller = load_caller(conn, session_id, clock.now())
    limits = policy.tickets.limits
    if caller.verified:
        conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (f"tickets:{caller.account_id}",))
        open_row = conn.execute(
            "SELECT id FROM tickets WHERE account_id = %s AND category = %s AND stage = 'open' "
            "ORDER BY created_at DESC, id DESC LIMIT 1 FOR UPDATE", (caller.account_id, body.category)).fetchone()
        if open_row:
            tid = open_row[0]
            now = clock.now()
            _event(conn, tid, "merged", actor=actor, actor_id=caller.user_id, at=now, body=body.summary)
            conn.execute("UPDATE tickets SET updated_at = %s WHERE id = %s", (now, tid))
            write_audit(conn, AuditEvent(
                event="ticket_merge", actor=actor, trace_id=trace_id, session_id=uuid.UUID(str(session_id)), mode=mode,
                actor_id=caller.user_id, account_id=caller.account_id, ticket_id=tid,
                policy_version=policy.policy_version, args={"category": body.category, "origin": body.origin},
                idempotency_key=idempotency_key), clock=clock)
            ref = _ref(conn.execute(f"SELECT {_REF_COLS} FROM tickets WHERE id = %s", (tid,)).fetchone())
            return TicketOutcome(ref, merged=True, created=False)
        # doc 07 s8.1: approvals (every approval ticket has a pending action) and merges are not counted
        where, args = ("account_id = %s AND created_date = %s AND pending_action_id IS NULL",
                       (caller.account_id, clock.today()))
        cap = limits.verified_per_account_per_day
    else:
        conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (f"tickets:session:{session_id}",))
        where, args = "session_id = %s AND verified = false", (session_id,)
        cap = limits.unverified_per_session
    if conn.execute(f"SELECT count(*) FROM tickets WHERE {where}", args).fetchone()[0] >= cap:
        raise _limit_error(conn, where, args, caller.verified)
    ref = insert_ticket(
        conn, policy=policy, clock=clock, session_id=session_id, account_id=caller.account_id, user_id=caller.user_id,
        tier=caller.tier, stage="open", queue=queue_for(policy, body.category), category=body.category,
        summary=body.summary, origin=body.origin, actor=actor, actor_id=caller.user_id, blocking=body.blocking,
        steps_tried=body.steps_tried, confidence=body.confidence, transcript_ref=body.transcript_ref,
        escalation_reason=body.escalation_reason, rng=rng, trace_id=trace_id, mode=mode, idempotency_key=idempotency_key)
    return TicketOutcome(ref, merged=False, created=True)


def transition(conn: Any, *, policy: PolicyConfig, clock: Clock, ticket_id: str, to_stage: Stage, actor: Actor,
               actor_id: str | None, outcome: str | None = None, note: str | None = None,
               trace_id: str | None = None) -> TicketRef:
    """What: move a ticket to a new stage by the doc 07 s7.2 table, with a stage_changed event and an audit row.
    Use when: an operator resolves or closes, or approve and reject finish a Pending approval ticket.
    Inputs: connection, policy, clock, ticket id, target stage, actor, actor id, outcome (required for resolved and checked
    against the starting stage: open takes answered or resolved_by_operator, pending_approval takes approved_executed,
    rejected or rules_changed), optional customer-safe note,
    trace id. Output: the updated TicketRef. Side effects: SELECT FOR UPDATE, one UPDATE, one event, one audit row.
    Raises CoreError NOT_FOUND, INVALID_TRANSITION or VALIDATION_ERROR (missing or wrong outcome); nothing changes.
    Role: the single stage writer; the slice itself creates Open and Pending approval, the rest is the designed
    operator path, kept here so the table is enforced in one place."""
    row = conn.execute("SELECT stage, account_id FROM tickets WHERE id = %s FOR UPDATE", (ticket_id,)).fetchone()
    if row is None:
        raise CoreError("NOT_FOUND")
    from_stage, account_id = row
    check_transition(from_stage, to_stage)
    if to_stage == "resolved" and outcome not in OUTCOMES_BY_STAGE[from_stage]:
        raise CoreError("VALIDATION_ERROR", "That outcome does not fit this ticket.",
                        details={"reason": "outcome_invalid", "from_stage": from_stage})
    now = clock.now()
    if to_stage == "resolved":
        conn.execute("UPDATE tickets SET stage = %s, outcome = %s, outcome_note = %s, updated_at = %s WHERE id = %s",
                     (to_stage, outcome, note, now, ticket_id))
    else:
        conn.execute("UPDATE tickets SET stage = %s, updated_at = %s WHERE id = %s", (to_stage, now, ticket_id))
    _event(conn, ticket_id, "stage_changed", actor=actor, actor_id=actor_id, at=now, from_stage=from_stage,
           to_stage=to_stage, body=note)
    write_audit(conn, AuditEvent(
        event="ticket_transition", actor=actor, trace_id=trace_id, actor_id=actor_id, account_id=account_id,
        ticket_id=ticket_id, policy_version=policy.policy_version,
        args={"from_stage": from_stage, "to_stage": to_stage, "outcome": outcome}), clock=clock)
    return _ref(conn.execute(f"SELECT {_REF_COLS} FROM tickets WHERE id = %s", (ticket_id,)).fetchone())


def session_lockout_ticket(conn: Any, session_id: uuid.UUID | str) -> TicketRef | None:
    """What: the newest core-made access ticket of an unverified session (the verify lockout ticket).
    Use when: a locked session tries to verify again and the answer should name the ticket. Inputs: connection,
    session id. Output: TicketRef or None. Side effects: one SELECT. Role: lets VERIFY_LOCKED carry the same ticket
    on every later attempt (doc 07 s3.1 `VERIFY_LOCKED {ticket}`)."""
    row = conn.execute(f"SELECT {_REF_COLS} FROM tickets WHERE session_id = %s AND verified = false AND origin = 'core' "
                       "AND category = 'access' ORDER BY created_at DESC, id DESC LIMIT 1", (session_id,)).fetchone()
    return _ref(row) if row else None
