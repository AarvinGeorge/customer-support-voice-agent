"""What: Postgres-backed tests of POST /v1/identity/verify and GET /v1/sessions/{id}/claims over HTTP.
Use when: changing core/identity.py or core/identity_routes.py. Inputs: the Compose Postgres (marker pg).
Output: pytest results. Side effects: committed rows under random ids, removed with the owner role at teardown
(audit rows stay: audit_events is append-only by design).
Role: CS-E-03.S1.T1 Done-when: attempts_left 2, then 1, then 423 with a ticket ref; a second identity in one
session gets 409 IDENTITY_CONFLICT. Failures must persist across requests, so these tests commit."""
from __future__ import annotations

import json
import logging
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import psycopg
import pytest
from fastapi.testclient import TestClient

from core.app import create_app
from core.clock import FixedClock
from core.config import Settings
from core.seeds.seed import pin_hmac
from tests.conftest import TOKENS
from tests.pin_probe import leaks

pytestmark = pytest.mark.pg
NY = ZoneInfo("America/New_York")
POLICY_PATH = Path(__file__).resolve().parents[4] / "data" / "policy.yaml"
PEPPER = "test-pepper-not-a-secret"
START = datetime(2026, 11, 27, 9, 30, tzinfo=NY)
BANNED_PINS = ("4821", "7352", "1357", "6093")  # every PIN these tests use: no random id may contain one


def pick_ids(draw) -> tuple[str, str, str, str, str]:
    """What: random account and user ids, redrawn until none contains a PIN used in these tests.
    Use when: building a World. Inputs: draw, a callable returning a random int. Output: (a, b, owner, member, owner_b).
    Side effects: none. Role: keeps PIN-absence probes free of chance matches on ids."""
    while True:
        n = draw() % 40000 + 10000
        suffix = draw() % 900 + 100
        ids = (f"QS-{n}", f"QS-{n + 40000}", f"U-{suffix}1", f"U-{suffix}2", f"U-{suffix}3")
        if not any(p in i for i in ids for p in BANNED_PINS):
            return ids


class World:
    """What: two committed accounts: A (pro) with an owner and a member, B (business) with an owner.
    Use when: a test needs real credentials. Inputs: a connection (autocommit), the pepper. Output: ids and PINs.
    Side effects: INSERTs accounts and users. Role: test data builder."""

    def __init__(self, conn: psycopg.Connection) -> None:
        """What: insert the rows. Use when: constructing. Inputs: connection. Output: none.
        Side effects: two accounts, three users. Role: credentials for verify."""
        self.a, self.b, self.owner, self.member, self.owner_b = pick_ids(lambda: uuid.uuid4().int)
        self.pins = {self.owner: "4821", self.member: "7352", self.owner_b: "1357"}
        conn.execute("INSERT INTO accounts (id, name, tier, contract_status, seats, grace_until) "
                     "VALUES (%s,'Acme','pro','active',5,NULL)", (self.a,))
        conn.execute("INSERT INTO accounts (id, name, tier, contract_status, seats) "
                     "VALUES (%s,'Globex','business','past_due',50)", (self.b,))
        for uid, acct, name, role in ((self.owner, self.a, "Priya", "owner"), (self.member, self.a, "Sam", "member"),
                                      (self.owner_b, self.b, "Gina", "billing_admin")):
            conn.execute("INSERT INTO users (id, account_id, display_name, role, pin_hmac) VALUES (%s,%s,%s,%s,%s)",
                         (uid, acct, name, role, pin_hmac(PEPPER, acct, self.pins[uid])))


def test_fixture_ids_never_contain_a_fixture_pin():
    """What: pick_ids redraws when the first draw is unlucky (n=14821 gives QS-14821, which holds 4821).
    Use when: always. Inputs: none. Output: assertions. Side effects: none.
    Role: forced reproduction of the PIN-probe chance flake on random account ids, and the fix."""
    draws = iter([4821, 5, 7, 7])  # attempt 1: QS-14821 (bad), attempt 2: QS-10007 (fine)
    a, b, owner, member, owner_b = pick_ids(lambda: next(draws))
    assert a == "QS-10007" and b == "QS-50007"
    assert not any(p in i for i in (a, b, owner, member, owner_b) for p in BANNED_PINS)
    unlucky_user = iter([5000, 482, 6000, 3])  # user id suffix 482 gives U-4821
    ids = pick_ids(lambda: next(unlucky_user))
    assert not any(p in i for i in ids for p in BANNED_PINS)


def count_tickets(conn: psycopg.Connection, sid: str) -> int:
    """What: how many tickets a session has. Use when: asserting the lockout ticket rule. Inputs: connection, session
    id. Output: int. Side effects: one SELECT. Role: shorthand that keeps the type checker happy."""
    row = conn.execute("SELECT count(*) FROM tickets WHERE session_id=%s::uuid", (sid,)).fetchone()
    return row[0] if row else -1


@pytest.fixture
def stack(app_url, owner_url, migrated):
    """What: a TestClient on the real pool, a committed World, a settable clock and a session-id tracker; removes
    every row it made at teardown. Use when: HTTP-level pg tests. Inputs: urls. Output: SimpleNamespace.
    Side effects: committed rows, removed at teardown. Role: end-to-end harness."""
    settings = Settings(token_agent=TOKENS["agent_tools"], token_commit=TOKENS["agent_commit"], token_ops=TOKENS["ops"],
                        db_url=app_url, demo_faults=False, policy_path=str(POLICY_PATH), pin_pepper=PEPPER)
    clk = FixedClock(START)
    sessions: list[str] = []
    with psycopg.connect(app_url, autocommit=True) as c:
        w = World(c)
    with TestClient(create_app(settings, clock=clk)) as client:
        def verify(session: str, account: str, pin: str, *, headers=None):
            """What: POST verify as the agent. Inputs: session id, account id, pin. Output: response."""
            if session not in sessions:
                sessions.append(session)
            return client.post("/v1/identity/verify", json={"account_id": account, "pin": pin},
                               headers={"Authorization": f"Bearer {TOKENS['agent_tools']}", "X-Session-Id": session,
                                        "X-Mode": "chat", **(headers or {})})

        def claims(session: str):
            """What: GET claims. Inputs: session id. Output: response."""
            return client.get(f"/v1/sessions/{session}/claims",
                              headers={"Authorization": f"Bearer {TOKENS['agent_tools']}"})

        def new_session() -> str:
            """What: a fresh session uuid, tracked for cleanup. Output: str."""
            sid = str(uuid.uuid4())
            sessions.append(sid)
            return sid

        yield type("S", (), {"client": client, "w": w, "verify": staticmethod(verify), "claims": staticmethod(claims),
                             "new_session": staticmethod(new_session), "clock": clk, "app_url": app_url,
                             "owner_url": owner_url})
    with psycopg.connect(owner_url, autocommit=True) as o:
        ids = [r[0] for r in o.execute("SELECT id FROM tickets WHERE session_id = ANY(%s::uuid[])", (sessions,)).fetchall()]
        o.execute("DELETE FROM ticket_events WHERE ticket_id = ANY(%s)", (ids,))
        o.execute("DELETE FROM tickets WHERE id = ANY(%s)", (ids,))
        o.execute("DELETE FROM idempotency WHERE key LIKE 'idn-%'")
        o.execute("DELETE FROM verify_attempts WHERE session_id = ANY(%s::uuid[]) OR claimed_account_id = ANY(%s)",
                  (sessions, [w.a, w.b]))
        o.execute("DELETE FROM session_claims WHERE session_id = ANY(%s::uuid[])", (sessions,))
        o.execute("DELETE FROM users WHERE account_id = ANY(%s)", ([w.a, w.b],))
        o.execute("DELETE FROM accounts WHERE id = ANY(%s)", ([w.a, w.b],))


def test_done_when_attempts_left_2_then_1_then_423_with_ticket(stack):
    """What: wrong PIN three times gives 401 attempts_left 2, 401 attempts_left 1, then 423 VERIFY_LOCKED with a
    ticket ref; the ticket is P4, category access, queue technical, with no account link; a fourth try with the right
    PIN is still 423. Use when: always. Inputs: stack. Output: assertions. Side effects: committed rows.
    Role: the S1.T1 Done-when over the wire."""
    s, sid = stack, stack.new_session()
    r1 = s.verify(sid, s.w.a, "0000")
    assert r1.status_code == 401
    e1 = r1.json()["error"]
    assert (e1["code"], e1["class"], e1["retryable"], e1["changed"]) == ("VERIFY_FAILED", "access_denied", False, False)
    assert e1["details"]["attempts_left"] == 2 and e1["details"]["ticket"] is None
    r2 = s.verify(sid, s.w.a, "1111")
    assert r2.status_code == 401 and r2.json()["error"]["details"]["attempts_left"] == 1
    r3 = s.verify(sid, s.w.a, "2222")
    assert r3.status_code == 423
    e3 = r3.json()["error"]
    assert e3["code"] == "VERIFY_LOCKED"
    ticket = e3["details"]["ticket"]
    assert ticket["ticket_id"].startswith("TCK-26") and ticket["priority"] == "P4" and ticket["verified"] is False
    assert ticket["queue"] == "technical" and ticket["stage"] == "open"
    with psycopg.connect(s.app_url) as c:
        row = c.execute("SELECT category, account_id, user_id, origin, session_id::text FROM tickets WHERE id=%s",
                        (ticket["ticket_id"],)).fetchone()
        assert row == ("access", None, None, "core", sid)
    # still locked with the right PIN, and claims say so
    assert s.verify(sid, s.w.a, "4821").status_code == 423
    c = s.claims(sid).json()
    assert c == {"verified": False, "verify_locked": True, "failed_verifies": 3, "attempts_left": 0}


def test_unknown_account_and_wrong_pin_give_the_same_answer(stack):
    """What: an account that does not exist and a real account with a wrong PIN produce identical status, code,
    message and details (so an attacker cannot enumerate accounts). Use when: always. Inputs: stack.
    Output: assertions. Side effects: committed rows. Role: doc 07 s3.1 'same response'."""
    s = stack
    a = s.verify(s.new_session(), s.w.a, "0000")
    b = s.verify(s.new_session(), "QS-99998", "0000")
    assert a.status_code == b.status_code == 401
    ea, eb = a.json()["error"], b.json()["error"]
    for k in ("code", "class", "message", "retryable", "changed", "details"):
        assert ea[k] == eb[k], k


def test_success_returns_claims_and_claims_endpoint_agrees(stack):
    """What: the right account and PIN give 200 Claims (role, tier, status, permissions, 120 minute expiry); GET
    claims returns the same body; the owner has billing permissions and the member does not. Use when: always.
    Inputs: stack. Output: assertions. Side effects: committed rows. Role: doc 07 s3.1."""
    s, sid = stack, stack.new_session()
    r = s.verify(sid, s.w.a, "4821")
    assert r.status_code == 200, r.text
    body = r.json()
    assert (body["verified"], body["user_id"], body["display_name"], body["account_id"], body["account_name"]) == \
        (True, s.w.owner, "Priya", s.w.a, "Acme")
    assert (body["role"], body["tier"], body["contract_status"], body["grace_until"]) == ("owner", "pro", "active", None)
    assert body["permissions"] == {"billing_read": True, "billing_actions": True, "own_notification_prefs": True}
    assert body["session_id"] == sid
    assert datetime.fromisoformat(body["expires_at"]) == START + timedelta(minutes=120)
    assert s.claims(sid).json() == body
    sm = s.new_session()
    m = s.verify(sm, s.w.a, "7352").json()
    assert m["role"] == "member" and m["permissions"]["billing_read"] is False and m["permissions"]["billing_actions"] is False


def test_input_is_normalised_over_the_wire(stack):
    """What: 'qs 10423'-style ids and '4 8 2 1' PINs verify. Use when: always. Inputs: stack. Output: assertions.
    Side effects: committed rows. Role: doc 09 'normalization'."""
    s = stack
    r = s.verify(s.new_session(), s.w.a.lower().replace("-", " "), "4 8 2 1")
    assert r.status_code == 200 and r.json()["account_id"] == s.w.a


def test_reverify_same_person_is_idempotent_and_a_second_identity_is_409(stack):
    """What: verifying again as the same person is 200 with the same claims (expiry unchanged); another account or
    another user of the same account in that session is 409 IDENTITY_CONFLICT and changes nothing. Use when: always.
    Inputs: stack. Output: assertions. Side effects: committed rows. Role: doc 07 s3.1, Done-when second identity."""
    s, sid = stack, stack.new_session()
    first = s.verify(sid, s.w.a, "4821").json()
    s.clock.set(START + timedelta(minutes=5))
    again = s.verify(sid, s.w.a, "4821")
    assert again.status_code == 200 and again.json() == first
    other_account = s.verify(sid, s.w.b, "1357")
    assert other_account.status_code == 409
    err = other_account.json()["error"]
    assert (err["code"], err["class"], err["retryable"], err["changed"]) == ("IDENTITY_CONFLICT", "core_conflict", False, False)
    assert s.verify(sid, s.w.a, "7352").status_code == 409
    assert s.claims(sid).json() == first
    s.clock.set(START)


def test_wrong_pin_in_a_verified_session_does_not_lock_the_session(stack):
    """What: wrong PINs after a successful verify answer 401 but never set verify_locked or open a ticket, and the
    verified claims stay. Use when: always. Inputs: stack. Output: assertions. Side effects: committed rows.
    Role: the session lock is for unverified sessions."""
    s, sid = stack, stack.new_session()
    s.verify(sid, s.w.a, "4821")
    for _ in range(4):
        assert s.verify(sid, s.w.a, "9999").status_code == 401
    c = s.claims(sid).json()
    assert c["verified"] is True


def test_account_lock_after_five_failures_in_fifteen_minutes(stack):
    """What: five failures against one claimed account across sessions lock that account for 15 minutes (423, even
    with the right PIN, no session lockout ticket); after 15 minutes the right PIN verifies. Unknown account ids
    lock the same way. Use when: always. Inputs: stack. Output: assertions. Side effects: committed rows.
    Role: doc 07 s3.1 account lock."""
    s = stack
    s1, s2 = s.new_session(), s.new_session()
    for _ in range(3):
        s.verify(s1, s.w.a, "0000")
    for _ in range(2):
        s.verify(s2, s.w.a, "0000")
    s3 = s.new_session()
    locked = s.verify(s3, s.w.a, "4821")
    assert locked.status_code == 423 and locked.json()["error"]["code"] == "VERIFY_LOCKED"
    assert locked.json()["error"]["details"]["ticket"] is None
    assert s.claims(s3).json()["failed_verifies"] == 0
    s.clock.set(START + timedelta(minutes=15, seconds=1))
    assert s.verify(s3, s.w.a, "4821").status_code == 200
    s.clock.set(START)
    ghost = "QS-99997"
    for i in range(5):
        s.verify(s.new_session(), ghost, "0000")
    assert s.verify(s.new_session(), ghost, "0000").status_code == 423


def test_pin_never_reaches_audit_logs_or_tables(stack, caplog):
    """What: after a failed and a successful verify the PINs appear in no audit row, no verify_attempts row, no log
    line and no 422 body; the audit rows exist with ok flags and the account id. The test first proves the raw PIN is
    in the request, so a zero is possible. Use when: always. Inputs: stack, caplog. Output: assertions.
    Side effects: committed rows. Role: doc 07 s11 'PIN never stored or logged'."""
    s, sid = stack, stack.new_session()
    wrong, right = "6093", "4821"
    with caplog.at_level(logging.DEBUG):
        r1 = s.verify(sid, s.w.a, wrong)
        r2 = s.verify(sid, s.w.a, right)
        bad = s.verify(sid, s.w.a, wrong + "1")
    assert r1.status_code == 401 and r2.status_code == 200 and bad.status_code == 422
    assert leaks(json.dumps({"account_id": s.w.a, "pin": wrong}), wrong)  # the secret is present in the input
    assert not leaks(caplog.text, wrong) and not leaks(caplog.text, right) and not leaks(bad.text, wrong + "1")
    with psycopg.connect(s.app_url) as c:
        audits = c.execute("SELECT event, actor, account_id, args_redacted::text, session_id::text FROM audit_events "
                           "WHERE session_id = %s::uuid ORDER BY ts", (sid,)).fetchall()
        attempts = c.execute("SELECT claimed_account_id, kind, ok FROM verify_attempts WHERE session_id = %s::uuid "
                             "ORDER BY at", (sid,)).fetchall()
    assert [a[0] for a in audits] == ["verify_failed", "verify_ok"]
    assert all(a[2] in (s.w.a, None) for a in audits)
    dump = json.dumps(audits) + json.dumps(attempts, default=str)
    assert not leaks(dump, wrong) and not leaks(dump, right)
    assert attempts == [(s.w.a, "verify", False), (s.w.a, "verify", True)]


def test_lockout_ticket_is_skipped_when_the_session_already_has_two_unverified_tickets(stack):
    """What: with two unverified tickets already open in the session the third failed verify still locks the
    session (423) but details.ticket is null and no third ticket exists. Use when: always. Inputs: stack.
    Output: assertions. Side effects: committed rows. Role: doc 07 s3.1 'unless the session already has 2'."""
    s, sid = stack, stack.new_session()
    for i in range(2):
        r = s.client.post("/v1/tickets", json={"category": "bug", "summary": f"x{i}", "origin": "model"},
                          headers={"Authorization": f"Bearer {TOKENS['agent_tools']}", "X-Session-Id": sid,
                                   "Idempotency-Key": f"idn-{sid}-{i}"})
        assert r.status_code == 200, r.text
    for _ in range(2):
        s.verify(sid, s.w.a, "0000")
    r = s.verify(sid, s.w.a, "0000")
    assert r.status_code == 423 and r.json()["error"]["details"]["ticket"] is None
    with psycopg.connect(s.app_url) as c:
        assert count_tickets(c, sid) == 2


def test_expired_claims_are_unverified_and_can_be_replaced(stack):
    """What: after the 120 minute lifetime GET claims says verified false, and a new verify (even as another
    person) succeeds instead of 409. Use when: always. Inputs: stack. Output: assertions. Side effects: committed rows.
    Role: expired claims are no identity."""
    s, sid = stack, stack.new_session()
    assert s.verify(sid, s.w.a, "4821").status_code == 200
    s.clock.set(START + timedelta(minutes=121))
    assert s.claims(sid).json()["verified"] is False
    r = s.verify(sid, s.w.b, "1357")
    assert r.status_code == 200 and r.json()["account_id"] == s.w.b
    s.clock.set(START)


def test_unknown_session_claims_is_unverified_with_full_attempts(stack):
    """What: GET claims for a session core has never seen is verified false, not locked, 0 failures, 3 left.
    Use when: always. Inputs: stack. Output: assertions. Side effects: none. Role: doc 07 s3.1 'none (unknown
    session returns verified: false)'."""
    r = stack.claims(str(uuid.uuid4()))
    assert r.status_code == 200 and r.json() == {"verified": False, "verify_locked": False, "failed_verifies": 0,
                                                 "attempts_left": 3}


def test_pin_check_uses_a_constant_time_compare_for_known_and_unknown_accounts(stack, monkeypatch):
    """What: every verify attempt, for a real account and for an unknown one, goes through hmac.compare_digest.
    Use when: always. Inputs: stack, monkeypatch. Output: assertions. Side effects: committed rows.
    Role: doc 09 'PIN compared via HMAC (constant time)'; an unknown account runs a dummy compare, so timing agrees."""
    import core.identity as identity

    calls: list[int] = []
    real = identity.hmac.compare_digest

    def spy(a, b):
        """What: count and delegate. Inputs: two byte strings. Output: bool."""
        calls.append(1)
        return real(a, b)

    monkeypatch.setattr(identity.hmac, "compare_digest", spy)
    s = stack
    s.verify(s.new_session(), s.w.a, "0000")
    known = len(calls)
    s.verify(s.new_session(), "QS-99996", "0000")
    assert known >= 1 and len(calls) > known


def test_concurrent_wrong_pins_lock_once_and_open_one_ticket(stack):
    """What: six parallel wrong-PIN requests in one session end with exactly 3 counted failures, the session locked
    and exactly one lockout ticket. Use when: always. Inputs: stack. Output: assertions. Side effects: committed rows.
    Role: the per-session advisory lock (a lost update would let a fourth guess through)."""
    s, sid = stack, stack.new_session()
    with ThreadPoolExecutor(6) as pool:
        codes = list(pool.map(lambda _: s.verify(sid, s.w.a, "0000").status_code, range(6)))
    assert sorted(codes) == [401, 401, 423, 423, 423, 423]
    c = s.claims(sid).json()
    assert c["failed_verifies"] == 3 and c["verify_locked"] is True
    with psycopg.connect(s.app_url) as conn:
        assert count_tickets(conn, sid) == 1
