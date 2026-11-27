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
