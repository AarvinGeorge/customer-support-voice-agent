"""What: tests for PgStore against a stub connection (no Postgres): queries, row shaping, polling for stored events.
Use when: CI. Inputs: stub rows. Output: pass or fail. Side effects: none. Role: the real store's logic without a database."""
from __future__ import annotations

from typing import Any

import pytest

from evals.store import DataUnavailable, PgStore


class StubCursor:
    """What: a cursor returning canned rows. Use when: stubbing psycopg. Inputs: rows. Output: rows. Side effects: none. Role: stub."""

    def __init__(self, rows: list[Any]) -> None:
        """What: keep rows. Use when: execute is called. Inputs: rows. Output: none. Side effects: none. Role: init."""
        self.rows = rows

    def fetchall(self) -> list[Any]:
        """What: return the rows. Use when: the store reads. Inputs: none. Output: rows. Side effects: none. Role: stub."""
        return self.rows


class StubConn:
    """What: a connection that routes SQL by a substring. Use when: PgStore tests. Inputs: routes. Output: cursors.
    Side effects: records SQL. Role: stub."""

    def __init__(self, routes: dict[str, list[Any]]) -> None:
        """What: keep routes. Use when: building. Inputs: substring to rows. Output: none. Side effects: none. Role: init."""
        self.routes, self.sql, self.committed = routes, [], False

    def execute(self, sql: str, params: Any = None) -> StubCursor:
        """What: record and route. Use when: the store queries. Inputs: sql, params. Output: StubCursor. Side effects: records.
        Role: stub."""
        self.sql.append((sql, params))
        for k, rows in self.routes.items():
            if k in sql:
                return StubCursor(rows)
        raise AssertionError(f"unrouted sql {sql}")

    def commit(self) -> None:
        """What: note the commit. Use when: a write finishes. Inputs: none. Output: none. Side effects: flag. Role: stub."""
        self.committed = True

    def close(self) -> None:
        """What: no-op. Use when: the store finishes. Inputs: none. Output: none. Side effects: none. Role: stub."""


def make(core_rows: dict[str, list[Any]], agent_rows: dict[str, list[Any]]) -> tuple[PgStore, StubConn, StubConn]:
    """What: a PgStore over two stub connections. Use when: each test. Inputs: routes per database. Output: store and stubs.
    Side effects: none. Role: helper."""
    core, agent = StubConn(core_rows), StubConn(agent_rows)
    conns = {"core-url": core, "agent-url": agent}
    return PgStore("core-url", "agent-url", connect=lambda url: conns[url], wait_s=0.0, sleep=lambda s: None), core, agent


EV = {"turn_id": "t1", "seq": 1, "type": "turn.started"}


def test_collect_reads_rows_by_session_and_the_stored_events() -> None:
    store, core, agent = make(
        {"FROM pending_actions": [{"id": "PA-1"}], "FROM action_history": [{"amount_cents": 9600}], "FROM tickets": [{"id": "TCK-1"}]},
        {"agent.turn_events": [{"payload": EV}]})
    d = store.collect("sid-1", {"t1": 1})
    assert d.pending == [{"id": "PA-1"}] and d.refunds == [{"amount_cents": 9600}] and d.tickets == [{"id": "TCK-1"}]
    assert d.events == [EV]
    assert all(p == ("sid-1",) for _, p in core.sql + agent.sql)


def test_reads_are_select_only() -> None:
    store, core, agent = make({"FROM pending_actions": [], "FROM action_history": [], "FROM tickets": []}, {"agent.turn_events": [{"payload": EV}]})
    store.collect("sid-1", {"t1": 1})
    assert all(sql.lstrip().upper().startswith("SELECT") for sql, _ in core.sql + agent.sql)


def test_incomplete_turn_events_are_data_unavailable_not_a_short_list() -> None:
    store, *_ = make({"FROM pending_actions": [], "FROM action_history": [], "FROM tickets": []}, {"agent.turn_events": [{"payload": EV}]})
    with pytest.raises(DataUnavailable):
        store.collect("sid-1", {"t1": 3})


def test_connection_failure_is_data_unavailable() -> None:
    def boom(url: str) -> Any:
        raise OSError("refused")
    store = PgStore("a", "b", connect=boom, wait_s=0.0, sleep=lambda s: None)
    with pytest.raises(DataUnavailable):
        store.collect("sid-1", {})


def test_register_voice_session_inserts_a_row_and_commits() -> None:
    store, _core, agent = make({}, {"INSERT INTO agent.voice_sessions": []})
    vs = store.register_voice_session("sid-1")
    assert vs.startswith("vs_eval_") and agent.committed


class RoConn(StubConn):
    """What: a stub connection that records read_only like psycopg and refuses writes once it is set. Use when: I1 tests.
    Inputs: routes. Output: cursors. Side effects: none. Role: proves a write attempt fails on the grading connections."""

    read_only = False

    def execute(self, sql: str, params: Any = None) -> StubCursor:
        """What: refuse non-SELECT when read_only. Use when: queried. Inputs: sql, params. Output: cursor. Side effects: records.
        Role: mimics Postgres 'cannot execute INSERT in a read-only transaction'."""
        if self.read_only and not sql.lstrip().upper().startswith("SELECT"):
            raise RuntimeError("cannot execute in a read-only transaction")
        return super().execute(sql, params)


def test_grading_connections_are_read_only_and_a_write_attempt_fails() -> None:
    core, agent = RoConn({"FROM pending_actions": [], "FROM action_history": [], "FROM tickets": []}), RoConn({"agent.turn_events": [{"payload": EV}]})
    conns = {"c": core, "a": agent}
    store = PgStore("c", "a", connect=lambda u: conns[u], wait_s=0.0, sleep=lambda s: None)
    store.collect("sid", {"t1": 1})
    assert core.read_only is True and agent.read_only is True
    with pytest.raises(RuntimeError, match="read-only"):
        core.execute("DELETE FROM tickets WHERE session_id = %s", ("sid",))


def test_the_select_guard_refuses_a_write_even_before_the_database_does() -> None:
    store, *_ = make({}, {})
    with pytest.raises(DataUnavailable):
        store._rows(StubConn({"": []}), "UPDATE tickets SET stage = 'closed' WHERE session_id = %s", "sid")


def test_the_voice_insert_uses_its_own_writable_connection() -> None:
    agent = RoConn({"INSERT INTO agent.voice_sessions": []})
    store = PgStore("c", "a", connect=lambda u: agent, wait_s=0.0, sleep=lambda s: None)
    store.register_voice_session("sid")
    assert agent.read_only is False and agent.committed
