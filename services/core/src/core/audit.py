"""What: the append-only audit writer. Builds a row from an event, redacts its args with contracts `redact`
(card PINs replaced, emails masked) and INSERTs it into core.audit_events.
Use when: any core code records a verify, prepare, commit, decline, ticket or fault event.
Inputs: an AuditEvent, a connection (inside the caller's transaction), the clock, a RedactContext.
Output: AuditEvent, audit_row, write_audit, write_refusal.
Side effects: write_audit INSERTs one row and logs one line (redacted facts only).
Role: doc 07 s11 and s12. The audit row commits or rolls back with the business effect it describes.
args_sha256 is over the REDACTED args: a hash of raw args would let anyone with the table try all 10,000 PINs.
The free-text PIN scanner (CS-E-03.S7.T9) plugs into `redact` and reaches this writer with no change here."""
from __future__ import annotations

import hashlib
import json
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from contracts.redact import RedactContext, redact
from psycopg.types.json import Jsonb

from core.clock import Clock

log = logging.getLogger("core.audit")

_COLUMNS = ("id", "ts", "trace_id", "session_id", "mode", "actor", "actor_id", "account_id", "event", "action_type",
            "pending_action_id", "ticket_id", "decision", "reason_codes", "policy_version", "args_redacted",
            "args_sha256", "result_status", "idempotency_key")


@dataclass(frozen=True)
class AuditEvent:
    """What: one thing to record. Use when: calling write_audit. Inputs: event name and actor (required), then
    optional trace_id, session_id, mode, actor_id, account_id, action_type, pending_action_id, ticket_id, decision,
    reason_codes, policy_version, args (raw, redacted by the writer), result_status, idempotency_key.
    Output: frozen value. Side effects: none. Role: the writer's input; args may hold a PIN, the row never does."""

    event: str
    actor: str
    trace_id: str | None = None
    session_id: uuid.UUID | None = None
    mode: str | None = None
    actor_id: str | None = None
    account_id: str | None = None
    action_type: str | None = None
    pending_action_id: str | None = None
    ticket_id: str | None = None
    decision: str | None = None
    reason_codes: list[str] | None = None
    policy_version: str | None = None
    args: Any = None
    result_status: int | None = None
    idempotency_key: str | None = None


def audit_row(ev: AuditEvent, context: RedactContext, ts: datetime, audit_id: uuid.UUID) -> dict[str, Any]:
    """What: turn an event into the column dict, redacting args and hashing the redacted JSON.
    Use when: write_audit, and unit tests that need no database. Inputs: event, redaction context, timestamp, id.
    Output: dict keyed by column name (args_redacted is plain data, wrapped for the driver in write_audit).
    Side effects: none. Role: the pure half of the writer, so the redaction rule is testable on its own."""
    redacted = redact(ev.args, context) if ev.args is not None else None
    digest = None
    if redacted is not None:
        canonical = json.dumps(redacted, sort_keys=True, separators=(",", ":"), default=str).encode()
        digest = hashlib.sha256(canonical).digest()
    return {
        "id": audit_id, "ts": ts, "trace_id": ev.trace_id, "session_id": ev.session_id, "mode": ev.mode,
        "actor": ev.actor, "actor_id": ev.actor_id, "account_id": ev.account_id, "event": ev.event,
        "action_type": ev.action_type, "pending_action_id": ev.pending_action_id, "ticket_id": ev.ticket_id,
        "decision": ev.decision, "reason_codes": ev.reason_codes, "policy_version": ev.policy_version,
        "args_redacted": redacted, "args_sha256": digest, "result_status": ev.result_status,
        "idempotency_key": ev.idempotency_key,
    }


def write_audit(conn: Any, ev: AuditEvent, *, clock: Clock, context: RedactContext | None = None) -> uuid.UUID:
    """What: insert one redacted audit row stamped by the injected clock and log that it happened.
    Use when: inside a request transaction, next to the effect being audited. Inputs: connection, event, clock,
    optional RedactContext (default: nothing awaiting, unverified, no step-up). Output: the audit row id.
    Side effects: one INSERT (no commit; the caller's transaction decides) and one INFO log line that names the
    event and ids only, never args. Role: the only code that writes audit_events."""
    audit_id = uuid.uuid4()
    row = audit_row(ev, context or RedactContext(), clock.now(), audit_id)
    row["args_redacted"] = Jsonb(row["args_redacted"]) if row["args_redacted"] is not None else None
    placeholders = ", ".join(["%s"] * len(_COLUMNS))
    conn.execute(f"INSERT INTO audit_events ({', '.join(_COLUMNS)}) VALUES ({placeholders})",
                 tuple(row[c] for c in _COLUMNS))
    log.info("audit id=%s event=%s actor=%s session=%s trace=%s", audit_id, ev.event, ev.actor, ev.session_id, ev.trace_id)
    return audit_id


def write_refusal(db: Any, *, event: str, code: str, status: int, session_id: str, clock: Clock,
                  policy_version: str | None, trace_id: str | None, mode: str | None, action_type: str | None = None,
                  pending_action_id: str | None = None, idempotency_key: str | None = None) -> None:
    """What: one refusal audit row in its own transaction, after the request transaction rolled back.
    Use when: a route raised a CoreError (prepare, commit, submit, cancel, ticket create), so the effect and any
    audit row inside it are gone. Inputs: the Database, event name (e.g. ticket_create_refused), the error code and
    HTTP status, session, clock, policy version, trace, mode, optional action type, pending id, idempotency key.
    Output: none. Side effects: one INSERT on audit_events; a failure is logged, never raised (the original error must
    reach the caller). Role: the one refusal writer (review M2, D-055; PR #78 review M-6)."""
    try:
        with db.tx() as conn:
            write_audit(conn, AuditEvent(
                event=event, actor="agent", trace_id=trace_id, session_id=uuid.UUID(session_id), mode=mode,
                action_type=action_type, pending_action_id=pending_action_id, decision="error",
                policy_version=policy_version, args={"code": code}, result_status=status,
                idempotency_key=idempotency_key), clock=clock)
    except Exception as audit_exc:  # noqa: BLE001 (the refusal itself must still be returned)
        log.warning("refusal audit not written event=%s code=%s error=%s", event, code, type(audit_exc).__name__)
