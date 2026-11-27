"""What: Postgres-backed tests of refund prepare (T1) and commit, submit, cancel and status (T5), over HTTP and, for
the lock test, on the engine directly with two connections.
Use when: changing core/actions.py or core/actions_routes.py. Inputs: the Compose Postgres (marker pg).
Output: pytest results. Side effects: committed rows under random ids, removed with the owner role at teardown (audit
rows stay: the table is append-only).
Role: CS-E-03.S2.T1 Done-when (a retried prepare with the same key returns the same id; a past_due prepare returns
declined, PAST_DUE and exactly one ticket) and CS-E-03.S2.T5 Done-when (two concurrent commits give one action_history
row; submit creates exactly one approval ticket; cancel creates none)."""
from __future__ import annotations

import random
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import LiteralString
from zoneinfo import ZoneInfo

import psycopg
import pytest
from contracts.policy import load_policy
from fastapi.testclient import TestClient

from core.actions import commit_pending
from core.app import create_app
from core.clock import FixedClock
from core.config import Settings
from core.errors import CoreError
from tests.conftest import TOKENS

pytestmark = pytest.mark.pg
NY = ZoneInfo("America/New_York")
POLICY_PATH = Path(__file__).resolve().parents[4] / "data" / "policy.yaml"
START = datetime(2026, 11, 27, 9, 30, tzinfo=NY)


def build_world(c: psycopg.Connection) -> SimpleNamespace:
    """What: insert a Pro account (owner, member), a Business account, a past-due Pro account, their charges and
    verified sessions. Use when: the world fixture runs. Inputs: an autocommit connection. Output: namespace of ids.
    Side effects: INSERTs. Role: one data set for every prepare and commit path."""
    n = random.randrange(10000, 30000)
    w = SimpleNamespace(pro=f"QS-{n}", biz=f"QS-{n + 30000}", due=f"QS-{n + 60000}")
    sfx = random.randrange(100, 900)
    w.owner, w.member, w.biz_owner, w.due_owner = (f"U-{sfx}{i}" for i in range(1, 5))
    for acct, tier, status in ((w.pro, "pro", "active"), (w.biz, "business", "active"), (w.due, "pro", "past_due")):
        c.execute("INSERT INTO accounts (id, name, tier, contract_status, seats) VALUES (%s,'Acme',%s,%s,12)",
                  (acct, tier, status))
    for uid, acct, role in ((w.owner, w.pro, "owner"), (w.member, w.pro, "member"), (w.biz_owner, w.biz, "owner"),
                            (w.due_owner, w.due, "owner")):
        c.execute("INSERT INTO users (id, account_id, display_name, role, pin_hmac) VALUES (%s,%s,'Priya',%s,%s)",
                  (uid, acct, role, uuid.uuid4().bytes))
    pfx = random.randrange(1000, 9990)  # two increments follow: keep the prefix at 4 digits (CH ids are 8 digits)

    def charge(acct: str, ending: str, days_ago: int, cents: int, invoice: str | None = None) -> str:
        """What: insert one paid charge. Use when: building the world. Inputs: account, last four of the id, age in
        days, cents, invoice number. Output: charge id. Side effects: one INSERT. Role: data builder."""
        cid = f"CH-{pfx}{ending}"
        c.execute("INSERT INTO charges (id, account_id, invoice_number, charged_at, amount_cents, description, status) "
                  "VALUES (%s,%s,%s,%s,%s,'Pro plan, 12 seats, monthly','paid')",
                  (cid, acct, invoice, START - timedelta(days=days_ago), cents))
        return cid

    w.c1 = charge(w.pro, "0412", 6, 9600, invoice=f"INV-{pfx}-0412")
    w.c2 = charge(w.pro, "7771", 6, 9600)
    w.c3 = charge(w.pro, "7772", 6, 9600)
    pfx += 1
    w.c4 = charge(w.pro, "7771", 7, 4800)        # same ending as c2: an ambiguous reference
    pfx += 1
    w.cbiz = charge(w.biz, "0500", 10, 192000)
    w.cdue = charge(w.due, "0600", 3, 9600)
    w.s_owner, w.s_owner2, w.s_member, w.s_biz, w.s_due, w.s_anon = (uuid.uuid4() for _ in range(6))
    for sid, user, acct in ((w.s_owner, w.owner, w.pro), (w.s_owner2, w.owner, w.pro), (w.s_member, w.member, w.pro),
                            (w.s_biz, w.biz_owner, w.biz), (w.s_due, w.due_owner, w.due)):
        c.execute("INSERT INTO session_claims (session_id, user_id, account_id, verified_at, expires_at) "
                  "VALUES (%s,%s,%s,%s,%s)", (sid, user, acct, START, START + timedelta(hours=2)))
    return w


@pytest.fixture
def world(request, app_url, owner_url, migrated):
    """What: a TestClient on the real pool with a shared FixedClock, the data set and call helpers; removes every row
    at teardown. Use when: action tests (DEMO_FAULTS_ENABLED false; a test turns it on with
    `@pytest.mark.parametrize("world", [True], indirect=True)`). Inputs: urls. Output: namespace with client, clock, prepare, act, status.
    Side effects: committed rows, removed at teardown. Role: end-to-end harness."""
    settings = Settings(token_agent=TOKENS["agent_tools"], token_commit=TOKENS["agent_commit"], token_ops=TOKENS["ops"],
                        db_url=app_url, demo_faults=bool(getattr(request, "param", False)), policy_path=str(POLICY_PATH))
    with psycopg.connect(app_url, autocommit=True) as c:
        w = build_world(c)
    w.clock = FixedClock(START)
    w.app_url = app_url
    with TestClient(create_app(settings, clock=w.clock)) as client:
        def headers(token: str, session, turn: str | None, key: str | None, fault: str | None = None) -> dict[str, str]:
            """What: request headers for one call. Use when: any call. Inputs: token name, session, turn id,
            idempotency key. Output: dict. Side effects: none. Role: helper."""
            h = {"Authorization": f"Bearer {TOKENS[token]}", "X-Session-Id": str(session), "X-Mode": "chat"}
            if turn:
                h["X-Turn-Id"] = turn
            if key:
                h["Idempotency-Key"] = key
            if fault:
                h["X-Demo-Fault"] = fault
            return h

        def prepare(session, charge_ref, *, turn="turn-1", key=None, reason="duplicate", fault=None):
            """What: POST refund prepare as agent_tools. Use when: prepare tests. Inputs: session, charge_ref, turn,
            key, reason. Output: response. Side effects: the prepare. Role: helper."""
            return client.post("/v1/actions/refund/prepare",
                               headers=headers("agent_tools", session, turn, key or f"{session}:{uuid.uuid4()}", fault),
                               json={"charge_ref": charge_ref, "reason": reason, "note": "charged twice"})

        def act(verb, pid, session, *, turn="turn-2", token="agent_commit", fault=None):
            """What: POST commit, submit or cancel as agent_commit. Use when: T5 tests. Inputs: verb, pending id,
            session, turn, token. Output: response. Side effects: the action. Role: helper."""
            key = pid if verb == "commit" else f"{pid}:{verb}"
            return client.post(f"/v1/actions/pending/{pid}/{verb}", headers=headers(token, session, turn, key, fault), json={})

        def status(pid, session, token="agent_tools"):
            """What: GET pending status. Use when: status checks. Inputs: pending id, session, token.
            Output: response. Side effects: none. Role: helper."""
            return client.get(f"/v1/actions/pending/{pid}", headers=headers(token, session, None, None))

        def context(session):
            """What: GET /v1/me/context. Use when: card checks. Inputs: session. Output: response.
            Side effects: none. Role: helper."""
            return client.get("/v1/me/context", headers=headers("agent_tools", session, None, None))

        w.client, w.prepare, w.act, w.status, w.context = client, prepare, act, status, context
        yield w
    accounts = [w.pro, w.biz, w.due]
    with psycopg.connect(owner_url, autocommit=True) as o:
        o.execute("DELETE FROM ticket_events WHERE ticket_id IN (SELECT id FROM tickets WHERE account_id = ANY(%s))",
                  (accounts,))
        o.execute("DELETE FROM tickets WHERE account_id = ANY(%s)", (accounts,))
        o.execute("DELETE FROM action_history WHERE account_id = ANY(%s)", (accounts,))
        o.execute("DELETE FROM pending_actions WHERE account_id = ANY(%s)", (accounts,))
        o.execute("DELETE FROM charges WHERE account_id = ANY(%s)", (accounts,))
        o.execute("DELETE FROM session_claims WHERE account_id = ANY(%s)", (accounts,))
        o.execute("DELETE FROM users WHERE account_id = ANY(%s)", (accounts,))
        o.execute("DELETE FROM accounts WHERE id = ANY(%s)", (accounts,))


def q(w, sql: LiteralString, *args):
    """What: run one read query on a fresh connection. Use when: asserting. Inputs: world, SQL, args.
    Output: all rows. Side effects: none. Role: assertions on committed state."""
    with psycopg.connect(w.app_url) as c:
        return c.execute(sql, args).fetchall()


def err(r) -> str:
    """What: the error code of an envelope response. Use when: asserting errors. Inputs: response. Output: code.
    Side effects: none. Role: test shorthand."""
    return r.json()["error"]["code"]


# ---------------------------------------------------------------- T1 prepare

def test_allowed_prepare_writes_a_prepared_row_with_the_card(world):
    """What: a Pro owner's charge within limits gives allowed, a prepared commit row expiring in 600 s, read-backs
    from policy.yaml, and params.public that MeContext exposes as the confirmation card. Use when: always.
    Inputs: world. Output: assertions. Side effects: rows. Role: doc 07 s3.3 PrepareResult, s7.1 row 1, r4 card."""
    r = world.prepare(world.s_owner, {"id": world.c1})
    assert r.status_code == 200, r.text
    b = r.json()
    assert (b["decision"], b["reason_codes"], b["pending_kind"], b["state"]) == (
        "allowed", ["PRO_WITHIN_LIMITS"], "commit", "prepared")
    assert b["changed"] is False and b["requires_step_up"] is False and b["ticket"] is None and b["message"] is None
    assert b["policy_refs"] == [{"article": "refund-policy", "section": "pro-plan-refunds", "title": "Pro plan refunds"}]
    assert datetime.fromisoformat(b["expires_at"]) == START + timedelta(seconds=600)
    day = (START - timedelta(days=6)).date()
    assert b["readback"]["chat"] == (f"I can refund $96.00 for the charge on {day.day} {day:%B} "
                                     f"(invoice ending 0412). Shall I go ahead?")
    assert b["details"]["charge_id"] == world.c1 and b["details"]["amount_cents"] == 9600
    assert b["policy_version"].startswith("2026-11-27.1+")
    card = world.context(world.s_owner).json()["pending_action"]
    assert card["pending_action_id"] == b["pending_action_id"]
    assert (card["title"], card["invoice_ref"], card["charge_date"], card["reason_code"], card["amount_cents"]) == (
        f"Refund $96.00 for the charge on {day.day} {day:%B}", "invoice ending 0412", day.isoformat(),
        "PRO_WITHIN_LIMITS", 9600)
    assert card["readback_text"] == b["readback"]["chat"] and card["readback_say"] == b["readback"]["voice"]
    assert b["card"] == card  # the read-back turn gets the same card MeContext shows on the next turn


def test_retried_prepare_with_the_same_key_returns_the_same_id(world):
    """What: the same Idempotency-Key twice returns the same PrepareResult and writes one row. Use when: always.
    Inputs: world. Output: assertions. Side effects: rows. Role: T1 Done-when part 1."""
    key = f"{world.s_owner}:call-1"
    first = world.prepare(world.s_owner, {"id": world.c1}, key=key)
    again = world.prepare(world.s_owner, {"id": world.c1}, key=key)
    assert first.status_code == again.status_code == 200
    assert first.json()["pending_action_id"] == again.json()["pending_action_id"]
    assert first.json() == again.json()
    assert q(world, "SELECT count(*) FROM pending_actions WHERE session_id = %s", world.s_owner)[0][0] == 1


def test_past_due_prepare_declines_and_opens_exactly_one_billing_ticket(world):
    """What: a past_due account gets declined PAST_DUE with the policy message and a billing follow-up ticket; a
    second prepare (new key) reuses that ticket, so the account has exactly one. Use when: always. Inputs: world.
    Output: assertions. Side effects: one ticket. Role: T1 Done-when part 2; doc 07 s3.4 owners row 2."""
    r = world.prepare(world.s_due, {"id": world.cdue})
    assert r.status_code == 200, r.text
    b = r.json()
    assert (b["decision"], b["reason_codes"], b["pending_action_id"], b["state"]) == ("declined", ["PAST_DUE"], None, None)
    assert b["message"].startswith("Refunds can't be issued while the account has an unpaid balance.")
    assert b["ticket"]["queue"] == "billing" and b["ticket"]["stage"] == "open" and b["ticket"]["verified"] is True
    assert b["card"] is None
    again = world.prepare(world.s_due, {"id": world.cdue}).json()
    assert again["ticket"]["ticket_id"] == b["ticket"]["ticket_id"]
    rows = q(world, "SELECT id, category, origin, stage FROM tickets WHERE account_id = %s", world.due)
    assert rows == [(b["ticket"]["ticket_id"], "billing", "core", "open")]
    assert q(world, "SELECT count(*) FROM pending_actions WHERE account_id = %s", world.due)[0][0] == 0


def test_new_prepare_supersedes_the_open_row_even_when_declined(world):
    """What: a second prepare in the session marks the first row superseded and names it in details; a declined
    prepare also supersedes. Use when: always. Inputs: world. Output: assertions. Side effects: rows.
    Role: doc 07 s3.3 supersede rule, s7.1 row 6."""
    first = world.prepare(world.s_owner, {"id": world.c1}).json()
    second = world.prepare(world.s_owner, {"id": world.c3}).json()
    assert second["details"]["superseded_pending_action_id"] == first["pending_action_id"]
    q_state = "SELECT state FROM pending_actions WHERE id = %s"
    assert q(world, q_state, first["pending_action_id"])[0][0] == "superseded"
    # the charge c2 is refunded out of band: the next prepare on it is declined and still kills the open card
    with psycopg.connect(world.app_url, autocommit=True) as c:
        c.execute("UPDATE charges SET status = 'refunded', refunded_at = now() WHERE id = %s", (world.c2,))
    third = world.prepare(world.s_owner, {"id": world.c2}).json()
    assert third["decision"] == "declined" and third["reason_codes"] == ["ALREADY_REFUNDED"]
    assert third["details"]["superseded_pending_action_id"] == second["pending_action_id"]
    assert q(world, q_state, second["pending_action_id"])[0][0] == "superseded"


def test_charge_ref_resolution(world):
    """What: an ending that matches two charges is 409 AMBIGUOUS_REFERENCE with both candidates; a null ref for a
    billing role is 409 with the recent charges; an unknown id is 404; an ending plus date that matches one works.
    Use when: always. Inputs: world. Output: assertions. Side effects: rows. Role: doc 07 s3.3 prepare errors."""
    r = world.prepare(world.s_owner, {"ending": "7771"})
    assert r.status_code == 409 and err(r) == "AMBIGUOUS_REFERENCE"
    assert {c["charge_id"] for c in r.json()["error"]["details"]["candidates"]} == {world.c2, world.c4}
    r = world.prepare(world.s_owner, None)
    assert r.status_code == 409 and err(r) == "AMBIGUOUS_REFERENCE"
    assert {c["charge_id"] for c in r.json()["error"]["details"]["candidates"]} == {world.c1, world.c2, world.c3, world.c4}
    r = world.prepare(world.s_owner, {"id": "CH-99999999"})
    assert r.status_code == 404 and err(r) == "NOT_FOUND"
    r = world.prepare(world.s_owner, {"id": world.cbiz})  # another account's charge is not found
    assert r.status_code == 404 and err(r) == "NOT_FOUND"
    on = (START - timedelta(days=7)).date().isoformat()
    r = world.prepare(world.s_owner, {"ending": "7771", "on": on})
    assert r.status_code == 200 and r.json()["details"]["charge_id"] == world.c4
    assert q(world, "SELECT count(*) FROM tickets WHERE account_id = %s", world.pro)[0][0] == 0


def test_member_is_declined_before_any_lookup(world):
    """What: a member gets 200 declined ROLE_NOT_PERMITTED, even for a reference that does not exist.
    Use when: always. Inputs: world. Output: assertions. Side effects: none. Role: role first (draft C s2.4)."""
    for ref in ({"id": world.c1}, {"id": "CH-99999999"}, None):
        r = world.prepare(world.s_member, ref)
        assert r.status_code == 200, r.text
        assert (r.json()["decision"], r.json()["reason_codes"]) == ("declined", ["ROLE_NOT_PERMITTED"])


def test_business_prepare_waits_for_the_customer(world):
    """What: a Business refund inside 30 days is needs_approval, awaiting_customer, submit_for_approval, with the
    approval read-back, and no ticket yet. Use when: always. Inputs: world. Output: assertions. Side effects: rows.
    Role: doc 07 s7.1 row 2 (no ticket yet), R-3."""
    b = world.prepare(world.s_biz, {"id": world.cbiz}).json()
    assert (b["decision"], b["state"], b["pending_kind"], b["reason_codes"]) == (
        "needs_approval", "awaiting_customer", "submit_for_approval", ["BUSINESS_APPROVAL_REQUIRED"])
    assert b["readback"]["chat"].endswith("Shall I send this to our team for approval?")
    assert (b["card"]["kind"], b["card"]["state"], b["card"]["reason_code"], b["card"]["amount_cents"]) == (
        "submit_for_approval", "awaiting_customer", "BUSINESS_APPROVAL_REQUIRED", 192000)
    assert b["card"]["title"].startswith("Refund $1,920.00 for the charge on ")
    assert q(world, "SELECT count(*) FROM tickets WHERE account_id = %s", world.biz)[0][0] == 0


def test_second_live_action_on_a_target_is_in_progress(world):
    """What: a prepare on a charge another session holds open is 409 ACTION_IN_PROGRESS and changes nothing.
    Use when: always. Inputs: world. Output: assertions. Side effects: rows. Role: doc 07 s3.3, s7.1 unique index."""
    first = world.prepare(world.s_owner, {"id": world.c1}).json()
    r = world.prepare(world.s_owner2, {"id": world.c1})
    assert r.status_code == 409 and err(r) == "ACTION_IN_PROGRESS"
    assert q(world, "SELECT state FROM pending_actions WHERE id = %s", first["pending_action_id"])[0][0] == "prepared"


def test_stale_rows_are_expired_by_the_next_prepare(world):
    """What: an open row past expires_at is flipped to expired (not superseded) by the next prepare, which may then
    take the same target. Use when: always. Inputs: world. Output: assertions. Side effects: rows.
    Role: doc 07 s3.3 step 1, s7.1 row 7."""
    first = world.prepare(world.s_owner, {"id": world.c1}).json()
    world.clock.set(START + timedelta(seconds=600))
    second = world.prepare(world.s_owner2, {"id": world.c1})
    assert second.status_code == 200, second.text
    assert q(world, "SELECT state FROM pending_actions WHERE id = %s", first["pending_action_id"])[0][0] == "expired"


def test_prepare_needs_a_verified_session_a_turn_and_a_key(world):
    """What: an unverified session is 401; a missing X-Turn-Id or Idempotency-Key is 422. Use when: always.
    Inputs: world. Output: assertions. Side effects: none. Role: doc 07 s1.2 headers."""
    r = world.prepare(world.s_anon, {"id": world.c1})
    assert r.status_code == 401 and err(r) == "SESSION_NOT_VERIFIED"
    r = world.prepare(world.s_owner, {"id": world.c1}, turn=None)
    assert r.status_code == 422
    r = world.client.post("/v1/actions/refund/prepare", json={"charge_ref": None, "reason": "other", "note": ""},
                          headers={"Authorization": f"Bearer {TOKENS['agent_tools']}", "X-Session-Id": str(world.s_owner),
                                   "X-Turn-Id": "turn-1"})
    assert r.status_code == 422


def test_grace_prepare_is_disabled(world):
    """What: grace extension prepare answers 404 ACTION_DISABLED while policy has it switched off.
    Use when: always. Inputs: world. Output: assertion. Side effects: none. Role: doc 07 s3.3 grace row."""
    r = world.client.post("/v1/actions/grace_extension/prepare", json={},
                          headers={"Authorization": f"Bearer {TOKENS['agent_tools']}", "X-Session-Id": str(world.s_due),
                                   "X-Turn-Id": "turn-1", "Idempotency-Key": "k"})
    assert r.status_code == 404 and err(r) == "ACTION_DISABLED"


# ---------------------------------------------------------------- T5 commit, submit, cancel, status

def test_commit_needs_a_later_turn_then_refunds_once(world):
    """What: commit in the prepare turn is 409 CONFIRMATION_REQUIRED; in a later turn it refunds the charge, writes one
    action_history row and stores the result; a repeat replays it; status reads committed with the result.
    Use when: always. Inputs: world. Output: assertions. Side effects: rows. Role: doc 07 s3.3 commit, s7.1 row 3."""
    pid = world.prepare(world.s_owner, {"id": world.c1}, turn="turn-1").json()["pending_action_id"]
    r = world.act("commit", pid, world.s_owner, turn="turn-1")
    assert r.status_code == 409 and err(r) == "CONFIRMATION_REQUIRED"
    r = world.act("commit", pid, world.s_owner, turn=None)
    assert r.status_code == 422
    r = world.act("commit", pid, world.s_owner, turn="turn-2")
    assert r.status_code == 200, r.text
    b = r.json()
    assert (b["status"], b["pending_action_id"], b["replayed"], b["changed"]) == ("committed", pid, False, True)
    assert b["summary"].startswith("Refunded $96.00 for invoice ending 0412.")
    assert b["details"]["charge_id"] == world.c1 and b["details"]["amount_cents"] == 9600
    again = world.act("commit", pid, world.s_owner, turn="turn-3").json()
    assert again["replayed"] is True and again["summary"] == b["summary"]
    assert q(world, "SELECT count(*) FROM action_history WHERE pending_action_id = %s", pid)[0][0] == 1
    assert q(world, "SELECT status FROM charges WHERE id = %s", world.c1)[0][0] == "refunded"
    s = world.status(pid, world.s_owner).json()
    assert (s["state"], s["decision"], s["result"]["summary"]) == ("committed", "allowed", b["summary"])
    assert world.context(world.s_owner).json()["pending_action"] is None


def test_only_the_commit_token_may_commit_submit_or_cancel(world):
    """What: agent_tools gets 403 SCOPE_DENIED on commit, submit and cancel; the row stays prepared.
    Use when: always. Inputs: world. Output: assertions. Side effects: rows. Role: doc 07 s1.4 least privilege."""
    pid = world.prepare(world.s_owner, {"id": world.c1}).json()["pending_action_id"]
    for verb in ("commit", "submit", "cancel"):
        r = world.act(verb, pid, world.s_owner, token="agent_tools")
        assert r.status_code == 403 and err(r) == "SCOPE_DENIED", verb
    assert q(world, "SELECT state FROM pending_actions WHERE id = %s", pid)[0][0] == "prepared"


def test_two_concurrent_commits_give_one_history_row_engine_level(world):
    """What: commit A holds the row lock in an open transaction; commit B on a second connection blocks on the pending
    row's FOR UPDATE (checked in pg_stat_activity, not only by B being alive: B would also block later on the charge
    lock); after A commits, B returns the stored result with replayed true. One action_history row.
    Use when: always. Inputs: world. Output: assertions. Side effects: rows. Role: T5 Done-when part 1 (real lock)."""
    pid = world.prepare(world.s_owner, {"id": world.c1}).json()["pending_action_id"]
    policy = load_policy(POLICY_PATH)
    args = {"policy": policy, "clock": world.clock, "pending_id": pid, "session_id": str(world.s_owner),
            "turn_id": "turn-2", "trace_id": None, "mode": "chat"}
    out: dict[str, object] = {}
    with psycopg.connect(world.app_url) as a:
        first = commit_pending(a, **args)
        assert not isinstance(first, CoreError) and first.replayed is False

        def second() -> None:
            """What: run commit B on its own connection. Use when: the lock test. Inputs: none.
            Output: stores the result in out. Side effects: commit B's transaction. Role: the waiting commit."""
            with psycopg.connect(world.app_url) as b:
                out["b"] = commit_pending(b, **args)
                b.commit()

        t = threading.Thread(target=second)
        t.start()
        time.sleep(0.5)
        assert t.is_alive(), "commit B did not wait for the row lock"
        waiting = q(world, "SELECT query FROM pg_stat_activity WHERE wait_event_type = 'Lock' "
                           "AND query LIKE %s", "%FROM pending_actions WHERE id = %")
        assert len(waiting) == 1 and "FOR UPDATE" in waiting[0][0], f"commit B is not waiting on the pending row: {waiting}"
        a.commit()
    t.join(timeout=10)
    res = out["b"]
    assert not isinstance(res, CoreError) and res.replayed is True  # pyright: ignore[reportAttributeAccessIssue]
    assert q(world, "SELECT count(*) FROM action_history WHERE target_key = %s", f"refund:{world.c1}")[0][0] == 1


def test_repeated_commits_over_http_replay(world):
    """What: eight commits of one pending action fired from eight threads over HTTP: all answer 200, exactly one is not
    a replay, one action_history row. Use when: always. Inputs: world. Output: assertions. Side effects: rows.
    Role: the replay path through the real route and pool. It overlaps only by timing, so it is not the lock proof:
    the engine-level test above is (see the fix round 1 mutation runs in the lane A report)."""
    pid = world.prepare(world.s_owner, {"id": world.c1}).json()["pending_action_id"]
    barrier = threading.Barrier(8)

    def go(i: int):
        """What: wait for all, then commit. Use when: the threaded test. Inputs: index. Output: response.
        Side effects: one commit call. Role: worker."""
        barrier.wait()
        return world.act("commit", pid, world.s_owner, turn=f"turn-{i + 2}")

    with ThreadPoolExecutor(8) as pool:
        results = list(pool.map(go, range(8)))
    assert [r.status_code for r in results] == [200] * 8, [r.text for r in results]
    assert sorted(r.json()["replayed"] for r in results) == [False] + [True] * 7
    assert q(world, "SELECT count(*) FROM action_history WHERE pending_action_id = %s", pid)[0][0] == 1


def test_commit_after_expiry_is_410_and_the_row_reads_expired(world):
    """What: at expires_at the commit is 410 PENDING_EXPIRED and the row is flipped to expired (kept after the
    error). Use when: always. Inputs: world. Output: assertions. Side effects: rows. Role: doc 07 s7.1 row 7."""
    pid = world.prepare(world.s_owner, {"id": world.c1}).json()["pending_action_id"]
    world.clock.set(START + timedelta(seconds=599))
    assert world.status(pid, world.s_owner).json()["state"] == "prepared"
    world.clock.set(START + timedelta(seconds=600))
    assert world.status(pid, world.s_owner).json()["state"] == "expired"
    r = world.act("commit", pid, world.s_owner)
    assert r.status_code == 410 and err(r) == "PENDING_EXPIRED"
    assert q(world, "SELECT state FROM pending_actions WHERE id = %s", pid)[0][0] == "expired"
    assert q(world, "SELECT count(*) FROM action_history WHERE pending_action_id = %s", pid)[0][0] == 0


def test_commit_rechecks_rules_on_live_rows(world):
    """What: the account goes past_due after the read-back: commit is 409 RULES_CHANGED with the new codes, the row is
    rules_changed and nothing is refunded. Use when: always. Inputs: world. Output: assertions. Side effects: rows.
    Role: doc 07 s3.3 re-evaluate, s7.1 row 8."""
    pid = world.prepare(world.s_owner, {"id": world.c1}).json()["pending_action_id"]
    with psycopg.connect(world.app_url, autocommit=True) as c:
        c.execute("UPDATE accounts SET contract_status = 'past_due' WHERE id = %s", (world.pro,))
    r = world.act("commit", pid, world.s_owner)
    assert r.status_code == 409 and err(r) == "RULES_CHANGED"
    d = r.json()["error"]["details"]
    assert d["reason_codes"] == ["PAST_DUE"] and d["policy_refs"][0]["section"] == "past-due-accounts" and d["message"]
    assert q(world, "SELECT state FROM pending_actions WHERE id = %s", pid)[0][0] == "rules_changed"
    assert q(world, "SELECT status FROM charges WHERE id = %s", world.c1)[0][0] == "paid"
    r = world.act("commit", pid, world.s_owner, turn="turn-3")
    assert r.status_code == 409 and err(r) == "PENDING_NOT_ACTIVE"


def test_submit_creates_exactly_one_approval_ticket(world):
    """What: commit on an approval-path row is 409 PENDING_NOT_ACTIVE; submit in the same turn is 409
    CONFIRMATION_REQUIRED; in a later turn it creates one Pending approval ticket in the approvals queue and sets
    awaiting_approval with no expiry; a repeat replays with the same ticket; context then shows the ticket and no
    open pending action. Use when: always. Inputs: world. Output: assertions. Side effects: rows.
    Role: T5 Done-when part 2; doc 07 s7.1 row 4, s3.4 owners row 3."""
    pid = world.prepare(world.s_biz, {"id": world.cbiz}, turn="turn-1").json()["pending_action_id"]
    r = world.act("commit", pid, world.s_biz)
    assert r.status_code == 409 and err(r) == "PENDING_NOT_ACTIVE"
    r = world.act("submit", pid, world.s_biz, turn="turn-1")
    assert r.status_code == 409 and err(r) == "CONFIRMATION_REQUIRED"
    r = world.act("submit", pid, world.s_biz, turn="turn-2")
    assert r.status_code == 200, r.text
    b = r.json()
    assert (b["status"], b["replayed"], b["changed"]) == ("awaiting_approval", False, True)
    assert (b["ticket"]["stage"], b["ticket"]["queue"]) == ("pending_approval", "approvals")
    assert b["ticket"]["display"] in b["summary"]
    again = world.act("submit", pid, world.s_biz, turn="turn-3").json()
    assert again["replayed"] is True and again["ticket"]["ticket_id"] == b["ticket"]["ticket_id"]
    rows = q(world, "SELECT id, stage, queue, category, pending_action_id FROM tickets WHERE account_id = %s", world.biz)
    assert rows == [(b["ticket"]["ticket_id"], "pending_approval", "approvals", "refund", pid)]
    assert q(world, "SELECT state, expires_at, ticket_id FROM pending_actions WHERE id = %s", pid)[0] == (
        "awaiting_approval", None, b["ticket"]["ticket_id"])
    s = world.status(pid, world.s_biz, token="agent_commit").json()
    assert s["state"] == "awaiting_approval" and s["ticket"]["ticket_id"] == b["ticket"]["ticket_id"]
    ctx = world.context(world.s_biz).json()
    assert ctx["pending_action"] is None and ctx["open_tickets"][0]["ticket_id"] == b["ticket"]["ticket_id"]
    assert q(world, "SELECT count(*) FROM action_history WHERE account_id = %s", world.biz)[0][0] == 0


def test_cancel_creates_no_ticket(world):
    """What: cancel of an awaiting_customer row sets cancelled with changed false and creates no ticket; a repeat
    cancel answers the same; submit and commit afterwards are 409 PENDING_NOT_ACTIVE. Use when: always.
    Inputs: world. Output: assertions. Side effects: rows. Role: T5 Done-when part 3; doc 07 s7.1 row 5, R-3."""
    pid = world.prepare(world.s_biz, {"id": world.cbiz}).json()["pending_action_id"]
    r = world.act("cancel", pid, world.s_biz)
    assert r.status_code == 200, r.text
    assert r.json() == {"pending_action_id": pid, "state": "cancelled", "changed": False}
    assert world.act("cancel", pid, world.s_biz).json()["state"] == "cancelled"
    for verb in ("submit", "commit"):
        r = world.act(verb, pid, world.s_biz, turn="turn-3")
        assert r.status_code == 409 and err(r) == "PENDING_NOT_ACTIVE", verb
    assert q(world, "SELECT count(*) FROM tickets WHERE account_id = %s", world.biz)[0][0] == 0
    assert world.status(pid, world.s_biz).json()["state"] == "cancelled"


def test_pending_rows_are_private_to_their_session(world):
    """What: status, commit and cancel from another session are 404 and change nothing; an unknown id is 404.
    Use when: always. Inputs: world. Output: assertions. Side effects: rows. Role: the person comes only from the
    session (doc 07 s1.2 X-Session-Id)."""
    pid = world.prepare(world.s_owner, {"id": world.c1}).json()["pending_action_id"]
    assert world.status(pid, world.s_owner2).status_code == 404
    assert world.act("commit", pid, world.s_owner2).status_code == 404
    assert world.act("cancel", pid, world.s_owner2).status_code == 404
    assert world.status("PA-00000000", world.s_owner).status_code == 404
    assert q(world, "SELECT state FROM pending_actions WHERE id = %s", pid)[0][0] == "prepared"


# ---------------------------------------------------------------- CS-E-07.S1.T1 demo tool_error fault

def _fault_audit(w, session) -> int:
    """What: count fault_injected audit rows of a session. Use when: fault tests. Inputs: world, session id.
    Output: count. Side effects: none. Role: assertion helper (audit rows are append-only and stay)."""
    return q(w, "SELECT count(*) FROM audit_events WHERE session_id = %s AND event = 'fault_injected'", session)[0][0]


@pytest.mark.parametrize("world", [True], indirect=True)
def test_faulted_commit_leaves_the_row_prepared(world):
    """What: flag on: a commit with X-Demo-Fault tool_error is 503 TOOL_ERROR (fault_injected, changed false) before any
    transaction; the row stays `prepared`, no action_history row, the charge is untouched, one fault_injected audit row
    exists; the same commit without the header then succeeds. Use when: always. Inputs: world (flag on).
    Output: assertions. Side effects: rows. Role: CS-E-07.S1.T1 Done-when, doc 07 s10 (S5 script: fault on the yes turn)."""
    pid = world.prepare(world.s_owner, {"id": world.c1}, turn="turn-1").json()["pending_action_id"]
    r = world.act("commit", pid, world.s_owner, turn="turn-2", fault="tool_error")
    assert r.status_code == 503, r.text
    e = r.json()["error"]
    assert (e["code"], e["retryable"], e["changed"], e["fault_injected"]) == ("TOOL_ERROR", True, False, True)
    assert q(world, "SELECT state FROM pending_actions WHERE id = %s", pid)[0][0] == "prepared"
    assert q(world, "SELECT count(*) FROM action_history WHERE pending_action_id = %s", pid)[0][0] == 0
    assert q(world, "SELECT status FROM charges WHERE id = %s", world.c1)[0][0] == "paid"
    assert _fault_audit(world, world.s_owner) == 1
    assert world.status(pid, world.s_owner).json()["state"] == "prepared"  # reads ignore the fault
    ok = world.act("commit", pid, world.s_owner, turn="turn-2", fault="tool_error")  # the retry is faulted too
    assert ok.status_code == 503 and _fault_audit(world, world.s_owner) == 2
    done = world.act("commit", pid, world.s_owner, turn="turn-2")
    assert done.status_code == 200 and done.json()["status"] == "committed"


@pytest.mark.parametrize("world", [True], indirect=True)
def test_faulted_prepare_creates_no_pending_row(world):
    """What: flag on: a faulted prepare is 503 TOOL_ERROR, creates no pending row and no stored idempotency result, and
    writes one fault_injected audit row; the same key without the header then prepares normally. Use when: always.
    Inputs: world (flag on). Output: assertions. Side effects: rows. Role: doc 07 s10 (S5 script: fault on the prepare turn)."""
    before = q(world, "SELECT count(*) FROM pending_actions WHERE account_id = %s", world.pro)[0][0]
    r = world.prepare(world.s_owner, {"id": world.c1}, key="fault-key-1", fault="tool_error")
    assert r.status_code == 503 and r.json()["error"]["fault_injected"] is True
    assert q(world, "SELECT count(*) FROM pending_actions WHERE account_id = %s", world.pro)[0][0] == before
    assert q(world, "SELECT count(*) FROM idempotency WHERE key = %s", "fault-key-1")[0][0] == 0
    assert _fault_audit(world, world.s_owner) == 1
    ok = world.prepare(world.s_owner, {"id": world.c1}, key="fault-key-1")
    assert ok.status_code == 200 and ok.json()["state"] == "prepared"


def test_flag_off_ignores_the_header_on_commit(world):
    """What: flag off: the same faulted commit succeeds and writes no fault_injected audit row. Use when: always.
    Inputs: world (flag off, the default). Output: assertions. Side effects: rows.
    Role: CS-E-07.S1.T1 Done-when second half (the logging half is in tests/test_faults.py)."""
    pid = world.prepare(world.s_owner, {"id": world.c1}, turn="turn-1", fault="tool_error").json()["pending_action_id"]
    r = world.act("commit", pid, world.s_owner, turn="turn-2", fault="tool_error")
    assert r.status_code == 200 and r.json()["status"] == "committed"
    assert _fault_audit(world, world.s_owner) == 0


# ---------------------------------------------------------------- fix round 1 (review PR #37, D-055)

def audit_rows(w, pid: str | None, event: str, session=None):
    """What: the args of audit rows of one event for a pending id (or a session). Use when: refusal checks.
    Inputs: world, pending id or None, event name, optional session. Output: list of args dicts. Side effects: none.
    Role: assertions on the audit trail."""
    if pid is not None:
        return [r[0] for r in q(w, "SELECT args_redacted FROM audit_events WHERE pending_action_id = %s AND event = %s "
                                   "ORDER BY ts", pid, event)]
    return [r[0] for r in q(w, "SELECT args_redacted FROM audit_events WHERE session_id = %s AND event = %s ORDER BY ts",
                            session, event)]


def _set_claims(w, session, sql: LiteralString) -> None:
    """What: change the session's claims row. Use when: lapsing or locking a session. Inputs: world, session, an
    UPDATE with one %s for the session id. Output: none. Side effects: one committed UPDATE. Role: test control."""
    with psycopg.connect(w.app_url, autocommit=True) as c:
        c.execute(sql, (session,))


def test_commit_after_verification_lapsed_is_401_and_the_row_stays_prepared(world):
    """What: verification lapses between prepare and commit: commit is 401 SESSION_NOT_VERIFIED, the row stays
    prepared, nothing is refunded, and a refusal audit row names the code; after re-verification the same row
    commits. Use when: always. Inputs: world. Output: assertions. Side effects: rows. Role: D-055, review I1."""
    pid = world.prepare(world.s_owner, {"id": world.c1}).json()["pending_action_id"]
    _set_claims(world, world.s_owner, "UPDATE session_claims SET expires_at = '2026-11-27 09:00-05' WHERE session_id = %s")
    r = world.act("commit", pid, world.s_owner)
    assert r.status_code == 401 and err(r) == "SESSION_NOT_VERIFIED", r.text
    assert r.json()["error"]["changed"] is False
    assert q(world, "SELECT state FROM pending_actions WHERE id = %s", pid)[0][0] == "prepared"
    assert q(world, "SELECT status FROM charges WHERE id = %s", world.c1)[0][0] == "paid"
    assert [a["code"] for a in audit_rows(world, pid, "action_commit_refused")] == ["SESSION_NOT_VERIFIED"]
    _set_claims(world, world.s_owner, "UPDATE session_claims SET expires_at = '2026-11-27 11:30-05' WHERE session_id = %s")
    assert world.act("commit", pid, world.s_owner, turn="turn-3").status_code == 200


def test_submit_on_a_locked_session_is_401_and_opens_no_ticket(world):
    """What: a session locked after prepare cannot submit: 401 SESSION_NOT_VERIFIED, row awaiting_customer, no
    ticket, refusal audited. Use when: always. Inputs: world. Output: assertions. Side effects: rows. Role: D-055."""
    pid = world.prepare(world.s_biz, {"id": world.cbiz}).json()["pending_action_id"]
    _set_claims(world, world.s_biz, "UPDATE session_claims SET verify_locked = true WHERE session_id = %s")
    r = world.act("submit", pid, world.s_biz)
    assert r.status_code == 401 and err(r) == "SESSION_NOT_VERIFIED", r.text
    assert q(world, "SELECT state FROM pending_actions WHERE id = %s", pid)[0][0] == "awaiting_customer"
    assert q(world, "SELECT count(*) FROM tickets WHERE account_id = %s", world.biz)[0][0] == 0
    assert [a["code"] for a in audit_rows(world, pid, "action_submit_refused")] == ["SESSION_NOT_VERIFIED"]


def test_refused_commits_and_prepares_are_audited(world):
    """What: a same-turn commit (409), a commit with the prepare turn in upper case (409) and an ambiguous prepare
    (409) each leave one refusal audit row with the code; the row stays prepared. Use when: always. Inputs: world.
    Output: assertions. Side effects: rows. Role: review M1 and M2 (L1 s5: an audit row for every prepare and commit)."""
    turn = "0197b5a4-7c3e-7f00-8000-00000000000a"
    pid = world.prepare(world.s_owner, {"id": world.c1}, turn=turn).json()["pending_action_id"]
    assert err(world.act("commit", pid, world.s_owner, turn=turn)) == "CONFIRMATION_REQUIRED"
    assert err(world.act("commit", pid, world.s_owner, turn=turn.upper())) == "CONFIRMATION_REQUIRED"
    assert [a["code"] for a in audit_rows(world, pid, "action_commit_refused")] == ["CONFIRMATION_REQUIRED"] * 2
    assert q(world, "SELECT state FROM pending_actions WHERE id = %s", pid)[0][0] == "prepared"
    r = world.prepare(world.s_owner2, {"ending": "7771"})
    assert err(r) == "AMBIGUOUS_REFERENCE"
    assert [a["code"] for a in audit_rows(world, None, "action_prepare_refused", world.s_owner2)] == ["AMBIGUOUS_REFERENCE"]


def test_world_ids_stay_valid_when_every_random_draw_is_at_the_top_of_its_range(app_url, migrated, monkeypatch):
    """What: build the world with every random draw at its maximum and roll it back. Use when: guarding the id
    patterns (CH-8 digits, U-4 digits, QS-5 digits) against overflow of the random prefixes (CI flake: pfx 9998 or
    9999 plus the two increments gave CH-100000500). Inputs: app_url, migrated, monkeypatch. Output: none.
    Side effects: INSERTs inside a transaction that is rolled back. Role: regression for the pg setup flake."""
    monkeypatch.setattr(random, "randrange", lambda a, b=None: (b if b is not None else a) - 1)
    with psycopg.connect(app_url) as c:
        try:
            build_world(c)
        finally:
            c.rollback()
