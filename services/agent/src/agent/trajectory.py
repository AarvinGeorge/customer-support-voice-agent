"""What: the TrajectoryWriter: a batched background writer of a turn's SSE events (all but `token`) into
agent.turn_events (doc 07 s6.1, s12; doc 06 s8, s18).
Use when: the app lifespan starts one per replica; ingress passes its add method to each TurnStream as the sink.
Inputs: the agent_app DSN (the writer opens its OWN small pool) or an injected pool (tests); an on_incomplete
callback that sets agent.turns.events_complete = false through the turn store's pool.
Output: TrajectoryWriter, event_row, INSERT_SQL.
Side effects: INSERTs into agent.turn_events; logs dropped batches; calls on_incomplete.
Role: G6 "a failed write never blocks or fails a turn": add() is synchronous and never raises, the flush runs in
its own task with a bounded timeout, and a failed or overflowing batch is dropped and its turns are marked
events_complete=false (turns.final, written synchronously by ingress, stays the record of the answer).
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Callable

from contracts import limits
from contracts.events import Envelope
from contracts.redact import RedactContext, redact
from psycopg.types.json import Jsonb

log = logging.getLogger("agent.trajectory")

INSERT_SQL = ("INSERT INTO agent.turn_events (turn_id, seq, session_id, ts, type, step_id, payload) "
              "VALUES (%s, %s, %s, %s, %s, %s, %s) ON CONFLICT (turn_id, seq) DO NOTHING")
POOL_MAX = 2            # one flush at a time plus one spare; never competes with the turn pool
FLUSH_INTERVAL_S = 0.25
BATCH_MAX = 200
BUFFER_MAX = 5000       # about 125 turns of 40 events; beyond this new rows are dropped
Row = tuple[Any, ...]


def event_row(ev: Envelope, context: RedactContext | None = None) -> Row:
    """What: one agent.turn_events row for an event, payload redacted with contracts redact.
    Use when: TrajectoryWriter.add. Inputs: the typed event; redaction context (default: no session facts).
    Output: (turn_id, seq, session_id, ts, type, step_id, Jsonb(payload)).
    Side effects: none. Role: doc 07 s12 "payload: the full event as sent on SSE, already redacted"."""
    payload = redact(ev.model_dump(mode="json", by_alias=True), context or RedactContext())
    return (payload["turn_id"], payload["seq"], payload["session_id"], payload["ts"], payload["type"],
            payload.get("step_id"), Jsonb(payload))


class TrajectoryWriter:
    """What: buffer of event rows flushed in batches by a background task over its own pool.
    Use when: one per process (app.state.trajectory); TurnStream(sink=writer.add) per persisted turn.
    Inputs: dsn (agent_app) or pool; on_incomplete(turn_ids) awaitable; flush interval, batch and buffer sizes;
    timeout_s for one batch (connection checkout plus insert).
    Output: add, flush, start, aclose, pending; counters written and dropped.
    Side effects: database writes; one background task between start and aclose.
    Role: non-durable per-replica write buffer (doc 07 s12 honest list)."""

    def __init__(self, dsn: str | None = None, *, pool: Any = None,
                 on_incomplete: Callable[[set[str]], Awaitable[None]] | None = None,
                 flush_interval_s: float = FLUSH_INTERVAL_S, batch_max: int = BATCH_MAX,
                 buffer_max: int = BUFFER_MAX, timeout_s: float = limits.POSTGRES_CHECKPOINTER_TIMEOUT_S) -> None:
        """What: configure; nothing connects here. Use when: app start or tests. Inputs: see class.
        Output: none. Side effects: builds (does not open) a psycopg pool when a dsn is given. Role: constructor."""
        if pool is None and dsn is not None:
            from psycopg_pool import AsyncConnectionPool
            pool = AsyncConnectionPool(dsn, min_size=0, max_size=POOL_MAX, open=False, timeout=timeout_s,
                                       name="trajectory",
                                       kwargs={"autocommit": False, "prepare_threshold": None,
                                               "connect_timeout": max(int(timeout_s), 1),
                                               "options": f"-c statement_timeout={int(timeout_s * 1000)}"})
        self.pool, self.on_incomplete = pool, on_incomplete
        self.flush_interval_s, self.batch_max, self.buffer_max = flush_interval_s, batch_max, buffer_max
        self.timeout_s = timeout_s
        self.written = self.dropped = 0
        self._buf: list[Row] = []
        self._incomplete: set[str] = set()
        self._task: asyncio.Task[None] | None = None
        self._lock = asyncio.Lock()

    def add(self, ev: Envelope) -> None:
        """What: queue one event row (token events are skipped). Use when: TurnStream.emit (the sink).
        Inputs: the typed event. Output: none. Side effects: appends to the buffer, or drops the row and marks
        its turn incomplete when the buffer is full or the row cannot be built. Never raises.
        Role: the only call on the turn's path; synchronous and O(1) apart from redaction."""
        try:
            if getattr(ev, "type", None) == "token":
                return
            if len(self._buf) >= self.buffer_max:
                self.dropped += 1
                self._incomplete.add(str(getattr(ev, "turn_id", "")))
                return
            self._buf.append(event_row(ev))
        except Exception:  # noqa: BLE001 - a sink must never break a turn
            self.dropped += 1
            self._incomplete.add(str(getattr(ev, "turn_id", "")))
            log.exception("trajectory row build failed")

    def pending(self) -> int:
        """What: rows waiting in the buffer. Use when: tests and shutdown. Inputs: none. Output: int.
        Side effects: none. Role: accessor."""
        return len(self._buf)

    async def flush(self) -> None:
        """What: write everything buffered, in batches of batch_max, each bounded by timeout_s; a failed batch is
        dropped (logged) and its turn ids go to on_incomplete, with rows dropped by add().
        Use when: the background loop, aclose, tests. Inputs: none. Output: none.
        Side effects: INSERTs, on_incomplete call; never raises. Role: one flush at a time (lock)."""
        async with self._lock:
            while self._buf:
                batch, self._buf = self._buf[:self.batch_max], self._buf[self.batch_max:]
                await self._write(batch, split=True)
            await self._report_incomplete()

    async def _write(self, batch: list[Row], *, split: bool) -> None:
        """What: insert one batch; when the SERVER rejects it (psycopg DatabaseError, for example an FK violation
        for a turn with no turns row) and split is set, retry once per turn so one bad turn drops only its own
        rows. An unreachable database or a timeout drops the whole batch (no per-turn retries against it).
        Use when: flush. Inputs: rows, split flag. Output: none. Side effects: INSERTs, counters, incomplete set.
        Role: drop on failure, never raise (except cancellation, which still records the loss)."""
        import psycopg
        try:
            await asyncio.wait_for(self._insert(batch), self.timeout_s)
            self.written += len(batch)
            return
        except asyncio.CancelledError:  # shutdown mid-batch: the rows are lost, say so
            self._drop(batch)
            raise
        except psycopg.DatabaseError as exc:
            if isinstance(exc, psycopg.OperationalError):  # unreachable, pool timeout: drop all, no retries
                self._drop(batch)
                log.warning("turn_events batch dropped rows=%d error=%s", len(batch), type(exc).__name__)
                return
            turns = list(dict.fromkeys(r[0] for r in batch))
            if split and len(turns) > 1:
                for t in turns:
                    await self._write([r for r in batch if r[0] == t], split=False)
                return
            self._drop(batch)
            log.warning("turn_events batch dropped rows=%d error=%s", len(batch), type(exc).__name__)
        except Exception as exc:  # noqa: BLE001 - G6: drop on failure
            self._drop(batch)
            log.warning("turn_events batch dropped rows=%d error=%s", len(batch), type(exc).__name__)

    def _drop(self, batch: list[Row]) -> None:
        """What: count a lost batch and remember its turns. Use when: _write fails. Inputs: rows.
        Output: none. Side effects: counters. Role: helper."""
        self.dropped += len(batch)
        self._incomplete |= {str(r[0]) for r in batch}

    async def _insert(self, batch: list[Row]) -> None:
        """What: one transaction with executemany. Use when: flush. Inputs: rows. Output: none.
        Side effects: INSERT. Role: helper."""
        if self.pool is None:
            raise RuntimeError("trajectory writer has no pool")
        async with self.pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.executemany(INSERT_SQL, batch)

    async def _report_incomplete(self) -> None:
        """What: hand the dropped turns to on_incomplete once (bounded, never raises). Use when: end of flush.
        Inputs: none. Output: none. Side effects: on_incomplete call. Role: events_complete=false."""
        if not self._incomplete or self.on_incomplete is None:
            return
        ids, self._incomplete = {i for i in self._incomplete if i}, set()
        try:
            await asyncio.wait_for(self.on_incomplete(ids), limits.POSTGRES_CHECKPOINTER_TIMEOUT_S)
        except Exception:  # noqa: BLE001
            log.exception("events_complete=false could not be recorded turns=%d", len(ids))

    async def _loop(self) -> None:
        """What: flush every flush_interval_s. Use when: started by start(). Inputs: none. Output: none.
        Side effects: flushes. Role: background task."""
        while True:
            await asyncio.sleep(self.flush_interval_s)
            await self.flush()

    async def start(self) -> None:
        """What: open the pool without waiting and start the loop. Use when: app lifespan, tests.
        Inputs: none. Output: none. Side effects: pool open (background connects), one task. Role: lifecycle."""
        if self.pool is not None and hasattr(self.pool, "open"):
            await self.pool.open(wait=False)
        if self._task is None:
            self._task = asyncio.create_task(self._loop(), name="trajectory-writer")

    async def aclose(self) -> None:
        """What: stop the loop, flush what is left (bounded per batch) and close the pool.
        Use when: app shutdown, tests. Inputs: none. Output: none. Side effects: final flush, pool close.
        Role: lifecycle; a stuck database costs at most timeout_s per batch."""
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._task = None
        await self.flush()
        if self.pool is not None and hasattr(self.pool, "close"):
            try:
                await self.pool.close()
            except Exception:  # noqa: BLE001
                log.exception("trajectory pool close failed")
