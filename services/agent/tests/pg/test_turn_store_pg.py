"""What: Postgres-backed test (marker `pg`) for PgTurnStore.last_turn, the delivery source of the s7.3 adjacency
rule (fix round 1, I1 of the PR #47 review).
Use when: scripts/pg_tests.sh (CI job "pg tests") with AGENT_PG_TEST_URL set. Inputs: env AGENT_PG_TEST_URL.
Output: pass or fail; a missing variable FAILS. Side effects: one session plus turns, deleted afterwards (cascade).
Role: pins the SQL the MemoryTurnStore fake stands in for: the latest other turn of the session, with its status."""
from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime, timedelta

import pytest

from agent.ingress import PgTurnStore

pytestmark = pytest.mark.pg


async def test_last_turn_is_the_latest_other_turn_of_the_session_with_its_status() -> None:
    from psycopg_pool import AsyncConnectionPool

    url = os.environ.get("AGENT_PG_TEST_URL", "")
    if not url:
        pytest.fail("AGENT_PG_TEST_URL is not set; run scripts/pg_tests.sh (Compose Postgres, migrate_agent)")
    pool = AsyncConnectionPool(url, min_size=1, max_size=2, open=False)
    await pool.open()
    sid, other = str(uuid.uuid4()), str(uuid.uuid4())
    t0 = datetime.now(UTC)
    rows = [(str(uuid.uuid4()), sid, "completed", t0), (str(uuid.uuid4()), sid, "failed", t0 + timedelta(seconds=5)),
            (str(uuid.uuid4()), other, "completed", t0 + timedelta(seconds=9)),
            (str(uuid.uuid4()), sid, "running", t0 + timedelta(seconds=10))]
    try:
        async with pool.connection() as conn:
            for s in (sid, other):
                await conn.execute("INSERT INTO agent.sessions (session_id, visitor_id) VALUES (%s, 'pg-test')", (s,))
            for tid, s, status, started in rows:
                await conn.execute("INSERT INTO agent.turns (turn_id, session_id, client_turn_id, mode, input_kind, "
                                   "status, deadline_at, started_at) VALUES (%s, %s, %s, 'chat', 'text', %s, %s, %s)",
                                   (tid, s, str(uuid.uuid4()), status, started + timedelta(seconds=45), started))
        store = PgTurnStore(pool)
        current = rows[3][0]
        assert await store.last_turn(sid, current) == (rows[1][0], "failed")
        assert await store.last_turn(sid, rows[1][0]) == (current, "running")
        assert await store.last_turn(str(uuid.uuid4()), current) is None
    finally:
        async with pool.connection() as conn:
            for s in (sid, other):
                await conn.execute("DELETE FROM agent.sessions WHERE session_id = %s", (s,))
        await pool.close()
