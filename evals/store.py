"""What: the read side for the evidence the browser cannot see: core rows (pending_actions, action_history, tickets) and the
stored agent.turn_events, read directly from Postgres with the services' own app roles (CORE_DB_URL, AGENT_DB_URL), SELECT only.
Use when: grading a case after its turns. Inputs: two database URLs. Output: RunData. Side effects: SELECTs; one INSERT into
agent.voice_sessions for voice cases (the real mint would call OpenAI, which evals must not).
Role: doc 07 s2.2 "Evals read agent.turn_events in Postgres directly". Anything unreadable raises DataUnavailable, which grades
the case error."""
from __future__ import annotations

import time
import uuid
from collections.abc import Callable
from typing import Any, Protocol

from .model import RunData


class DataUnavailable(Exception):
    """What: raised when rows or events cannot be read or are incomplete. Use when: collecting. Inputs: message.
    Output: exception. Side effects: none. Role: missing data is error, never pass."""


class Store(Protocol):
    """What: what the runner needs from a store. Use when: typing the runner and writing test doubles. Inputs: see methods.
    Output: protocol. Side effects: none. Role: the seam between the runner and Postgres."""

    def collect(self, session_id: str, expected_events: dict[str, int]) -> RunData:
        """What: read rows and events for a session. Use when: after the turns. Inputs: session id, expected stored-event count
        per turn id. Output: RunData or raises DataUnavailable. Side effects: reads. Role: interface."""
        ...

    def register_voice_session(self, session_id: str) -> str:
        """What: create a voice session the agent will accept. Use when: before a voice turn. Inputs: session id.
        Output: voice_session_id. Side effects: may write. Role: interface."""
        ...


def _connect(url: str) -> Any:
    """What: open a psycopg connection returning dict rows. Use when: no test connect is injected. Inputs: URL.
    Output: connection. Side effects: opens a connection. Role: default connector."""
    import psycopg
    from psycopg.rows import dict_row
    return psycopg.connect(url, row_factory=dict_row, autocommit=False)  # pyright: ignore[reportArgumentType]  # dict_row typing quirk in psycopg stubs


class PgStore:
    """What: the Postgres-backed Store. Use when: a real run. Inputs: core_url, agent_url, optional connect, wait and sleep.
    Output: RunData per session. Side effects: SELECTs (read-only queries), one INSERT for voice sessions.
    Role: polls briefly for the background event writer (doc 07 s12: batched) and fails if events are still missing."""

    def __init__(self, core_url: str, agent_url: str, *, connect: Callable[[str], Any] = _connect, wait_s: float = 8.0,
                 sleep: Callable[[float], None] = time.sleep) -> None:
        """What: remember how to connect. Use when: the CLI builds it. Inputs: URLs, connector, wait seconds, sleeper.
        Output: none. Side effects: none. Role: init."""
        self.core_url, self.agent_url, self.connect, self.wait_s, self.sleep = core_url, agent_url, connect, wait_s, sleep

    def _rows(self, conn: Any, sql: str, sid: str) -> list[dict[str, Any]]:
        """What: run one SELECT for a session. Use when: collecting. Inputs: connection, sql, session id.
        Output: rows. Side effects: a read. Role: single query path (parameterised)."""
        if not sql.lstrip().upper().startswith("SELECT"):
            raise DataUnavailable("refusing to run a non-SELECT statement on a grading connection")
        return list(conn.execute(sql, (sid,)).fetchall())

    def collect(self, session_id: str, expected_events: dict[str, int]) -> RunData:
        """What: read pending rows, committed refunds, tickets and stored events for a session. Use when: grading.
        Inputs: session id, expected stored-event count per turn id. Output: RunData. Side effects: SELECTs.
        Role: raises DataUnavailable on any connection error or when stored events stay incomplete after the wait."""
        try:
            core = self.connect(self.core_url)
            agent = self.connect(self.agent_url)
            # grading never writes: a read-only session (READ COMMITTED stays, the event poll needs fresh reads)
            core.read_only = True
            agent.read_only = True
        except Exception as e:
            raise DataUnavailable(f"cannot connect to Postgres: {type(e).__name__}") from e
        try:
            pending = self._rows(core, "SELECT id, action_type, pending_kind, target_key, params, decision, reason_codes, state, "
                                       "prepare_turn_id, ticket_id FROM pending_actions WHERE session_id = %s ORDER BY created_at", session_id)
            refunds = self._rows(core, "SELECT a.target_key, a.amount_cents, a.committed_by FROM action_history a "
                                       "JOIN pending_actions p ON p.id = a.pending_action_id "
                                       "WHERE p.session_id = %s AND a.action_type = 'refund'", session_id)
            tickets = self._rows(core, "SELECT id, queue, category, stage, priority, origin, escalation_reason, verified, "
                                       "pending_action_id FROM tickets WHERE session_id = %s ORDER BY created_at", session_id)
            deadline = time.monotonic() + self.wait_s
            while True:
                rows = self._rows(agent, "SELECT e.payload FROM agent.turn_events e JOIN agent.turns t ON t.turn_id = e.turn_id "
                                         "WHERE e.session_id = %s ORDER BY t.started_at, e.seq", session_id)
                events = [r["payload"] for r in rows]
                have: dict[str, int] = {}
                for e in events:
                    have[e["turn_id"]] = have.get(e["turn_id"], 0) + 1
                short = {t: (have.get(t, 0), n) for t, n in expected_events.items() if have.get(t, 0) < n}
                if not short:
                    break
                if time.monotonic() >= deadline:
                    raise DataUnavailable(f"turn_events incomplete after {self.wait_s}s (stored, expected) per turn: {short}")
                self.sleep(0.25)
        except DataUnavailable:
            raise
        except Exception as e:
            raise DataUnavailable(f"database read failed: {type(e).__name__}: {e}") from e
        finally:
            for c in (core, agent):
                try:
                    c.close()
                except Exception:  # pragma: no cover - closing a broken connection
                    pass
        return RunData(pending=pending, refunds=refunds, tickets=tickets, events=events)

    def register_voice_session(self, session_id: str) -> str:
        """What: insert an agent.voice_sessions row minted now, valid 10 minutes. Use when: before a voice turn.
        Inputs: session id. Output: the voice_session_id. Side effects: one INSERT into agent.voice_sessions (no OpenAI call).
        Role: lets /voice/ask pass its session check (07 s2.4 step 3) without a real Realtime mint."""
        vs = f"vs_eval_{uuid.uuid4().hex[:12]}"
        try:
            conn = self.connect(self.agent_url)
            conn.execute("INSERT INTO agent.voice_sessions (voice_session_id, session_id, expires_at) "
                         "VALUES (%s, %s, now() + interval '10 minutes')", (vs, session_id))
            conn.commit()
            conn.close()
        except Exception as e:
            raise DataUnavailable(f"cannot register a voice session: {type(e).__name__}") from e
        return vs
