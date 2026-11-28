"""What: Postgres-backed tests of write_audit (rows really hold no PIN) and run_idempotent (replay, mismatch, race).
Use when: changing core/audit.py or core/idempotency.py. Inputs: the Compose Postgres (marker pg).
Output: pytest results. Side effects: the race test commits and then deletes its own idempotency rows; the rest roll back.
Role: CS-E-03.S13.T2 Done-when (PIN in fixture, absent from audit rows) and the idempotency helper."""
import threading
import uuid
from datetime import datetime
from zoneinfo import ZoneInfo

import psycopg
import pytest
from psycopg.types.json import Jsonb  # noqa: F401  (documents that responses are stored as jsonb)

from core.audit import AuditEvent, write_audit
from core.clock import FixedClock
from core.errors import CoreError
from core.idempotency import run_idempotent
from tests.pin_probe import leaks

pytestmark = pytest.mark.pg
CLOCK = FixedClock(datetime(2026, 11, 27, 9, 30, tzinfo=ZoneInfo("America/New_York")))
PIN = "4821"


def test_pin_is_in_the_fixture_and_absent_from_the_stored_audit_row(app_conn):
    """What: write_audit with a PIN-bearing fixture stores a row; scanning every column as text finds no PIN
    and no full email. Use when: always. Inputs: app_conn. Output: assertions. Side effects: rolled back.
    Role: the Done-when against the real table, not a fake."""
    session = uuid.UUID("00000000-0000-4821-8000-000000000000")  # a UUID group that is exactly the PIN digits
    args = {"account_id": "QS-10423", "pin": PIN, "nested": {"step_up_pin": PIN, "email": "natasha@acme.com"}}
    assert PIN in str(args)  # the secret is really in the fixture
    audit_id = write_audit(app_conn, AuditEvent(event="verify", actor="agent", session_id=session,
                                                mode="chat", args=args), clock=CLOCK)
    row_text = app_conn.execute("SELECT t::text FROM audit_events t WHERE id = %s", (audit_id,)).fetchone()[0]
    assert "[PIN]" in row_text and "n***@acme.com" in row_text  # the row exists and was redacted
    assert not leaks(row_text, PIN) and "natasha@acme.com" not in row_text


def test_free_text_pin_in_audit_args_is_absent_from_the_stored_row(app_conn):
    """What: a PIN typed into free text (a ticket summary carrying the customer's words, "QS-10423 4821") is
    redacted by the s11.1 scanner inside redact before the audit row is stored; the account id is kept.
    Use when: always. Inputs: app_conn. Output: assertions. Side effects: rolled back.
    Role: CS-E-03.S7.T9 sink test, core audit rows (the agent pg sink test covers the agent tables)."""
    args = {"summary": "Customer wrote: QS-10423 4821, charged twice", "said": "my PIN is four eight two one"}
    assert PIN in str(args) and "four eight two one" in str(args)  # the secret is in the fixture
    audit_id = write_audit(app_conn, AuditEvent(event="ticket_create", actor="agent", mode="chat", args=args),
                           clock=CLOCK)
    row_text = app_conn.execute("SELECT t::text FROM audit_events t WHERE id = %s", (audit_id,)).fetchone()[0]
    assert "QS-10423 [PIN]" in row_text  # the row holds the text, redacted
    assert PIN not in row_text and "four eight two one" not in row_text


def test_audit_ts_comes_from_the_injected_clock(app_conn):
    """What: the stored ts equals the injected clock, not the database clock. Use when: always. Inputs: app_conn.
    Output: assertion. Side effects: rolled back. Role: one time source (America/New_York clock)."""
    audit_id = write_audit(app_conn, AuditEvent(event="lookup", actor="agent"), clock=CLOCK)
    ts = app_conn.execute("SELECT ts FROM audit_events WHERE id = %s", (audit_id,)).fetchone()[0]
    assert ts == CLOCK.now()


def test_replay_returns_the_stored_response_without_running_again(app_conn):
    """What: the second call with the same caller and key returns the first response, replayed=True, and the
    function is not called again. Use when: always. Inputs: app_conn. Output: assertions. Side effects: rolled back.
    Role: doc 07 s3 (a retried mutation has one effect)."""
    calls = []

    def effect() -> tuple[int, dict]:
        """What: a counted effect. Use when: this test. Inputs: none. Output: (200, body). Side effects: appends. Role: probe."""
        calls.append(1)
        return 200, {"n": len(calls)}

    key = f"s:{uuid.uuid4()}"
    first = run_idempotent(app_conn, caller="agent_tools", key=key, endpoint="POST /v1/tickets", request={"a": 1}, fn=effect)
    second = run_idempotent(app_conn, caller="agent_tools", key=key, endpoint="POST /v1/tickets", request={"a": 1}, fn=effect)
    assert (first.replayed, first.body, first.status_code) == (False, {"n": 1}, 200)
    assert (second.replayed, second.body, second.status_code) == (True, {"n": 1}, 200)
    assert len(calls) == 1


def test_same_key_with_a_different_request_is_rejected(app_conn):
    """What: reusing a key for a different payload or endpoint raises VALIDATION_ERROR (422) and does not run fn.
    Use when: always. Inputs: app_conn. Output: assertions. Side effects: rolled back.
    Role: stops a key colliding across unrelated calls from replaying a wrong result."""
    key = f"s:{uuid.uuid4()}"
    run_idempotent(app_conn, caller="agent_tools", key=key, endpoint="E", request={"a": 1}, fn=lambda: (200, {}))
    for endpoint, request in (("E", {"a": 2}), ("OTHER", {"a": 1})):
        with pytest.raises(CoreError) as exc:
            run_idempotent(app_conn, caller="agent_tools", key=key, endpoint=endpoint, request=request,
                           fn=lambda: pytest.fail("fn must not run"))
        assert exc.value.code == "VALIDATION_ERROR" and exc.value.details["reason"] == "idempotency_key_reused"


def test_keys_are_scoped_per_caller(app_conn):
    """What: the same key from two callers is two entries. Use when: always. Inputs: app_conn. Output: assertions.
    Side effects: rolled back. Role: PRIMARY KEY (caller, key)."""
    key = f"s:{uuid.uuid4()}"
    a = run_idempotent(app_conn, caller="agent_tools", key=key, endpoint="E", request={}, fn=lambda: (200, {"who": "a"}))
    b = run_idempotent(app_conn, caller="agent_commit", key=key, endpoint="E", request={}, fn=lambda: (200, {"who": "b"}))
    assert (a.replayed, b.replayed, b.body) == (False, False, {"who": "b"})


def test_a_failed_effect_stores_nothing_so_the_key_can_be_retried(app_conn):
    """What: when fn raises CoreError the key is not recorded and a retry runs fn again. Use when: always.
    Inputs: app_conn. Output: assertions. Side effects: rolled back. Role: an error leaves no phantom success."""
    key = f"s:{uuid.uuid4()}"
    with pytest.raises(CoreError), app_conn.transaction():
        run_idempotent(app_conn, caller="agent_tools", key=key, endpoint="E", request={}, fn=_boom)
    assert app_conn.execute("SELECT count(*) FROM idempotency WHERE key = %s", (key,)).fetchone()[0] == 0
    out = run_idempotent(app_conn, caller="agent_tools", key=key, endpoint="E", request={}, fn=lambda: (201, {"ok": True}))
    assert (out.replayed, out.status_code) == (False, 201)


def _boom() -> tuple[int, dict]:
    """What: an effect that fails. Use when: the failure test. Inputs: none. Output: never returns.
    Side effects: raises CoreError TOOL_ERROR. Role: probe."""
    raise CoreError("TOOL_ERROR")


def test_two_concurrent_requests_with_one_key_run_the_effect_once(app_url, migrated):
    """What: two connections call run_idempotent with one key at the same moment; the effect runs once and one
    caller sees replayed=True. Use when: always. Inputs: app_url. Output: assertions.
    Side effects: commits one idempotency row, deleted at the end. Role: the advisory lock closes the race a plain
    SELECT-then-INSERT would leave open (both pass the SELECT, one then fails on the primary key)."""
    key = f"race:{uuid.uuid4()}"
    runs, results, barrier = [], [], threading.Barrier(2)

    def worker() -> None:
        """What: one racing caller. Use when: threaded. Inputs: none. Output: appends to results.
        Side effects: opens a connection and commits. Role: see test."""
        with psycopg.connect(app_url) as conn:
            barrier.wait()
            out = run_idempotent(conn, caller="agent_tools", key=key, endpoint="E", request={"x": 1},
                                 fn=lambda: (runs.append(1) or (200, {"n": len(runs)})))
            conn.commit()
            results.append(out)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    [t.start() for t in threads]
    [t.join(20) for t in threads]
    try:
        assert len(results) == 2 and len(runs) == 1
        assert sorted(r.replayed for r in results) == [False, True]
    finally:
        with psycopg.connect(app_url) as conn:
            conn.execute("DELETE FROM idempotency WHERE key = %s", (key,))
            conn.commit()
