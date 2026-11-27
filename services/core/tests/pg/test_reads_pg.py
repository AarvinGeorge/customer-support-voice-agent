"""What: Postgres-backed tests of GET /v1/me/context, /v1/me/charges and /v1/me/tickets over HTTP.
Use when: changing core/reads.py or core/reads_routes.py. Inputs: the Compose Postgres (marker pg).
Output: pytest results. Side effects: committed rows under random ids, removed with the owner role at teardown.
Role: CS-E-03.S13.T3 Done-when: a member gets 403 ROLE_NOT_PERMITTED for an existing and a missing charge alike;
context returns only the open pending action. Sessions are written straight into session_claims (verify is S1.T1)."""
from __future__ import annotations

import random
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg.types.json import Jsonb

from core.app import create_app
from core.clock import FixedClock
from core.config import Settings
from tests.conftest import TOKENS

pytestmark = pytest.mark.pg
NY = ZoneInfo("America/New_York")
POLICY_PATH = Path(__file__).resolve().parents[4] / "data" / "policy.yaml"
START = datetime(2026, 11, 27, 9, 30, tzinfo=NY)
PUBLIC = {"readback_text": "I can refund $96.00 for the charge on 22 November (invoice ending 0412). Shall I go ahead?",
          "readback_say": "I can refund ninety-six dollars. Shall I go ahead?", "amount_cents": 9600,
          "title": "Refund $96.00 for the charge on 22 November", "invoice_ref": "invoice ending 0412",
          "charge_date": "2026-11-22"}


def tid() -> str:
    """What: a random ticket id TCK-26dddddd. Use when: inserting tickets. Inputs: none. Output: str.
    Side effects: none. Role: unique test ids."""
    return f"TCK-26{random.randrange(10**6):06d}"


def build_world(c: psycopg.Connection) -> SimpleNamespace:
    """What: insert accounts A and B, users, charges, history, tickets, pending actions and session claims.
    Use when: the `world` fixture runs. Inputs: a connection (the caller owns the transaction). Output: namespace of ids.
    Side effects: many INSERTs. Role: one data set that exercises role filters, the 90-day cut and pending states."""
    n = random.randrange(10000, 40000)
    w = SimpleNamespace(a=f"QS-{n}", b=f"QS-{n + 40000}", p=f"{random.randrange(10**4):04d}")
    sfx = random.randrange(100, 900)
    w.owner, w.member, w.other = f"U-{sfx}1", f"U-{sfx}2", f"U-{sfx}3"
    c.execute("INSERT INTO accounts (id, name, tier, contract_status, seats, billing_email) "
              "VALUES (%s,'Acme','pro','active',5,'priya@acme.com'),(%s,'Globex','business','active',50,'g@globex.com')",
              (w.a, w.b))
    for uid, acct, name, role, prefs in ((w.owner, w.a, "Priya", "owner", {"weekly_digest": False}),
                                         (w.member, w.a, "Sam", "member", {}), (w.other, w.b, "Gina", "owner", {})):
        c.execute("INSERT INTO users (id, account_id, display_name, role, pin_hmac, notification_prefs) "
                  "VALUES (%s,%s,%s,%s,%s,%s)", (uid, acct, name, role, uuid.uuid4().bytes, Jsonb(prefs)))

    def charge(acct: str, ending: str, days_ago: int, hhmm: str, cents: int, *, invoice: bool = True, status="paid"):
        """What: insert one charge with the given last four digits. Inputs: account, ending, age, time, cents.
        Output: charge id. Side effects: one INSERT."""
        cid = f"CH-{random.randrange(10**4):04d}{ending}"
        hh, mm = (int(x) for x in hhmm.split(":"))
        when = datetime.combine((START - timedelta(days=days_ago)).date(), datetime.min.time(), tzinfo=NY).replace(hour=hh, minute=mm)
        c.execute("INSERT INTO charges (id, account_id, invoice_number, charged_at, amount_cents, description, status) "
                  "VALUES (%s,%s,%s,%s,%s,'Pro plan, 12 seats, monthly',%s)",
                  (cid, acct, f"INV-{w.p}{cid[-8:]}" if invoice else None, when, cents, status))
        return cid, when

    w.c411, w.when411 = charge(w.a, "0411", 5, "09:00", 9600)
    w.c412, w.when412 = charge(w.a, "0412", 5, "09:02", 9600)
    w.c355, _ = charge(w.a, "0355", 60, "09:00", 9600, invoice=False)
    w.c100, _ = charge(w.a, "0100", 200, "09:00", 4800)
    w.cb412, _ = charge(w.b, "0412", 5, "09:00", 1000)
    # history: owner refund (kept), member prefs change (kept), a refund 100 days ago (cut), another account's
    for target, acct, user, kind, cents, days in ((f"refund:{w.c412}", w.a, w.owner, "refund", 9600, 3),
                                                   (f"notification_prefs:{w.member}", w.a, w.member, "notification_prefs", None, 2),
                                                   (f"refund:{w.c100}", w.a, w.owner, "refund", 4800, 100),
                                                   (f"refund:{w.cb412}", w.b, w.other, "refund", 1000, 1)):
        at = START - timedelta(days=days)
        c.execute("INSERT INTO action_history (account_id, user_id, action_type, target_key, amount_cents, effective_date, "
                  "committed_at, committed_by) VALUES (%s,%s,%s,%s,%s,%s,%s,'customer')",
                  (acct, user, kind, target, cents, at.date(), at))
    w.s_owner, w.s_owner2, w.s_member, w.s_anon = (uuid.uuid4() for _ in range(4))
    for sid, user, acct in ((w.s_owner, w.owner, w.a), (w.s_owner2, w.owner, w.a), (w.s_member, w.member, w.a)):
        c.execute("INSERT INTO session_claims (session_id, user_id, account_id, verified_at, expires_at) "
                  "VALUES (%s,%s,%s,%s,%s)", (sid, user, acct, START, START + timedelta(hours=2)))

    def pending(sid, target, state, *, expires_in: timedelta | None, public: bool = True):
        """What: insert one pending action for a session. Inputs: session, target, state, expiry offset.
        Output: its id. Side effects: one INSERT."""
        pid = f"PA-{uuid.uuid4().hex[:8].upper()}"
        c.execute("INSERT INTO pending_actions (id, session_id, account_id, user_id, action_type, pending_kind, target_key, "
                  "params, decision, reason_codes, policy_version, state, prepare_turn_id, requires_step_up, expires_at) "
                  "VALUES (%s,%s,%s,%s,'refund',%s,%s,%s,%s,'{PRO_WITHIN_LIMITS,X}','v1',%s,'t1',false,%s)",
                  (pid, sid, w.a, w.owner, "submit_for_approval" if state == "awaiting_approval" else "commit", target,
                   Jsonb({"public": PUBLIC} if public else {}), "needs_approval" if state == "awaiting_approval" else "allowed",
                   state, START + expires_in if expires_in else None))
        return pid

    # owner session: history of dead pendings plus one open one; owner2 has only an awaiting_approval one
    for st in ("committed", "cancelled", "superseded"):
        pending(w.s_owner, f"refund:{w.c100}", st, expires_in=timedelta(minutes=10))
    w.pa_open = pending(w.s_owner, f"refund:{w.c412}", "prepared", expires_in=timedelta(minutes=10))
    w.pa_approval = pending(w.s_owner2, f"refund:{w.c355}", "awaiting_approval", expires_in=None)
    pending(w.s_member, f"refund:{w.c411}", "awaiting_customer", expires_in=timedelta(minutes=-1))  # expired, not swept

    def ticket(acct, user, stage, session, *, category="billing", pa=None, verified=True, summary="s", created=0):
        """What: insert one ticket. Inputs: account, user, stage, session, category, pending id, flags, age in seconds.
        Output: ticket id. Side effects: one INSERT."""
        t = tid()
        c.execute("INSERT INTO tickets (id, session_id, verified, account_id, user_id, stage, queue, category, priority, "
                  "target_label, summary, pending_action_id, outcome, created_at, created_date, updated_at) "
                  "VALUES (%s,%s,%s,%s,%s,%s,'billing',%s,%s,'24 hours',%s,%s,%s,%s,%s,%s)",
                  (t, session, verified, acct, user, stage, category, "P3" if verified else "P4", summary, pa,
                   "answered" if stage == "resolved" else None, START + timedelta(seconds=created),
                   START.date(), START + timedelta(seconds=created)))
        return t

    w.t_old = ticket(w.a, w.owner, "open", w.s_owner, category="bug", created=0)
    w.t_member = ticket(w.a, w.member, "open", w.s_member, category="sso", created=10)
    w.t_pending = ticket(w.a, w.owner, "pending_approval", w.s_owner2, category="refund", pa=w.pa_approval, created=20)
    w.t_resolved = ticket(w.a, w.owner, "resolved", w.s_owner, category="access", created=30)
    w.t_b = ticket(w.b, w.other, "open", uuid.uuid4(), created=40)
    w.t_anon = ticket(None, None, "open", w.s_anon, category="bug", verified=False, created=50)
    return w


def build_world_atomic(url: str) -> SimpleNamespace:
    """What: build the world in ONE transaction that commits only if every INSERT succeeds. Use when: the world
    fixture runs (a failed build must leave no committed rows, or teardown never runs and later tests collide with the
    leftovers). Inputs: a database url. Output: namespace of ids. Side effects: committed INSERTs on success, none on
    failure (rolled back). Role: fixture setup that cannot leak."""
    with psycopg.connect(url) as c:
        return build_world(c)


@pytest.fixture
def world(app_url, owner_url, migrated):
    """What: a TestClient on the real pool, the committed data set and a get helper; removes every row at teardown.
    Use when: HTTP-level read tests. Inputs: urls. Output: SimpleNamespace with client, get(session, path), ids.
    Side effects: committed rows, removed at teardown. Role: end-to-end harness."""
    settings = Settings(token_agent=TOKENS["agent_tools"], token_commit=TOKENS["agent_commit"], token_ops=TOKENS["ops"],
                        db_url=app_url, demo_faults=False, policy_path=str(POLICY_PATH))
    w = build_world_atomic(app_url)
    with TestClient(create_app(settings, clock=FixedClock(START))) as client:
        def get(session, path):
            """What: GET a read route as the agent for a session. Inputs: session uuid, path with query. Output: response."""
            return client.get(path, headers={"Authorization": f"Bearer {TOKENS['agent_tools']}", "X-Session-Id": str(session),
                                             "X-Mode": "chat"})

        w.get = get
        yield w
    accounts = [w.a, w.b]
    with psycopg.connect(owner_url, autocommit=True) as o:
        ids = [r[0] for r in o.execute("SELECT id FROM tickets WHERE account_id = ANY(%s) OR session_id = %s",
                                       (accounts, w.s_anon)).fetchall()]
        o.execute("DELETE FROM ticket_events WHERE ticket_id = ANY(%s)", (ids,))
        o.execute("DELETE FROM tickets WHERE id = ANY(%s)", (ids,))
        o.execute("DELETE FROM pending_actions WHERE account_id = ANY(%s)", (accounts,))
        o.execute("DELETE FROM action_history WHERE account_id = ANY(%s)", (accounts,))
        o.execute("DELETE FROM charges WHERE account_id = ANY(%s)", (accounts,))
        o.execute("DELETE FROM session_claims WHERE account_id = ANY(%s)", (accounts,))
        o.execute("DELETE FROM users WHERE account_id = ANY(%s)", (accounts,))
        o.execute("DELETE FROM accounts WHERE id = ANY(%s)", (accounts,))


def test_context_for_an_owner(world):
    """What: the owner's context has claims, profile with a masked billing email and own prefs, open and pending
    approval tickets of the whole account newest first (no resolved, no other account), account actions of the last
    90 days with amounts, and the one open pending action. Use when: always. Inputs: world. Output: assertions.
    Side effects: none. Role: doc 07 s3.1 MeContext."""
    r = world.get(world.s_owner, "/v1/me/context")
    assert r.status_code == 200, r.text
    b = r.json()
    assert b["claims"]["user_id"] == world.owner and b["claims"]["role"] == "owner"
    assert b["profile"] == {"account_id": world.a, "account_name": "Acme", "display_name": "Priya", "role": "owner",
                            "tier": "pro", "contract_status": "active", "grace_until": None, "seats": 5,
                            "billing_email_masked": "p***@acme.com", "notification_prefs": {"weekly_digest": False}}
    assert [t["ticket_id"] for t in b["open_tickets"]] == [world.t_pending, world.t_member, world.t_old]
    assert b["open_tickets"][0]["stage"] == "pending_approval"
    assert [(a["action_type"], a["amount_cents"]) for a in b["recent_actions"]] == [("notification_prefs", None), ("refund", 9600)]
    refund = next(a for a in b["recent_actions"] if a["action_type"] == "refund")
    assert refund["label"] == "Refunded $96.00 for charge ending 0412"
    assert refund["effective_date"] == (START - timedelta(days=3)).date().isoformat() and refund["committed_by"] == "customer"
    assert b["generated_at"]


def test_context_returns_only_the_open_pending_action(world):
    """What: with committed, cancelled and superseded pending rows in the history, context returns only the
    prepared one, with every PendingPublic field; a session whose only pending row is awaiting_approval gets
    pending_action null while that ticket shows as pending_approval in open_tickets. Use when: always.
    Inputs: world. Output: assertions. Side effects: none. Role: Done-when 'context returns only the open pending action'."""
    p = world.get(world.s_owner, "/v1/me/context").json()["pending_action"]
    assert p["pending_action_id"] == world.pa_open and p["state"] == "prepared" and p["kind"] == "commit"
    assert (p["action_type"], p["reason_code"], p["requires_step_up"], p["ticket"]) == ("refund", "PRO_WITHIN_LIMITS", False, None)
    assert p["title"] == PUBLIC["title"] and p["readback_text"] == PUBLIC["readback_text"]
    assert p["readback_say"] == PUBLIC["readback_say"] and p["amount_cents"] == 9600
    assert p["invoice_ref"] == "invoice ending 0412" and p["charge_date"] == "2026-11-22"
    assert datetime.fromisoformat(p["expires_at"]) == START + timedelta(minutes=10)
    other = world.get(world.s_owner2, "/v1/me/context").json()
    assert other["pending_action"] is None
    assert any(t["ticket_id"] == world.t_pending and t["stage"] == "pending_approval" for t in other["open_tickets"])


def test_context_for_a_member_is_role_filtered(world):
    """What: a member sees only their own tickets and own actions, no amounts, no billing email, and no pending row
    that has expired. Use when: always. Inputs: world. Output: assertions. Side effects: none.
    Role: doc 07 s3.1 'role-filtered' and s3.2 billing_email_masked billing roles only."""
    b = world.get(world.s_member, "/v1/me/context").json()
    assert b["profile"]["billing_email_masked"] is None and b["profile"]["role"] == "member"
    assert [t["ticket_id"] for t in b["open_tickets"]] == [world.t_member]
    assert [(a["action_type"], a["amount_cents"]) for a in b["recent_actions"]] == [("notification_prefs", None)]
    assert b["pending_action"] is None


def test_context_unverified_is_401(world):
    """What: a session with no claims gets 401 SESSION_NOT_VERIFIED. Use when: always. Inputs: world.
    Output: assertions. Side effects: none. Role: doc 07 s3.1 errors."""
    r = world.get(uuid.uuid4(), "/v1/me/context")
    assert r.status_code == 401 and r.json()["error"]["code"] == "SESSION_NOT_VERIFIED"


def test_member_gets_403_for_an_existing_and_a_missing_charge_alike(world):
    """What: a member asking for a charge that exists (ending 0412), one that does not (9999), or no filter at all
    gets the same 403 ROLE_NOT_PERMITTED answer, so the role check comes before any lookup. Use when: always.
    Inputs: world. Output: assertions. Side effects: none. Role: the S13.T3 Done-when."""
    answers = [world.get(world.s_member, f"/v1/me/charges{q}") for q in ("?ending=0412", "?ending=9999", "")]
    assert [a.status_code for a in answers] == [403, 403, 403]
    bodies = [a.json()["error"] for a in answers]
    for e in bodies:
        assert (e["code"], e["class"], e["retryable"], e["changed"]) == ("ROLE_NOT_PERMITTED", "access_denied", False, False)
    assert bodies[0]["message"] == bodies[1]["message"] and bodies[0]["details"] == bodies[1]["details"]


def test_charges_unverified_is_401(world):
    """What: no claims is 401 SESSION_NOT_VERIFIED (a role cannot be checked without a person). Use when: always.
    Inputs: world. Output: assertions. Side effects: none. Role: doc 07 s3.2."""
    assert world.get(uuid.uuid4(), "/v1/me/charges").status_code == 401


def test_charges_default_window_and_fields(world):
    """What: the default (90 days, limit 10) returns the three recent charges newest first with every ChargeView
    field and not the 200 day old one nor another account's; ambiguous is false without a filter. Use when: always.
    Inputs: world. Output: assertions. Side effects: none. Role: doc 07 s3.2 ChargeView."""
    b = world.get(world.s_owner, "/v1/me/charges").json()
    assert [c["charge_id"] for c in b["charges"]] == [world.c412, world.c411, world.c355] and b["ambiguous"] is False
    first = b["charges"][0]
    assert first["label"] == "invoice ending 0412" and first["amount_cents"] == 9600 and first["currency"] == "USD"
    assert first["status"] == "paid" and first["refunded_at"] is None and first["description"].startswith("Pro plan")
    assert first["charge_date"] == world.when412.date().isoformat()
    assert datetime.fromisoformat(first["charged_at"]) == world.when412
    assert b["charges"][2]["label"] == "charge ending 0355" and b["charges"][2]["invoice_number"] is None


def test_charges_ending_on_since_days_and_limit(world):
    """What: ending narrows to one charge of this account only (the other account's 0412 is ignored); `on` with two
    charges that day is ambiguous true; ending plus on narrows again; since_days widens to the 200 day old charge;
    limit caps the list; no match for ending is 404 NOT_FOUND. Use when: always. Inputs: world. Output: assertions.
    Side effects: none. Role: doc 07 s3.2 resolver."""
    one = world.get(world.s_owner, "/v1/me/charges?ending=0412").json()
    assert [c["charge_id"] for c in one["charges"]] == [world.c412] and one["ambiguous"] is False
    day = world.when412.date().isoformat()
    two = world.get(world.s_owner, f"/v1/me/charges?on={day}").json()
    assert {c["charge_id"] for c in two["charges"]} == {world.c411, world.c412} and two["ambiguous"] is True
    both = world.get(world.s_owner, f"/v1/me/charges?on={day}&ending=0411").json()
    assert [c["charge_id"] for c in both["charges"]] == [world.c411] and both["ambiguous"] is False
    assert world.get(world.s_owner, "/v1/me/charges?ending=0100").status_code == 404
    wide = world.get(world.s_owner, "/v1/me/charges?ending=0100&since_days=300").json()
    assert [c["charge_id"] for c in wide["charges"]] == [world.c100]
    assert len(world.get(world.s_owner, "/v1/me/charges?limit=1").json()["charges"]) == 1
    miss = world.get(world.s_owner, "/v1/me/charges?ending=9999")
    assert miss.status_code == 404 and miss.json()["error"]["code"] == "NOT_FOUND"
    assert world.get(world.s_owner, "/v1/me/charges?since_days=1").json() == {"charges": [], "ambiguous": False}


def test_tickets_by_role_status_and_ending(world):
    """What: an owner sees the account's open and pending tickets (status=open), all four with status=all, one with
    ending; a member sees their own only; an unverified session sees only its own session's tickets; other accounts
    never appear; each item has the TicketView fields. Use when: always. Inputs: world. Output: assertions.
    Side effects: none. Role: doc 07 s3.2 /me/tickets."""
    opened = world.get(world.s_owner, "/v1/me/tickets").json()["tickets"]
    assert [t["ticket_id"] for t in opened] == [world.t_pending, world.t_member, world.t_old]
    everything = world.get(world.s_owner, "/v1/me/tickets?status=all").json()["tickets"]
    assert [t["ticket_id"] for t in everything] == [world.t_resolved, world.t_pending, world.t_member, world.t_old]
    assert everything[0]["outcome"] == "answered" and everything[0]["stage"] == "resolved"
    one = world.get(world.s_owner, f"/v1/me/tickets?status=all&ending={world.t_old[-4:]}").json()["tickets"]
    assert [t["ticket_id"] for t in one] == [world.t_old]
    keys = {"ticket_id", "short_id", "display", "stage", "priority", "queue", "target_label", "verified", "category",
            "summary", "outcome", "outcome_note", "created_at", "updated_at"}
    assert set(one[0]) == keys and one[0]["category"] == "bug" and one[0]["display"] == f"ticket ending {world.t_old[-4:]}"
    member = world.get(world.s_member, "/v1/me/tickets?status=all").json()["tickets"]
    assert [t["ticket_id"] for t in member] == [world.t_member]
    anon = world.get(world.s_anon, "/v1/me/tickets").json()["tickets"]
    assert [t["ticket_id"] for t in anon] == [world.t_anon] and anon[0]["verified"] is False and anon[0]["priority"] == "P4"
    assert world.get(uuid.uuid4(), "/v1/me/tickets").json() == {"tickets": []}


def test_a_failed_world_build_leaves_no_committed_rows(app_url, migrated, monkeypatch):
    """What: force the charge-prefix collision (c412 and cb412 share ending and prefix) midway through the build and
    assert nothing stays committed. Use when: guarding the fixture against leaking rows. Inputs: app_url, migrated,
    monkeypatch. Output: none. Side effects: attempts INSERTs, all rolled back. Role: regression for the leak."""
    real = random.randrange
    monkeypatch.setattr(random, "randrange",
                        lambda a, b=None: 20000 if (a, b) == (10000, 40000) else 5 if (a, b) == (10**4, None) else real(a, b))
    with pytest.raises(psycopg.errors.UniqueViolation):
        build_world_atomic(app_url)
    with psycopg.connect(app_url) as c:
        left = c.execute("SELECT count(*) FROM accounts WHERE id = ANY(%s)", (["QS-20000", "QS-60000"],)).fetchone()
    assert left == (0,)
