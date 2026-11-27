"""What: Postgres-backed test (marker `pg`) for PgSessionFlags as the D-070 escalation hold store: the
held_escalation column round trip, and the facts read only from agent.sessions (awaiting_verification, the
verified user), never inferred from turn history (review I-2).
Use when: scripts/pg_tests.sh (CI job "pg tests") with AGENT_PG_TEST_URL set. Inputs: env AGENT_PG_TEST_URL.
Output: pass or fail; a missing variable FAILS. Side effects: one session plus turns, deleted afterwards (cascade).
Role: pins the SQL the MemoryHolds fake (tests/test_ticket_requester.py) stands in for, and migration 0002."""
from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime, timedelta

import pytest

from agent.escalation import EscalationFacts
from agent.turn_body import PgSessionFlags

pytestmark = pytest.mark.pg


async def test_hold_round_trip_and_facts_come_only_from_agent_sessions() -> None:
    from psycopg_pool import AsyncConnectionPool

    url = os.environ.get("AGENT_PG_TEST_URL", "")
    if not url:
        pytest.fail("AGENT_PG_TEST_URL is not set; run scripts/pg_tests.sh (Compose Postgres, migrate_agent)")
    pool = AsyncConnectionPool(url, min_size=1, max_size=2, open=False)
    await pool.open()
    sid = str(uuid.uuid4())
    t0 = datetime.now(UTC)

    async def turn(status: str, end_reason: str | None) -> None:
        """What: insert one turn of the session. Use when: arranging. Inputs: status, end reason. Output: none.
        Side effects: one INSERT. Role: helper."""
        async with pool.connection() as conn:
            await conn.execute("INSERT INTO agent.turns (turn_id, session_id, client_turn_id, mode, input_kind, status, "
                               "end_reason, deadline_at, started_at) VALUES (%s, %s, %s, 'chat', 'text', %s, %s, %s, %s)",
                               (str(uuid.uuid4()), sid, str(uuid.uuid4()), status, end_reason,
                                t0 + timedelta(seconds=45), t0))
    try:
        async with pool.connection() as conn:
            await conn.execute("INSERT INTO agent.sessions (session_id, visitor_id) VALUES (%s, 'pg-test')", (sid,))
        flags = PgSessionFlags(pool)
        assert await flags.escalation_facts(sid) == EscalationFacts(False, None, None)
        await turn("completed", "verification_requested")  # an old card request (review I-2): never read
        assert (await flags.escalation_facts(sid)).awaiting is False
        await flags.set_awaiting_verification(sid, True)
        held = {"reason": "not_in_docs", "category": "other", "summary": "s", "turn_id": "t", "prior_user_id": None}
        await flags.hold_escalation(sid, held)
        assert await flags.escalation_facts(sid) == EscalationFacts(True, held, None)
        await flags.set_awaiting_verification(sid, False, "U-6633")
        await flags.hold_escalation(sid, None)
        assert await flags.escalation_facts(sid) == EscalationFacts(False, None, "U-6633")
        assert await flags.escalation_facts(str(uuid.uuid4())) == EscalationFacts(False, None, None)
    finally:
        async with pool.connection() as conn:
            await conn.execute("DELETE FROM agent.sessions WHERE session_id = %s", (sid,))
        await pool.close()
