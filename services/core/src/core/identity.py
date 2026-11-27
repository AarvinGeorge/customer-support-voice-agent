"""What: identity for core: normalise an account id and PIN, check the PIN by constant-time HMAC compare, keep the
per-session and per-account failure counts, lock and build Claims.
Use when: POST /v1/identity/verify or GET /v1/sessions/{id}/claims runs, or a read route needs the verified person.
Inputs: a connection inside the caller's transaction, PolicyConfig, the injected clock, the pepper, ids, the raw PIN.
Output: normalize_account_id, normalize_pin, permissions_for, attempts_left, load_claims, read_unverified,
require_claims, verify, VerifyOutcome.
Side effects: verify takes a transaction advisory lock per session, writes session_claims, verify_attempts and
audit_events. A lockout opens no ticket (D-070: a ticket needs a known requester, and a locked session has none).
Nothing here logs or audits the PIN.
Role: doc 07 s3.1, s8.1 verify rows, L1 s10. Core resolves the person only from session_claims for the session id; the
tier, role and status are read live from users and accounts, never cached in the claims row. Step-up is designed only."""
from __future__ import annotations

import hashlib
import hmac
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Literal

from contracts.core_api import Claims, Permissions, UnverifiedClaims
from contracts.policy import PolicyConfig

from core.audit import AuditEvent, write_audit
from core.clock import Clock
from core.errors import CoreError
from core.seeds.seed import pin_hmac

_ACCOUNT_DIGITS = re.compile(r"^[0-9]{5}$")


def normalize_account_id(raw: str) -> str | None:
    """What: turn "qs 10423", "QS-10423", "10423" or "Q S - 1 0 4 2 3" into "QS-10423"; None for anything else.
    Use when: a verify request arrives. Inputs: the account id as typed or spoken. Output: canonical id or None.
    Side effects: none. Role: doc 09 'normalization'; the shape check is free of data, so a 422 reveals nothing."""
    text = re.sub(r"[\s\-_.]", "", raw).upper()
    if text.startswith("QS"):
        text = text[2:]
    return f"QS-{text}" if _ACCOUNT_DIGITS.match(text) else None


def normalize_pin(raw: str, digits: int = 4) -> str | None:
    """What: drop spaces and dashes from a PIN and accept exactly `digits` digits. Use when: a verify request arrives.
    Inputs: the PIN as typed or spoken, digit count from policy. Output: the digits or None. Side effects: none.
    Role: normalization before the HMAC; the value is never logged by this module."""
    text = re.sub(r"[\s\-]", "", raw)
    return text if re.fullmatch(rf"[0-9]{{{digits}}}", text) else None


def permissions_for(policy: PolicyConfig, role: str) -> Permissions:
    """What: what a role may do. Use when: building Claims. Inputs: policy, role. Output: Permissions.
    Side effects: none. Role: doc 07 s3.1; billing read and actions follow policy roles.billing_roles."""
    billing = role in policy.roles.billing_roles
    return Permissions(billing_read=billing, billing_actions=billing, own_notification_prefs=True)


def attempts_left(policy: PolicyConfig, failed: int, locked: bool) -> int:
    """What: tries left in a session. Use when: a failure or an unverified claims read is answered.
    Inputs: policy, failures counted, locked flag. Output: int from 0 to max_failures_per_session.
    Side effects: none. Role: doc 07 s3.1 attempts_left."""
    return 0 if locked else max(0, policy.identity.max_failures_per_session - failed)


def _iso(value: datetime, now: datetime) -> str:
    """What: format a database timestamp in the clock's zone. Use when: building Claims. Inputs: value, now (zone source).
    Output: ISO text. Side effects: none. Role: one time format across the identity answers."""
    return value.astimezone(now.tzinfo).isoformat()


def load_claims(conn: Any, session_id: uuid.UUID | str, now: datetime, policy: PolicyConfig) -> Claims | None:
    """What: the session's Claims if it holds an unexpired verification. Use when: claims are read, verify repeats,
    a read route needs the person. Inputs: connection, session id, now, policy. Output: Claims or None.
    Side effects: one SELECT. Role: role, tier and status are read live, so a downgrade shows at the next read."""
    row = conn.execute(
        "SELECT c.session_id, c.user_id, u.display_name, c.account_id, a.name, u.role, a.tier, a.contract_status, "
        "a.grace_until, c.expires_at FROM session_claims c JOIN users u ON u.id = c.user_id "
        "JOIN accounts a ON a.id = c.account_id WHERE c.session_id = %s AND c.expires_at > %s",
        (session_id, now)).fetchone()
    if row is None:
        return None
    return Claims(verified=True, session_id=str(row[0]), user_id=row[1], display_name=row[2], account_id=row[3],
                  account_name=row[4], role=row[5], tier=row[6], contract_status=row[7],
                  grace_until=row[8].isoformat() if row[8] else None, permissions=permissions_for(policy, row[5]),
                  expires_at=_iso(row[9], now))


def read_unverified(conn: Any, session_id: uuid.UUID | str, policy: PolicyConfig) -> UnverifiedClaims:
    """What: the failure counters of a session that is not verified. Use when: claims are read for it.
    Inputs: connection, session id, policy. Output: UnverifiedClaims (zero failures for an unknown session).
    Side effects: one SELECT. Role: doc 07 s3.1 'unknown session returns verified: false'."""
    row = conn.execute("SELECT failed_verifies, verify_locked FROM session_claims WHERE session_id = %s",
                       (session_id,)).fetchone()
    failed, locked = (row[0], row[1]) if row else (0, False)
    return UnverifiedClaims(verified=False, verify_locked=locked, failed_verifies=failed,
                            attempts_left=attempts_left(policy, failed, locked))


def require_claims(conn: Any, session_id: uuid.UUID | str, now: datetime, policy: PolicyConfig) -> Claims:
    """What: the verified Claims or a 401. Use when: a route needs the person (context, charges, tickets).
    Inputs: connection, session id, now, policy. Output: Claims. Side effects: raises CoreError SESSION_NOT_VERIFIED.
    Role: the single gate for 'verified' on reads."""
    claims = load_claims(conn, session_id, now, policy)
    if claims is None:
        raise CoreError("SESSION_NOT_VERIFIED", "This chat is not verified yet.")
    return claims


@dataclass(frozen=True)
class VerifyOutcome:
    """What: the result of verify. Use when: the route turns it into a 200 or an error. Inputs: kind, claims,
    attempts_left. Output: frozen value. Side effects: none.
    Role: lets the route commit the counters first and raise afterwards (a raised error would roll them back)."""

    kind: Literal["ok", "failed", "locked", "conflict"]
    claims: Claims | None = None
    attempts_left: int = 0


def _match_user(conn: Any, pepper: str, account_id: str, pin: str) -> str | None:
    """What: find the user whose PIN HMAC equals the submitted one, comparing every candidate in constant time.
    Use when: verify checks credentials. Inputs: connection, pepper, normalised account id, normalised PIN.
    Output: user id or None. Side effects: one SELECT. Role: the same work (one HMAC, one compare per user, a dummy
    compare when the account has none) runs for a wrong PIN and an unknown account, so timing and answers agree."""
    computed = pin_hmac(pepper, account_id, pin)
    rows = conn.execute("SELECT id, pin_hmac FROM users WHERE account_id = %s", (account_id,)).fetchall()
    found: str | None = None
    for user_id, stored in rows:
        if hmac.compare_digest(bytes(stored), computed):
            found = user_id
    if not rows:
        hmac.compare_digest(hashlib.sha256(computed).digest(), computed)
    return found


def _account_locked(conn: Any, policy: PolicyConfig, account_id: str, now: datetime) -> bool:
    """What: is the claimed account locked by failures. Use when: verify starts. Inputs: connection, policy, the
    claimed id (real or not), now. Output: bool. Side effects: one SELECT. Role: doc 07 s3.1: `failures` failures
    within `window_minutes` lock the account for `lock_minutes` from the newest one (unknown ids lock the same way)."""
    lock = policy.identity.account_lock
    rows = conn.execute("SELECT at FROM verify_attempts WHERE claimed_account_id = %s AND kind = 'verify' AND ok = false "
                        "ORDER BY at DESC LIMIT %s", (account_id, lock.failures)).fetchall()
    if len(rows) < lock.failures:
        return False
    newest, oldest = rows[0][0], rows[-1][0]
    return newest - oldest <= timedelta(minutes=lock.window_minutes) and now < newest + timedelta(minutes=lock.lock_minutes)


def _attempt(conn: Any, session_id: uuid.UUID | str, account_id: str, ok: bool, now: datetime) -> None:
    """What: record one verify attempt. Use when: credentials were checked. Inputs: connection, session, claimed id,
    result, now. Output: none. Side effects: one INSERT. Role: feeds the account lock; holds no PIN."""
    conn.execute("INSERT INTO verify_attempts (session_id, claimed_account_id, kind, ok, at) VALUES (%s,%s,'verify',%s,%s)",
                 (session_id, account_id, ok, now))


def _audit(conn: Any, *, policy: PolicyConfig, clock: Clock, event: str, session_id: uuid.UUID | str, account_id: str,
           trace_id: str | None, mode: str | None, ok: bool, status: int, user_id: str | None = None,
           extra: dict[str, Any] | None = None) -> None:
    """What: one audit row for a verify attempt. Use when: an attempt ends. Inputs: connection, policy, clock, event
    name, session, the claimed id, trace, mode, success flag, HTTP status, matched user, extra args. Output: none.
    Side effects: one INSERT. Role: audit with the claimed id in args only (an unlinked claim is not account_id) and no PIN."""
    write_audit(conn, AuditEvent(
        event=event, actor="customer", trace_id=trace_id, session_id=uuid.UUID(str(session_id)), mode=mode,
        actor_id=user_id, account_id=account_id if ok else None, decision="allowed" if ok else "declined",
        policy_version=policy.policy_version, result_status=status,
        args={"claimed_account_id": account_id, "ok": ok, **(extra or {})}), clock=clock)


def verify(conn: Any, *, policy: PolicyConfig, clock: Clock, pepper: str, session_id: uuid.UUID | str,
           account_id: str, pin: str, trace_id: str | None = None, mode: str | None = None) -> VerifyOutcome:
    """What: check one verification attempt for a session and record the result.
    Use when: POST /v1/identity/verify. Inputs: connection (inside the request transaction), policy, clock, pepper,
    session id, NORMALISED account id and PIN, trace id, mode. Output: VerifyOutcome. Side effects: advisory lock
    per session, session_claims upsert, verify_attempts and audit rows (no ticket on lockout, D-070).
    Role: doc 07 s3.1. Order: session lock, account lock, credentials. A locked session or account answers `locked`
    without counting. A verified session that sends the same person's credentials gets the same claims; another
    person's valid credentials give `conflict`; wrong credentials fail without counting against the session."""
    ident = policy.identity
    now = clock.now()
    conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (f"verify:{session_id}",))
    conn.execute("INSERT INTO session_claims (session_id, created_at) VALUES (%s,%s) ON CONFLICT (session_id) DO NOTHING",
                 (session_id, now))
    user_id, expires, failed, locked = conn.execute(
        "SELECT user_id, expires_at, failed_verifies, verify_locked FROM session_claims WHERE session_id = %s FOR UPDATE",
        (session_id,)).fetchone()
    held = user_id if (user_id is not None and expires is not None and expires > now) else None
    common = {"policy": policy, "clock": clock, "session_id": session_id, "account_id": account_id,
              "trace_id": trace_id, "mode": mode}
    if locked or _account_locked(conn, policy, account_id, now):
        _audit(conn, event="verify_blocked", ok=False, status=423, extra={"reason": "session" if locked else "account"},
               **common)
        return VerifyOutcome("locked")
    matched = _match_user(conn, pepper, account_id, pin)
    if matched is not None:
        _attempt(conn, session_id, account_id, True, now)
        if held is not None and matched != held:
            _audit(conn, event="verify_conflict", ok=False, status=409, **common)
            return VerifyOutcome("conflict")
        if held is None:
            conn.execute(
                "UPDATE session_claims SET user_id = %s, account_id = %s, verified_at = %s, expires_at = %s, "
                "failed_verifies = 0 WHERE session_id = %s",
                (matched, account_id, now, now + timedelta(minutes=ident.session_ttl_minutes), session_id))
        claims = load_claims(conn, session_id, now, policy)
        assert claims is not None  # just written (or already held), same transaction
        _audit(conn, event="verify_ok", ok=True, status=200, user_id=matched, **common)
        return VerifyOutcome("ok", claims=claims)
    _attempt(conn, session_id, account_id, False, now)
    if held is not None:
        _audit(conn, event="verify_failed", ok=False, status=401, extra={"session_verified": True}, **common)
        return VerifyOutcome("failed", attempts_left=attempts_left(policy, failed, False))
    failed += 1
    lock_now = failed >= ident.max_failures_per_session
    conn.execute("UPDATE session_claims SET failed_verifies = %s, verify_locked = %s WHERE session_id = %s",
                 (failed, lock_now, session_id))
    _audit(conn, event="verify_failed", ok=False, status=423 if lock_now else 401,
           extra={"failed_verifies": failed, "locked": lock_now}, **common)
    if not lock_now:
        return VerifyOutcome("failed", attempts_left=attempts_left(policy, failed, False))
    return VerifyOutcome("locked")
