"""What: Postgres-backed tests of ticket create, merge, limits, idempotency, transition and the HTTP route.
Use when: changing core/tickets.py or core/tickets_routes.py. Inputs: the Compose Postgres (marker pg).
Output: pytest results. Side effects: logic tests roll back; HTTP tests commit rows under unique ids and remove them
with the owner role at teardown (audit rows stay: audit_events is append-only by design).
Role: CS-E-03.S3.T1 Done-when: the merge test passes; the sixth verified ticket in a day gets 429 TICKET_LIMIT_REACHED
with `existing`."""
from __future__ import annotations

import itertools
import json
import re
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import psycopg
import pytest
from contracts.core_api import TicketCreate
from contracts.policy import load_policy
from fastapi.testclient import TestClient

from core.app import create_app
from core.clock import FixedClock
from core.config import Settings
from core.errors import CoreError
from core.tickets import create_ticket, insert_ticket, transition

pytestmark = pytest.mark.pg
NY = ZoneInfo("America/New_York")
POLICY_PATH = Path(__file__).resolve().parents[4] / "data" / "policy.yaml"
POLICY = load_policy(POLICY_PATH)
CATEGORIES = ["billing", "refund", "access", "sso", "integration", "bug", "account_change", "commitment"]


_TICK = itertools.count()


def clock() -> FixedClock:
    """What: a clock at 2026-11-27 09:30 New York plus one second per call. Use when: a test needs time.
    Inputs: none. Output: FixedClock. Side effects: advances a counter. Role: deterministic business date for the
    daily limit, with created_at strictly increasing so "most recent ticket" is well defined."""
    return FixedClock(datetime(2026, 11, 27, 9, 30, tzinfo=NY) + timedelta(seconds=next(_TICK)))


class TickingClock:
    """What: a Clock whose now() moves forward one second per call on 2026-11-27 New York. Use when: the app under
    test needs strictly increasing created_at. Inputs: none. Output: now() and today(). Side effects: counter.
    Role: deterministic stand-in for the system clock in HTTP tests."""

    def now(self) -> datetime:
        """What: next instant. Use when: stamping. Inputs: none. Output: aware datetime. Side effects: counter. Role: see class."""
        return clock().now()

    def today(self):
        """What: the fixed business date. Use when: date rules. Inputs: none. Output: date. Side effects: none. Role: see class."""
        return datetime(2026, 11, 27, 9, 30, tzinfo=NY).date()


def body(category: str = "billing", **kw) -> TicketCreate:
    """What: a TicketCreate. Use when: building input. Inputs: category and overrides. Output: model.
    Side effects: none. Role: shorthand."""
    data = {"category": category, "summary": f"summary for {category}", "origin": "model", **kw}
    return TicketCreate.model_validate(data)


class World:
    """What: one committed-or-rolled-back account with an owner user and a verified session.
    Use when: a test needs a verified caller. Inputs: connection, tier. Output: ids as attributes.
    Side effects: INSERTs into accounts, users, session_claims. Role: test data builder."""

    def __init__(self, conn: psycopg.Connection, tier: str = "pro") -> None:
        """What: insert the rows. Use when: constructing. Inputs: connection, tier. Output: none.
        Side effects: three INSERTs. Role: parents for tickets."""
        n = uuid.uuid4().int % 90000 + 10000
        self.account_id, self.user_id = f"QS-{n}", f"U-{uuid.uuid4().int % 9000 + 1000}"
        self.session_id = uuid.uuid4()
        self.conn = conn
        conn.execute("INSERT INTO accounts (id, name, tier, contract_status, seats) VALUES (%s,'Acme',%s,'active',5)",
                     (self.account_id, tier))
        conn.execute("INSERT INTO users (id, account_id, display_name, role, pin_hmac) VALUES (%s,%s,'Priya','owner',%s)",
                     (self.user_id, self.account_id, uuid.uuid4().bytes))
        self.session_id = self.add_session(verified=True)

    def add_session(self, *, verified: bool, expires_in: timedelta = timedelta(hours=2)) -> uuid.UUID:
        """What: insert a session_claims row. Use when: another session on the same account or an unverified one.
        Inputs: verified flag, lifetime relative to the fixed clock. Output: the session uuid.
        Side effects: one INSERT. Role: claims fixture."""
        sid = uuid.uuid4()
        now = clock().now()
        if verified:
            self.conn.execute("INSERT INTO session_claims (session_id, user_id, account_id, verified_at, expires_at) "
                              "VALUES (%s,%s,%s,%s,%s)", (sid, self.user_id, self.account_id, now, now + expires_in))
        else:
            self.conn.execute("INSERT INTO session_claims (session_id, failed_verifies, verify_locked) VALUES (%s,3,true)",
                              (sid,))
        return sid


def make(conn, session, category="billing", clk=None, **kw):
    """What: call create_ticket with defaults. Use when: a test creates a ticket. Inputs: connection, session uuid,
    category, optional clock, TicketCreate overrides. Output: TicketOutcome. Side effects: SQL. Role: shorthand."""
    return create_ticket(conn, policy=POLICY, clock=clk or clock(), session_id=session, body=body(category, **kw),
                         actor="agent")


def rows(conn, sql, *args):
    """What: fetch all rows of a query. Use when: asserting stored state. Inputs: connection, SQL, args.
    Output: list of tuples. Side effects: one SELECT. Role: shorthand."""
    return conn.execute(sql, args).fetchall()


def test_verified_create_stores_row_event_and_audit(app_conn):
    """What: a verified create gives queue and priority from policy, a TCK id, a created event and an audit row.
    Use when: always. Inputs: app_conn. Output: assertions. Side effects: rolled back. Role: doc 07 s3.4, s7.2."""
    w = World(app_conn, "pro")
    out = make(app_conn, w.session_id, "refund")
    assert out.created and not out.merged
    ref = out.ref
    assert re.fullmatch(r"TCK-26[0-9]{6}", ref.ticket_id) and ref.short_id == ref.ticket_id[-4:]
    assert (ref.stage, ref.queue, ref.priority, ref.target_label, ref.verified) == ("open", "billing", "P3", "24 hours", True)
    assert ref.display == f"ticket ending {ref.short_id}"
    (t,) = rows(app_conn, "SELECT account_id, user_id, session_id, created_date, origin, summary FROM tickets WHERE id=%s",
                ref.ticket_id)
    assert t[:3] == (w.account_id, w.user_id, w.session_id) and str(t[3]) == "2026-11-27" and t[4] == "model"
    (ev,) = rows(app_conn, "SELECT kind, to_stage, actor, actor_id FROM ticket_events WHERE ticket_id=%s", ref.ticket_id)
    assert ev == ("created", "open", "agent", w.user_id)
    (au,) = rows(app_conn, "SELECT event, ticket_id, account_id, actor FROM audit_events WHERE ticket_id=%s", ref.ticket_id)
    assert au == ("ticket_create", ref.ticket_id, w.account_id, "agent")


@pytest.mark.parametrize("tier,blocking,priority,label", [("enterprise", True, "P1", "1 hour"),
                                                           ("enterprise", False, "P2", "4 hours"),
                                                           ("business", True, "P2", "8 hours"),
                                                           ("free", False, "P4", "72 hours")])
def test_priority_and_label_by_account_tier(app_conn, tier, blocking, priority, label):
    """What: the account's tier (read from accounts, never from the caller) sets priority and target label.
    Use when: always. Inputs: app_conn. Output: assertions. Side effects: rolled back. Role: L1 s5 A3."""
    w = World(app_conn, tier)
    ref = make(app_conn, w.session_id, "bug", blocking=blocking).ref
    assert (ref.priority, ref.target_label, ref.queue) == (priority, label, "technical")


def test_merge_into_open_ticket_of_same_account_and_category(app_conn):
    """What: a second verified create in the same category adds a merged event to the open ticket and creates no
    row; a different category, a resolved ticket and another account do not merge. Use when: always.
    Inputs: app_conn. Output: assertions. Side effects: rolled back. Role: THE merge test (Done-when)."""
    w = World(app_conn)
    first = make(app_conn, w.session_id, "billing")
    other_session = w.add_session(verified=True)  # same person, new session: merge is per account
    second = make(app_conn, other_session, "billing", summary="Still charged twice")
    assert (second.created, second.merged) == (False, True)
    assert second.ref.ticket_id == first.ref.ticket_id
    assert rows(app_conn, "SELECT count(*) FROM tickets WHERE account_id=%s", w.account_id)[0][0] == 1
    kinds = [r[0] for r in rows(app_conn, "SELECT kind FROM ticket_events WHERE ticket_id=%s ORDER BY at, kind",
                                first.ref.ticket_id)]
    assert sorted(kinds) == ["created", "merged"]
    (note,) = rows(app_conn, "SELECT body FROM ticket_events WHERE kind='merged' AND ticket_id=%s", first.ref.ticket_id)
    assert "Still charged twice" in note[0]
    # different category: new ticket
    assert make(app_conn, w.session_id, "bug").created
    # another account, same category: new ticket
    w2 = World(app_conn)
    assert make(app_conn, w2.session_id, "billing").created
    # a resolved ticket is not an open ticket: next one is new
    transition(app_conn, policy=POLICY, clock=clock(), ticket_id=first.ref.ticket_id, to_stage="resolved",
               actor="operator", actor_id="op-1", outcome="resolved_by_operator", note="done")
    third = make(app_conn, w.session_id, "billing")
    assert third.created and third.ref.ticket_id != first.ref.ticket_id


def test_sixth_verified_ticket_in_a_day_is_limited_with_existing(app_conn):
    """What: five distinct-category tickets succeed; the sixth raises TICKET_LIMIT_REACHED carrying the most recent
    ticket of the day as `existing`; a merge still works at the limit; the next business day is allowed.
    Use when: always. Inputs: app_conn. Output: assertions. Side effects: rolled back. Role: Done-when, L1 s5 A3."""
    w = World(app_conn)
    made = [make(app_conn, w.session_id, c).ref for c in CATEGORIES[:5]]
    assert len({r.ticket_id for r in made}) == 5
    with pytest.raises(CoreError) as exc:
        make(app_conn, w.session_id, CATEGORIES[5])
    assert exc.value.code == "TICKET_LIMIT_REACHED"
    existing = exc.value.details["existing"]
    assert existing["ticket_id"] == made[-1].ticket_id and existing["stage"] == "open"
    assert rows(app_conn, "SELECT count(*) FROM tickets WHERE account_id=%s", w.account_id)[0][0] == 5
    assert make(app_conn, w.session_id, CATEGORIES[0]).merged  # no new ticket, so no limit
    tomorrow = FixedClock(clock().now() + timedelta(days=1))
    assert make(app_conn, w.session_id, CATEGORIES[5], clk=tomorrow).created


def test_unverified_session_rules(app_conn):
    """What: an unverified session (no claims row, or a locked one) gets P4, no account or user link, no merge, and
    is limited to 2 per session with `existing`; another session is independent. Use when: always.
    Inputs: app_conn. Output: assertions. Side effects: rolled back. Role: L1 s5 A3 unverified rule."""
    anon = uuid.uuid4()  # no session_claims row at all
    a = make(app_conn, anon, "sso")
    b = make(app_conn, anon, "sso")  # same category: still a new ticket
    assert a.created and b.created and a.ref.ticket_id != b.ref.ticket_id
    assert (a.ref.priority, a.ref.queue, a.ref.target_label, a.ref.verified) == ("P4", "technical", "72 hours", False)
    (r,) = rows(app_conn, "SELECT account_id, user_id, verified FROM tickets WHERE id=%s", a.ref.ticket_id)
    assert r == (None, None, False)
    with pytest.raises(CoreError) as exc:
        make(app_conn, anon, "bug")
    assert exc.value.code == "TICKET_LIMIT_REACHED" and exc.value.details["existing"]["ticket_id"] == b.ref.ticket_id
    assert make(app_conn, uuid.uuid4(), "bug").created
    w = World(app_conn, "enterprise")  # a locked, unverified session on a real account stays unverified
    locked = w.add_session(verified=False)
    ref = make(app_conn, locked, "access", blocking=True).ref
    assert (ref.priority, ref.verified) == ("P4", False)


def test_expired_claims_are_unverified(app_conn):
    """What: a session whose claims have expired is treated as unverified. Use when: always. Inputs: app_conn.
    Output: assertions. Side effects: rolled back. Role: session_ttl_minutes, doc 07 s3.1."""
    w = World(app_conn, "enterprise")
    old = w.add_session(verified=True, expires_in=timedelta(minutes=-1))
    ref = make(app_conn, old, "bug").ref
    assert (ref.verified, ref.priority) == (False, "P4")


def test_ticket_id_collision_is_retried(app_conn):
    """What: when the random id already exists the insert retries with a new one. Use when: always.
    Inputs: app_conn. Output: assertions. Side effects: rolled back. Role: ids are random, so a clash must not be a 500."""
    w = World(app_conn)
    seq = iter([555555, 555555, 666666])
    first = create_ticket(app_conn, policy=POLICY, clock=clock(), session_id=w.session_id, body=body("bug"),
                          actor="agent", rng=lambda n: next(seq))
    second = create_ticket(app_conn, policy=POLICY, clock=clock(), session_id=w.session_id, body=body("sso"),
                           actor="agent", rng=lambda n: next(seq))
    assert (first.ref.ticket_id, second.ref.ticket_id) == ("TCK-26555555", "TCK-26666666")


def test_transition_writes_events_audit_and_rejects_bad_moves(app_conn):
    """What: open to resolved to closed writes stage_changed events, sets outcome and audit rows; closed to open
    raises INVALID_TRANSITION and changes nothing. Use when: always. Inputs: app_conn. Output: assertions.
    Side effects: rolled back. Role: doc 07 s7.2 'every change writes ticket_events and audit_events'."""
    w = World(app_conn)
    tid = make(app_conn, w.session_id, "bug").ref.ticket_id
    ref = transition(app_conn, policy=POLICY, clock=clock(), ticket_id=tid, to_stage="resolved", actor="operator",
                     actor_id="op-1", outcome="resolved_by_operator", note="fixed")
    assert ref.stage == "resolved"
    ref = transition(app_conn, policy=POLICY, clock=clock(), ticket_id=tid, to_stage="closed", actor="operator",
                     actor_id="op-1")
    assert ref.stage == "closed"
    (t,) = rows(app_conn, "SELECT stage, outcome, outcome_note FROM tickets WHERE id=%s", tid)
    assert t == ("closed", "resolved_by_operator", "fixed")
    ev = rows(app_conn, "SELECT from_stage, to_stage, actor FROM ticket_events WHERE kind='stage_changed' AND ticket_id=%s "
              "ORDER BY at, to_stage", tid)
    assert sorted(ev) == [("open", "resolved", "operator"), ("resolved", "closed", "operator")]
    assert rows(app_conn, "SELECT count(*) FROM audit_events WHERE ticket_id=%s AND event='ticket_transition'", tid)[0][0] == 2
    with pytest.raises(CoreError) as exc:
        transition(app_conn, policy=POLICY, clock=clock(), ticket_id=tid, to_stage="open", actor="operator", actor_id="op-1")
    assert exc.value.code == "INVALID_TRANSITION"
    assert rows(app_conn, "SELECT stage FROM tickets WHERE id=%s", tid)[0][0] == "closed"
    with pytest.raises(CoreError) as nf:
        transition(app_conn, policy=POLICY, clock=clock(), ticket_id="TCK-26000000", to_stage="closed", actor="operator",
                   actor_id="op-1")
    assert nf.value.code == "NOT_FOUND"


def test_pending_approval_ticket_resolves_with_outcome(app_conn, seed):
    """What: a pending_approval ticket (the stage submit creates) is written by insert_ticket in the approvals queue
    with its pending action, and transitions to resolved with outcome approved_executed.
    Use when: always. Inputs: app_conn, seed. Output: assertions. Side effects: rolled back.
    Role: the Pending approval stage the slice creates (07 s7.2), used by CS-E-03.S2.T5."""
    pid = seed.pending(uuid.uuid4(), f"refund:{seed.charge_id}", state="awaiting_approval")
    ref = insert_ticket(app_conn, policy=POLICY, clock=clock(), session_id=uuid.uuid4(), account_id=seed.account_id,
                        user_id=seed.user_id, tier="pro", stage="pending_approval", queue="approvals",
                        category="refund", summary="Refund request", origin="core", pending_action_id=pid,
                        actor="system", actor_id=None)
    assert (ref.stage, ref.queue, ref.verified) == ("pending_approval", "approvals", True)
    out = transition(app_conn, policy=POLICY, clock=clock(), ticket_id=ref.ticket_id, to_stage="resolved",
                     actor="operator", actor_id="op-1", outcome="approved_executed")
    assert out.stage == "resolved"
    assert rows(app_conn, "SELECT outcome FROM tickets WHERE id=%s", ref.ticket_id)[0][0] == "approved_executed"


# ---------------------------------------------------------------- HTTP, committed rows, owner cleanup

TOKENS = {"agent_tools": "tok-agent", "agent_commit": "tok-commit", "ops": "tok-ops"}


@pytest.fixture
def http(app_url, owner_url, migrated):
    """What: a TestClient on the real pool plus a committed World, and an owner-role teardown that deletes this
    test's tickets, events, claims, user, account and idempotency rows. Use when: HTTP-level pg tests.
    Inputs: urls. Output: (client, world, headers fn). Side effects: committed rows, removed at teardown.
    Role: end-to-end check of POST /v1/tickets through auth, idempotency and the pool."""
    settings = Settings(token_agent=TOKENS["agent_tools"], token_commit=TOKENS["agent_commit"], token_ops=TOKENS["ops"],
                        db_url=app_url, demo_faults=False, policy_path=str(POLICY_PATH))
    prefix = f"t-{uuid.uuid4().hex[:8]}"
    with psycopg.connect(app_url, autocommit=True) as c:
        w = World(c, "pro")
        anon_sessions: list[uuid.UUID] = []
        with TestClient(create_app(settings, clock=TickingClock())) as client:
            def post(payload, *, key, session=None):
                """What: POST /v1/tickets as the agent. Inputs: json payload, key suffix, session uuid. Output: response."""
                headers = {"Authorization": f"Bearer {TOKENS['agent_tools']}", "X-Session-Id": str(session or w.session_id),
                           "Idempotency-Key": f"{prefix}:{key}"}
                if session:
                    anon_sessions.append(session)
                return client.post("/v1/tickets", headers=headers, json=payload)

            yield client, w, post, prefix
    with psycopg.connect(owner_url, autocommit=True) as o:
        sessions = [w.session_id, *anon_sessions]
        ids = [r[0] for r in o.execute("SELECT id FROM tickets WHERE account_id=%s OR session_id = ANY(%s)",
                                       (w.account_id, sessions)).fetchall()]
        o.execute("DELETE FROM ticket_events WHERE ticket_id = ANY(%s)", (ids,))
        o.execute("DELETE FROM tickets WHERE id = ANY(%s)", (ids,))
        o.execute("DELETE FROM idempotency WHERE key LIKE %s", (prefix + ":%",))
        o.execute("DELETE FROM session_claims WHERE account_id=%s", (w.account_id,))
        o.execute("DELETE FROM users WHERE account_id=%s", (w.account_id,))
        o.execute("DELETE FROM accounts WHERE id=%s", (w.account_id,))


def payload(category="billing", **kw):
    """What: a request body dict. Inputs: category, overrides. Output: dict. Role: shorthand."""
    return {"category": category, "summary": f"summary for {category}", "origin": "model", **kw}


def test_http_create_then_merge(http):
    """What: POST creates (200, created true) and a second POST in the same category merges (merged true, same id).
    Use when: always. Inputs: http fixture. Output: assertions. Side effects: committed rows, removed. Role: s3.4."""
    _client, _w, post, _ = http
    r1 = post(payload("billing"), key="a")
    assert r1.status_code == 200 and (r1.json()["created"], r1.json()["merged"]) == (True, False)
    r2 = post(payload("billing"), key="b")
    assert (r2.json()["created"], r2.json()["merged"]) == (False, True)
    assert r2.json()["ticket"]["ticket_id"] == r1.json()["ticket"]["ticket_id"]


def test_http_sixth_ticket_is_429_with_existing(http):
    """What: five distinct categories return 200, the sixth returns 429 TICKET_LIMIT_REACHED in the envelope with
    details.existing set and Retry-After absent (not retryable). Use when: always. Inputs: http fixture.
    Output: assertions. Side effects: committed rows, removed. Role: Done-when over the wire."""
    _client, _w, post, _ = http
    last: dict = {}
    for i, c in enumerate(CATEGORIES[:5]):
        r = post(payload(c), key=f"c{i}")
        assert r.status_code == 200, r.text
        last = r.json()["ticket"]
    r = post(payload(CATEGORIES[5]), key="c5")
    assert r.status_code == 429
    err = r.json()["error"]
    assert (err["code"], err["class"], err["retryable"], err["changed"]) == ("TICKET_LIMIT_REACHED", "rate_limited", False, False)
    assert err["details"]["existing"]["ticket_id"] == last["ticket_id"]


def test_http_idempotency_replay_and_key_reuse(http, owner_url):
    """What: the same key and body returns the same response with one ticket row; the same key with a different
    body is 422 idempotency_key_reused. Use when: always. Inputs: http fixture. Output: assertions.
    Side effects: committed rows, removed. Role: doc 07 s3.4 Idempotency-Key."""
    _client, w, post, _ = http
    first = post(payload("sso"), key="same")
    again = post(payload("sso"), key="same")
    assert first.status_code == again.status_code == 200 and first.json() == again.json()
    with psycopg.connect(owner_url) as o:
        assert rows(o, "SELECT count(*) FROM tickets WHERE account_id=%s", w.account_id)[0][0] == 1
        assert rows(o, "SELECT count(*) FROM ticket_events WHERE ticket_id=%s",
                    first.json()["ticket"]["ticket_id"])[0][0] == 1
    clash = post(payload("sso", summary="different"), key="same")
    assert clash.status_code == 422 and clash.json()["error"]["details"]["reason"] == "idempotency_key_reused"


def test_http_unverified_session_limit(http):
    """What: an anonymous session gets P4 with no account link, two tickets, then 429 with existing.
    Use when: always. Inputs: http fixture. Output: assertions. Side effects: committed rows, removed.
    Role: L1 s5 A3 unverified limit."""
    _c, _w, post, _ = http
    anon = uuid.uuid4()
    r = post(payload("bug"), key="u1", session=anon)
    assert r.status_code == 200 and r.json()["ticket"]["priority"] == "P4" and r.json()["ticket"]["verified"] is False
    assert post(payload("bug"), key="u2", session=anon).json()["created"] is True
    r3 = post(payload("bug"), key="u3", session=anon)
    assert r3.status_code == 429 and r3.json()["error"]["details"]["existing"] is not None


def test_http_concurrent_creates_respect_the_daily_limit(http, owner_url):
    """What: six distinct-category requests sent at once leave exactly five tickets and one 429, never six.
    Use when: always. Inputs: http fixture. Output: assertions. Side effects: committed rows, removed.
    Role: the limit is checked under a per-account lock, not by a racy count."""
    _client, w, post, _ = http
    with ThreadPoolExecutor(max_workers=6) as pool:
        codes = sorted(pool.map(lambda i: post(payload(CATEGORIES[i]), key=f"p{i}").status_code, range(6)))
    assert codes == [200, 200, 200, 200, 200, 429]
    with psycopg.connect(owner_url) as o:
        assert rows(o, "SELECT count(*) FROM tickets WHERE account_id=%s", w.account_id)[0][0] == 5


def test_http_response_has_no_raw_error_for_bad_json(http):
    """What: a body that is not the TicketCreate shape is 422 with the envelope. Use when: always.
    Inputs: http fixture. Output: assertions. Side effects: none. Role: no 500s on bad input."""
    client, w, _post, prefix = http
    r = client.post("/v1/tickets", content=json.dumps({"category": "x"}),
                    headers={"Authorization": f"Bearer {TOKENS['agent_tools']}", "X-Session-Id": str(w.session_id),
                             "Idempotency-Key": f"{prefix}:bad", "Content-Type": "application/json"})
    assert r.status_code == 422 and r.json()["error"]["code"] == "VALIDATION_ERROR"


# ---------------------------------------------------------------- fix round 1 (review PR #28)

def _approval_ticket(conn, w: World):
    """What: insert one Pending approval ticket for a World's account. Use when: a test needs an approval ticket.
    Inputs: connection, world. Output: TicketRef. Side effects: a pending_actions row and the ticket rows.
    Role: the core-created ticket that doc 07 s8.1 says is not counted toward the daily limit."""
    from types import SimpleNamespace

    from tests.pg.conftest import Seed
    holder = SimpleNamespace(account_id=w.account_id, user_id=w.user_id, conn=conn)
    pid = Seed.pending(holder, uuid.uuid4(), f"refund:CH-{uuid.uuid4().int % 90000000 + 10000000}",  # type: ignore[arg-type]
                       state="awaiting_approval")
    return insert_ticket(conn, policy=POLICY, clock=clock(), session_id=uuid.uuid4(), account_id=w.account_id,
                         user_id=w.user_id, tier="pro", stage="pending_approval", queue="approvals", category="refund",
                         summary="Refund request", origin="core", pending_action_id=pid, actor="system", actor_id=None)


def test_approval_tickets_do_not_count_toward_the_daily_limit(app_conn):
    """What: with two Pending approval tickets already today, five customer tickets are still created and the
    sixth is limited. Use when: always. Inputs: app_conn. Output: assertions. Side effects: rolled back.
    Role: doc 07 s8.1 'merges and approvals not counted' (review I1)."""
    w = World(app_conn)
    _approval_ticket(app_conn, w)
    _approval_ticket(app_conn, w)
    for c in CATEGORIES[:5]:
        assert make(app_conn, w.session_id, c).created
    with pytest.raises(CoreError) as exc:
        make(app_conn, w.session_id, CATEGORIES[5])
    assert exc.value.code == "TICKET_LIMIT_REACHED"
    assert exc.value.details["existing"]["stage"] == "open"  # existing is a counted (customer) ticket


def test_daily_limit_follows_the_new_york_business_day(app_conn):
    """What: five tickets at 18:00 New York, a sixth at 19:30 New York (00:30 UTC next day) is refused and the
    stored created_date is the New York date; at 00:01 New York next day it is created, and that day starts at zero.
    Use when: always. Inputs: app_conn. Output: assertions. Side effects: rolled back.
    Role: review I2 and re-review; kills 'count ignores the date', 'count date from UTC' and 'created_date from UTC'."""
    w = World(app_conn)
    sid = w.add_session(verified=True, expires_in=timedelta(days=2))  # claims must outlive the evening
    evening = datetime(2026, 11, 27, 18, 0, tzinfo=NY)
    # three at 18:00 (same date in New York and UTC), then 19:30 and 21:00 New York, which are already the next date
    # in UTC: only creates in that window can tell a New York created_date from a UTC one
    when = [evening + timedelta(seconds=i) for i in range(3)] + [datetime(2026, 11, 27, 19, 30, tzinfo=NY),
                                                                 datetime(2026, 11, 27, 21, 0, tzinfo=NY)]
    for t, c in zip(when, CATEGORIES[:5], strict=True):
        assert t.astimezone(ZoneInfo("UTC")).date().isoformat() == ("2026-11-28" if t.hour >= 19 else "2026-11-27")
        assert make(app_conn, sid, c, clk=FixedClock(t)).created
    late = FixedClock(datetime(2026, 11, 27, 22, 0, tzinfo=NY))
    assert late.now().astimezone(ZoneInfo("UTC")).date().isoformat() == "2026-11-28"  # UTC is already tomorrow
    with pytest.raises(CoreError) as exc:
        make(app_conn, sid, CATEGORIES[5], clk=late)
    assert exc.value.code == "TICKET_LIMIT_REACHED"
    late_night = FixedClock(datetime(2026, 11, 27, 23, 59, tzinfo=NY))
    with pytest.raises(CoreError):
        make(app_conn, sid, CATEGORIES[5], clk=late_night)
    dates = {str(r[0]) for r in rows(app_conn, "SELECT created_date FROM tickets WHERE account_id=%s", w.account_id)}
    assert dates == {"2026-11-27"}
    next_day = [FixedClock(datetime(2026, 11, 28, 0, 1, tzinfo=NY) + timedelta(seconds=i)) for i in range(6)]
    for i, c in enumerate(CATEGORIES[:5]):  # the day reset: five more are allowed (merge target is closed by category)
        out = make(app_conn, sid, c, clk=next_day[i], summary="next day")
        assert out.merged or out.created
    created_next = rows(app_conn, "SELECT count(*) FROM tickets WHERE account_id=%s AND created_date='2026-11-28'",
                        w.account_id)[0][0]
    assert created_next == 0  # all five merged into the open tickets of the 27th: merges are never limited
    w2 = World(app_conn)
    sid2 = w2.add_session(verified=True, expires_in=timedelta(days=2))
    for c in CATEGORIES[:5]:
        make(app_conn, sid2, c, clk=FixedClock(evening))
    assert make(app_conn, sid2, "other", clk=FixedClock(datetime(2026, 11, 28, 0, 1, tzinfo=NY))).created


def test_merge_writes_a_merge_audit_row(app_conn):
    """What: a merge writes one ticket_merge audit row and no second ticket_create row. Use when: always.
    Inputs: app_conn. Output: assertions. Side effects: rolled back. Role: review M5."""
    w = World(app_conn)
    first = make(app_conn, w.session_id, "sso").ref
    make(app_conn, w.session_id, "sso")
    got = sorted(r[0] for r in rows(app_conn, "SELECT event FROM audit_events WHERE ticket_id=%s", first.ticket_id))
    assert got == ["ticket_create", "ticket_merge"]


@pytest.mark.parametrize("start,outcome", [("open", "approved_executed"), ("open", "rejected"),
                                           ("pending_approval", "answered"), ("pending_approval", "resolved_by_operator"),
                                           ("open", "banana")])
def test_transition_outcome_must_fit_the_starting_stage(app_conn, start, outcome):
    """What: an outcome that does not belong to the starting stage, or is unknown, is VALIDATION_ERROR and nothing
    is written (no database error). Use when: always. Inputs: app_conn. Output: assertions.
    Side effects: rolled back. Role: review M3 (07 s3.5: operator resolve is for open tickets only)."""
    w = World(app_conn)
    tid = (make(app_conn, w.session_id, "bug").ref if start == "open" else _approval_ticket(app_conn, w)).ticket_id
    with pytest.raises(CoreError) as exc:
        transition(app_conn, policy=POLICY, clock=clock(), ticket_id=tid, to_stage="resolved", actor="operator",
                   actor_id="op-1", outcome=outcome)
    assert exc.value.code == "VALIDATION_ERROR"
    assert rows(app_conn, "SELECT stage FROM tickets WHERE id=%s", tid)[0][0] == start
    assert rows(app_conn, "SELECT count(*) FROM ticket_events WHERE kind='stage_changed' AND ticket_id=%s", tid)[0][0] == 0


def test_http_concurrent_same_category_creates_merge_into_one_ticket(http, owner_url):
    """What: six simultaneous requests in one category leave exactly one ticket (one created, five merged).
    Use when: always. Inputs: http fixture. Output: assertions. Side effects: committed rows, removed.
    Role: review M5; the per-account lock serialises find-or-create."""
    _client, w, post, _ = http
    with ThreadPoolExecutor(max_workers=6) as pool:
        out = list(pool.map(lambda i: post(payload("bug"), key=f"m{i}").json(), range(6)))
    assert sorted((o["created"], o["merged"]) for o in out) == [(False, True)] * 5 + [(True, False)]
    with psycopg.connect(owner_url) as o:
        assert rows(o, "SELECT count(*) FROM tickets WHERE account_id=%s", w.account_id)[0][0] == 1
