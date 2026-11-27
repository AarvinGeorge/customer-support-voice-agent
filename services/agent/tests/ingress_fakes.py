"""What: in-memory TurnStore and TurnLock with the same contract as the Postgres and Redis versions.
Use when: ingress unit tests. Inputs: none. Output: MemoryTurnStore, MemoryTurnLock. Side effects: none.
Role: test doubles (the real ones are exercised in the Compose stack, see the S7 report)."""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta
from typing import Any

from agent.ingress import DuplicateTurn, SessionRow, TurnRow, swept_error, swept_final
from agent.sse import TurnOutcome


class MemoryTurnStore:
    """What: dict-backed agent.sessions and agent.turns. Use when: tests. Inputs: sessions.
    Output: TurnStore methods. Side effects: mutates its dicts. Role: fake."""

    def __init__(self, *sessions: SessionRow) -> None:
        """What: seed sessions. Use when: tests. Inputs: rows. Output: none. Side effects: none. Role: fake."""
        self.sessions = {s.session_id: s for s in sessions}
        self.turns: dict[str, TurnRow] = {}
        self.finished: list[tuple[str, TurnOutcome]] = []
        self.swept_calls: list[str] = []

    async def get_session(self, session_id: str) -> SessionRow | None:
        """What: lookup. Use when: step 1. Inputs: id. Output: row or None. Side effects: none. Role: fake."""
        return self.sessions.get(session_id)

    async def find_turn(self, session_id: str, client_turn_id: str, realtime_call_id: str | None = None) -> TurnRow | None:
        """What: replay lookup. Use when: step 4. Inputs: ids. Output: row or None. Side effects: none. Role: fake."""
        for t in self.turns.values():
            if t.session_id == session_id and (t.client_turn_id == client_turn_id or
                                               (realtime_call_id and t.realtime_call_id == realtime_call_id)):
                return t
        return None

    async def sweep(self, session_id: str, now: datetime) -> int:
        """What: mark overdue running turns swept. Use when: step 6. Inputs: id, now. Output: count.
        Side effects: updates rows. Role: fake of the doc 07 s2.4 step 6 UPDATE."""
        self.swept_calls.append(session_id)
        n = 0
        for k, t in self.turns.items():
            if t.session_id == session_id and t.status == "running" and t.deadline_at + timedelta(seconds=10) < now:
                self.turns[k] = replace(t, status="failed", end_reason="swept",
                                        error=swept_error().model_dump(mode="json", by_alias=True),
                                        final=swept_final().model_dump(mode="json"))
                n += 1
        return n

    async def insert_turn(self, row: TurnRow) -> None:
        """What: insert; unique (session_id, client_turn_id). Use when: step 9. Inputs: row. Output: none.
        Side effects: stores; raises DuplicateTurn. Role: fake."""
        if any(t.session_id == row.session_id and t.client_turn_id == row.client_turn_id for t in self.turns.values()):
            raise DuplicateTurn(row.client_turn_id)
        self.turns[row.turn_id] = row

    async def count_turn(self, session_id: str) -> None:
        """What: user_turns += 1. Use when: step 9. Inputs: id. Output: none. Side effects: updates. Role: fake."""
        s = self.sessions[session_id]
        self.sessions[session_id] = replace(s, user_turns=s.user_turns + 1)

    async def last_turn(self, session_id: str, before_turn_id: str) -> tuple[str, str] | None:
        """What: (turn_id, status) of the session's latest turn other than before_turn_id. Use when: adjacency (I1).
        Inputs: ids. Output: pair or None. Side effects: none. Role: fake (insertion order is start order)."""
        rows = [t for t in self.turns.values() if t.session_id == session_id and t.turn_id != before_turn_id]
        return (rows[-1].turn_id, rows[-1].status) if rows else None

    async def finish_turn(self, turn_id: str, outcome: TurnOutcome, ended_at: datetime) -> None:
        """What: final row. Use when: step 16. Inputs: id, outcome, time. Output: none. Side effects: updates. Role: fake."""
        t = self.turns[turn_id]
        self.turns[turn_id] = replace(t, status=outcome.status, end_reason=outcome.end_reason,
                                      final=outcome.final.model_dump(mode="json"),
                                      usage=outcome.usage.model_dump(mode="json"),
                                      error=outcome.error.model_dump(mode="json", by_alias=True) if outcome.error else None)
        self.finished.append((turn_id, outcome))


class MemoryTurnLock:
    """What: SET NX with owner token and compare-and-delete. Use when: tests. Inputs: none.
    Output: TurnLock methods. Side effects: mutates its dict. Role: fake of lock:turn:{sid}."""

    def __init__(self, fail: Exception | None = None) -> None:
        """What: empty lock table. Use when: tests. Inputs: optional exception to raise. Output: none.
        Side effects: none. Role: fake."""
        self.held: dict[str, str] = {}
        self.fail = fail
        self.log: list[tuple[str, Any]] = []

    async def acquire(self, session_id: str, token: str, ttl_ms: int) -> bool:
        """What: take the lock if free. Use when: step 7. Inputs: sid, token, ttl. Output: bool.
        Side effects: records. Role: fake."""
        if self.fail:
            raise self.fail
        self.log.append(("acquire", ttl_ms))
        if session_id in self.held:
            return False
        self.held[session_id] = token
        return True

    async def release(self, session_id: str, token: str) -> bool:
        """What: delete only when the token matches. Use when: step 16. Inputs: sid, token. Output: bool.
        Side effects: records. Role: fake."""
        self.log.append(("release", token))
        if self.held.get(session_id) == token:
            del self.held[session_id]
            return True
        return False
