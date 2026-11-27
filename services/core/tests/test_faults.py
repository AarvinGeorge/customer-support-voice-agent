"""What: unit tests (no database) of the demo `tool_error` fault: the dependency on prepare and commit, its audit row,
and the flag gate in the header parser.
Use when: changing core/faults.py, core/auth.py or the route table.
Inputs: the `client` fixture (DEMO_FAULTS_ENABLED true) or an app built with the flag false.
Output: pytest results. Side effects: none.
Role: CS-E-07.S1.T1 (doc 07 s10): the fault fires only on prepare and commit, only with the flag, before any transaction,
and an ignored header is logged as fault_header_ignored."""
from __future__ import annotations

import logging
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from fastapi.testclient import TestClient

from core.app import create_app
from core.clock import FixedClock
from core.config import Settings
from tests.conftest import POLICY, TOKENS

SID = str(uuid.uuid4())
BODY = {"charge_ref": {"ending": "4242"}, "reason": "duplicate", "note": "charged twice"}


class FakeConn:
    """What: a connection stand-in that records executed SQL. Use when: asserting an audit INSERT without Postgres.
    Inputs: none. Output: .calls list of (sql, params). Side effects: none. Role: test double."""

    def __init__(self) -> None:
        """What: start with no calls. Use when: constructing. Inputs: none. Output: none. Side effects: none.
        Role: test double."""
        self.calls: list[tuple[str, Any]] = []

    def execute(self, sql: str, params: Any = None) -> None:
        """What: record one statement. Use when: the audit writer INSERTs. Inputs: sql, params. Output: none.
        Side effects: appends to calls. Role: test double."""
        self.calls.append((sql, params))


class FakeDb:
    """What: a Database stand-in whose tx() yields one FakeConn and counts transactions. Use when: proving the fault
    audit row is written in its own short transaction. Inputs: none. Output: .conn, .txs. Side effects: none.
    Role: test double."""

    def __init__(self) -> None:
        """What: build the connection and the counter. Use when: constructing. Inputs: none. Output: none.
        Side effects: none. Role: test double."""
        self.conn = FakeConn()
        self.txs = 0

    def close(self) -> None:
        """What: nothing to close. Use when: the app lifespan ends. Inputs: none. Output: none. Side effects: none.
        Role: test double."""

    @contextmanager
    def tx(self) -> Iterator[FakeConn]:
        """What: yield the connection and count one transaction. Use when: code calls db.tx(). Inputs: none.
        Output: FakeConn. Side effects: increments txs. Role: test double."""
        self.txs += 1
        yield self.conn


def _headers(token: str = "agent_tools", fault: str | None = "tool_error") -> dict[str, str]:
    """What: request headers for a prepare or commit call. Use when: any test here. Inputs: token name, fault value.
    Output: dict. Side effects: none. Role: helper."""
    h = {"Authorization": f"Bearer {TOKENS[token]}", "X-Session-Id": SID, "X-Turn-Id": "turn-1", "X-Mode": "chat",
         "Idempotency-Key": "k1"}
    if fault:
        h["X-Demo-Fault"] = fault
    return h


def _app(flag: bool) -> TestClient:
    """What: a started app with a FakeDb and the flag set. Use when: a test needs the flag off or a fake db.
    Inputs: the flag. Output: TestClient (use as a context manager). Side effects: none. Role: helper."""
    settings = Settings(token_agent=TOKENS["agent_tools"], token_commit=TOKENS["agent_commit"],
                        token_ops=TOKENS["ops"], db_url=None, demo_faults=flag, policy_path=str(POLICY))
    from datetime import datetime
    from zoneinfo import ZoneInfo
    app = create_app(settings, clock=FixedClock(datetime(2026, 11, 27, 9, 30, tzinfo=ZoneInfo("America/New_York"))))
    return TestClient(app)


def test_faulted_prepare_returns_503_tool_error_before_any_validation(client: TestClient) -> None:
    """What: flag on, tool_error on refund prepare: 503 TOOL_ERROR, retryable, changed false, fault_injected true.
    The body is empty, so a 422 would mean the fault ran after validation. Use when: pytest. Inputs: client.
    Output: asserts. Side effects: none. Role: doc 07 s10 tool_error row; before-anything ordering."""
    r = client.post("/v1/actions/refund/prepare", headers=_headers(), json={})
    assert r.status_code == 503, r.text
    e = r.json()["error"]
    assert (e["code"], e["retryable"], e["changed"], e["fault_injected"]) == ("TOOL_ERROR", True, False, True)


def test_faulted_commit_and_grace_prepare_return_503(client: TestClient) -> None:
    """What: commit and the grace prepare route are faulted too. Use when: pytest. Inputs: client. Output: asserts.
    Side effects: none. Role: doc 07 s10 names POST /v1/actions/*/prepare and the commit route."""
    r = client.post("/v1/actions/pending/PA-0000000A/commit", headers=_headers("agent_commit"), json={})
    assert (r.status_code, r.json()["error"]["fault_injected"]) == (503, True)
    r = client.post("/v1/actions/grace_extension/prepare", headers=_headers(), json={})
    assert (r.status_code, r.json()["error"]["fault_injected"]) == (503, True)


def test_other_routes_ignore_the_fault(client: TestClient) -> None:
    """What: status, submit, cancel, identity, reads and tickets are not faulted. Use when: pytest. Inputs: client.
    Output: asserts (they answer their own 422, not 503). Side effects: none. Role: the status check and the escalation
    ticket must still work (doc 07 s10)."""
    for method, path, token in (
        ("GET", "/v1/actions/pending/PA-0000000A", "agent_tools"),
        ("POST", "/v1/actions/pending/PA-0000000A/submit", "agent_commit"),
        ("POST", "/v1/actions/pending/PA-0000000A/cancel", "agent_commit"),
        ("POST", "/v1/tickets", "agent_tools"),
        ("GET", "/v1/me/tickets", "agent_tools"),
    ):
        h = _headers(token)
        h.pop("X-Session-Id")
        r = client.request(method, path, headers=h, json={} if method == "POST" else None)
        assert r.status_code != 503, (path, r.text)


def test_llm_429_header_is_not_a_core_fault(client: TestClient) -> None:
    """What: X-Demo-Fault llm_429 does nothing in core. Use when: pytest. Inputs: client. Output: assert.
    Side effects: none. Role: the agent handles llm_429; core applies only tool_error."""
    r = client.post("/v1/actions/refund/prepare", headers=_headers(fault="llm_429"), json={})
    assert r.status_code == 422


def test_flag_off_ignores_the_header_and_logs_it(caplog) -> None:
    """What: flag false: the header has no effect and `fault_header_ignored` is logged. Use when: pytest.
    Inputs: caplog. Output: asserts. Side effects: none. Role: doc 07 s10 Gate."""
    with _app(False) as c, caplog.at_level(logging.INFO, logger="core.auth"):
        r = c.post("/v1/actions/refund/prepare", headers=_headers(), json={})
    assert r.status_code == 422  # the normal validation path ran
    assert r.json()["error"].get("fault_injected") is False
    assert any("fault_header_ignored" in m for m in caplog.messages), caplog.messages


def test_no_header_no_log(caplog) -> None:
    """What: a request without the header logs no fault line. Use when: pytest. Inputs: caplog. Output: assert.
    Side effects: none. Role: the log line means a header really arrived."""
    with _app(False) as c, caplog.at_level(logging.INFO, logger="core.auth"):
        c.post("/v1/actions/refund/prepare", headers=_headers(fault=None), json={})
    assert not any("fault_header_ignored" in m for m in caplog.messages)


def test_fault_writes_a_fault_injected_audit_row_in_its_own_transaction() -> None:
    """What: with a database present the fault INSERTs one audit row event=fault_injected (decision error, status 503)
    in a transaction of its own, before the 503. Use when: pytest. Inputs: none. Output: asserts.
    Side effects: none. Role: doc 07 s10 "an audit row fault_injected"."""
    with _app(True) as c:
        db = FakeDb()
        c.app.state.db = db  # type: ignore[attr-defined]
        r = c.post("/v1/actions/refund/prepare", headers=_headers(), json=BODY)
    assert r.status_code == 503
    assert db.txs == 1
    sql, params = db.conn.calls[0]
    assert "INSERT INTO audit_events" in sql
    assert "fault_injected" in params
    assert 503 in params and "error" in params


def test_an_audit_failure_still_returns_the_fault() -> None:
    """What: if the audit INSERT itself fails the caller still gets the 503 fault, not a 500. Use when: pytest.
    Inputs: none. Output: assert. Side effects: none. Role: a fault must only ever fail a call, never change its kind."""

    class BrokenDb:
        """What: a Database whose tx() raises. Use when: this test. Inputs: none. Output: none. Side effects: none.
        Role: test double."""

        def close(self) -> None:
            """What: nothing to close. Use when: the app lifespan ends. Inputs: none. Output: none.
            Side effects: none. Role: test double."""

        def tx(self):
            """What: raise like a down database. Use when: called. Inputs: none. Output: never returns.
            Side effects: raises. Role: test double."""
            raise RuntimeError("db down")

    with _app(True) as c:
        c.app.state.db = BrokenDb()  # type: ignore[attr-defined]
        r = c.post("/v1/actions/refund/prepare", headers=_headers(), json=BODY)
    assert (r.status_code, r.json()["error"]["code"], r.json()["error"]["fault_injected"]) == (503, "TOOL_ERROR", True)
