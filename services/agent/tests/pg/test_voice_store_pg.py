"""What: Postgres-backed tests (marker `pg`) for PgVoiceStore on the real agent schema (review PR #2 I3).
Use when: scripts/pg_tests.sh (CI job "pg tests") with AGENT_PG_TEST_URL pointing at agent_app on the Compose
Postgres, after migrate_agent built the schema.
Inputs: env AGENT_PG_TEST_URL. Output: pass or fail; a missing variable FAILS (a skipped DB test tests nothing).
Side effects: inserts one session (plus turns, voice session, items) per test and deletes it (cascade) afterwards.
Role: pins the SQL the fakes stand in for: posted order inside one post (the clock_timestamp fix), covered item
excluded, re-post ignored, claim once under two concurrent turns, the voice_sessions round trip."""
from __future__ import annotations

import asyncio
import os
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, AsyncIterator, Literal

import pytest
from contracts.agent_api import TranscriptItemIn

from agent.voice import CoveredItem, PgVoiceStore, VoiceSessionRow

pytestmark = pytest.mark.pg


def item(i: str, role: Literal["user", "assistant"], text: str) -> TranscriptItemIn:
    """What: one posted item. Use when: tests. Inputs: id, role, text. Output: model. Side effects: none.
    Role: helper."""
    return TranscriptItemIn(realtime_item_id=i, role=role, text=text, interrupted=False, client_ts="2026-11-28T13:00:00Z")


class Ctx:
    """What: a pool, a store and one fresh session with a voice session. Use when: each test. Inputs: pool.
    Output: attributes and a turn() helper. Side effects: rows. Role: fixture value."""

    def __init__(self, pool: Any, sid: str) -> None:
        """What: keep handles. Use when: fixture. Inputs: pool, session id. Output: none. Side effects: none.
        Role: constructor."""
        self.pool, self.sid, self.store = pool, sid, PgVoiceStore(pool)

    async def turn(self) -> str:
        """What: insert a running voice turn (FK parent of merged_into_turn_id). Use when: before a claim.
        Inputs: none. Output: turn id. Side effects: INSERT. Role: helper."""
        tid = str(uuid.uuid4())
        async with self.pool.connection() as conn:
            await conn.execute("INSERT INTO agent.turns (turn_id, session_id, client_turn_id, mode, input_kind, status, "
                               "deadline_at) VALUES (%s, %s, %s, 'voice', 'voice', 'running', now())",
                               (tid, self.sid, str(uuid.uuid4())))
        return tid


@pytest.fixture
async def ctx() -> AsyncIterator[Ctx]:
    """What: open an agent_app pool, create a session and voice session, delete the session after the test.
    Use when: every test here. Inputs: env. Output: Ctx. Side effects: rows created then removed (cascade).
    Role: isolation without a schema reset."""
    from psycopg_pool import AsyncConnectionPool

    url = os.environ.get("AGENT_PG_TEST_URL", "")
    if not url:
        pytest.fail("AGENT_PG_TEST_URL is not set; run scripts/pg_tests.sh (Compose Postgres, migrate_agent)")
    pool = AsyncConnectionPool(url, min_size=1, max_size=4, open=False)
    await pool.open()
    sid = str(uuid.uuid4())
    async with pool.connection() as conn:
        await conn.execute("INSERT INTO agent.sessions (session_id, visitor_id) VALUES (%s, 'pg-test')", (sid,))
    c = Ctx(pool, sid)
    now = datetime.now(UTC)
    await c.store.create_voice_session(VoiceSessionRow("vs-" + sid, sid, now, now + timedelta(seconds=600)))
    try:
        yield c
    finally:
        async with pool.connection() as conn:
            await conn.execute("DELETE FROM agent.sessions WHERE session_id = %s", (sid,))
        await pool.close()


async def test_voice_session_round_trip(ctx: Ctx) -> None:
    row = await ctx.store.get_voice_session("vs-" + ctx.sid)
    assert row is not None and row.session_id == ctx.sid
    assert row.expires_at - row.minted_at == timedelta(seconds=600)
    assert await ctx.store.get_voice_session("vs-missing") is None


async def test_one_post_keeps_its_order_and_a_repost_is_ignored(ctx: Ctx) -> None:
    vs = "vs-" + ctx.sid
    posted = [item(f"o{i:02d}", "user" if i % 2 else "assistant", f"line {i:02d}") for i in range(50)]
    assert await ctx.store.add_transcript_items(ctx.sid, vs, posted) == 50
    assert await ctx.store.add_transcript_items(ctx.sid, vs, posted[:5]) == 0
    merged = await ctx.store.take_unmerged(ctx.sid, await ctx.turn(), None)
    assert [m.text for m in merged] == [f"line {i:02d}" for i in range(50)]  # same now() in one post: order kept


async def test_covered_item_is_left_out_and_stays_out_after_a_late_post(ctx: Ctx) -> None:
    vs = "vs-" + ctx.sid
    await ctx.store.add_transcript_items(ctx.sid, vs, [item("a1", "assistant", "Hi"), item("u1", "user", "refund?")])
    t1 = await ctx.turn()
    first = await ctx.store.take_unmerged(ctx.sid, t1, CoveredItem("u1", vs, "refund?"))
    assert [m.text for m in first] == ["Hi"]
    t2 = await ctx.turn()  # covered before its own post arrives
    await ctx.store.take_unmerged(ctx.sid, t2, CoveredItem("u2", vs, "second"))
    await ctx.store.add_transcript_items(ctx.sid, vs, [item("u2", "user", "second"), item("a2", "assistant", "One moment.")])
    third = await ctx.store.take_unmerged(ctx.sid, await ctx.turn(), None)
    assert [m.text for m in third] == ["One moment."]
    assert await ctx.store.take_unmerged(ctx.sid, await ctx.turn(), None) == []
    async with ctx.pool.connection() as conn:
        cur = await conn.execute("SELECT realtime_item_id, turn_id::text, merged_into_turn_id::text "
                                 "FROM agent.transcript_items WHERE session_id = %s AND realtime_item_id IN ('u1', 'u2')",
                                 (ctx.sid,))
        rows = {r[0]: (r[1], r[2]) for r in await cur.fetchall()}
    assert rows == {"u1": (t1, t1), "u2": (t2, t2)}


async def test_two_concurrent_claims_take_each_item_once(ctx: Ctx) -> None:
    vs = "vs-" + ctx.sid
    await ctx.store.add_transcript_items(ctx.sid, vs, [item(f"c{i}", "user", f"c{i}") for i in range(20)])
    ta, tb = await ctx.turn(), await ctx.turn()
    a, b = await asyncio.gather(ctx.store.take_unmerged(ctx.sid, ta, None), ctx.store.take_unmerged(ctx.sid, tb, None))
    texts = [m.text for m in a] + [m.text for m in b]
    assert sorted(texts) == sorted(f"c{i}" for i in range(20)) and len(texts) == 20
