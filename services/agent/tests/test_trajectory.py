"""What: tests for the TrajectoryWriter (doc 07 s6.1, s12 turn_events) and turn Usage (s6.2) of CS-E-03.S8.T8.
Use when: pytest (fakes, plus one psycopg pool pointed at a closed local port; no provider call).
Inputs: none. Output: pass/fail. Side effects: opens a socket briefly to find a free port.
Role: Done when part 1: with the writer pointed at a closed port the turn still completes with
events_complete=false. Part 2 (usage.model_calls equals the LangSmith llm run count) is test_real_trajectory.py."""
from __future__ import annotations

import asyncio
import socket
import time
import uuid
from pathlib import Path
from typing import Any

from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.checkpoint.memory import InMemorySaver

from agent.assembly import build_agent, turn_run_config
from agent.ingress import Ingress, IngressInput, IngressStream, SessionRow, TurnRow
from agent.models import ModelRegistry, load_models_config
from agent.models.usage import token_usage
from agent.sse import TurnMeta, TurnOutcome, TurnStream, stream_agent_turn
from agent.trajectory import INSERT_SQL, TrajectoryWriter, event_row
from ingress_fakes import MemoryTurnLock, MemoryTurnStore
from pin_probe import leaks

ROOT = Path(__file__).resolve().parents[3]
SID, VISITOR = "019a7c2e-5b10-7000-8000-00000000a001", "visitor-1"
TID = "019a7c2e-5b20-7000-8000-0000000000c1"


def meta() -> TurnMeta:
    """What: fixed turn metadata. Use when: tests. Inputs: none. Output: TurnMeta. Side effects: none. Role: helper."""
    return TurnMeta(session_id=SID, turn_id=TID, mode="chat", served_by="agent_a", input_kind="text")


class FakePool:
    """What: psycopg-pool look-alike recording executemany rows, optionally failing. Use when: writer tests.
    Inputs: fail (exception to raise on connection). Output: connection() context. Side effects: records.
    Role: test double for the writer's own pool."""

    def __init__(self, fail: Exception | None = None) -> None:
        """What: configure. Use when: tests. Inputs: fail. Output: none. Side effects: none. Role: fake."""
        self.fail, self.batches, self.closed = fail, [], False

    def connection(self) -> "FakePool":
        """What: the connection context. Use when: writer flush. Inputs: none. Output: self.
        Side effects: none. Role: fake."""
        return self

    async def __aenter__(self) -> "FakePool":
        if self.fail:
            raise self.fail
        return self

    async def __aexit__(self, *exc: Any) -> None:
        return None

    def cursor(self) -> "FakePool":
        """What: cursor context. Use when: flush. Inputs: none. Output: self. Side effects: none. Role: fake."""
        return self

    async def executemany(self, sql: str, rows: list[tuple[Any, ...]]) -> None:
        """What: record a batch. Use when: flush. Inputs: sql, rows. Output: none. Side effects: records. Role: fake."""
        assert sql == INSERT_SQL
        self.batches.append(list(rows))

    async def open(self, wait: bool = False) -> None:
        """What: no-op open. Use when: start. Inputs: wait. Output: none. Side effects: none. Role: fake."""

    async def close(self) -> None:
        """What: mark closed. Use when: aclose. Inputs: none. Output: none. Side effects: flag. Role: fake."""
        self.closed = True


class Incomplete:
    """What: records on_incomplete calls. Use when: writer tests. Inputs: none. Output: callable.
    Side effects: records. Role: fake of PgTurnStore.mark_events_incomplete."""

    def __init__(self) -> None:
        """What: empty record. Use when: tests. Inputs: none. Output: none. Side effects: none. Role: fake."""
        self.turn_ids: set[str] = set()

    async def __call__(self, turn_ids: set[str]) -> None:
        self.turn_ids |= set(turn_ids)


def closed_port() -> int:
    """What: a local TCP port with nothing listening. Use when: the closed-port test. Inputs: none.
    Output: port number. Side effects: binds and closes a socket. Role: helper."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _freeze_sse_clock(monkeypatch, microsecond: int) -> None:
    """What: pin agent.sse's datetime.now to 2026-11-27T15:22:04.<microsecond>Z. Use when: a test must not depend on
    the wall clock. Inputs: monkeypatch, microsecond. Output: none. Side effects: patches agent.sse.datetime.
    Role: deterministic ts (a real microsecond like .482100 contains the PIN digits by chance)."""
    import agent.sse as sse_mod
    from datetime import datetime as real

    class Frozen(real):
        """What: datetime with a fixed now(). Use when: _freeze_sse_clock. Inputs: tz. Output: fixed datetime.
        Side effects: none. Role: clock stub."""

        @classmethod
        def now(cls, tz=None):
            """What: the frozen instant. Use when: TurnStream.emit. Inputs: tz. Output: datetime. Side effects: none. Role: stub."""
            return real(2026, 11, 27, 15, 22, 4, microsecond, tzinfo=tz)

    monkeypatch.setattr(sse_mod, "datetime", Frozen)


def test_event_row_maps_columns_and_redacts_the_payload(monkeypatch):
    _freeze_sse_clock(monkeypatch, 482100)  # the unlucky microsecond: ts must not matter to the PIN probe
    s = TurnStream(meta())
    ev = s.emit("step.started", step_id=f"{TID}:1", kind="tool", name="get_account_summary", label="Loading",
                args_redacted={"email": "pat.lee@acme.com", "pin": "4821"})
    raw = ev.model_dump(mode="json", by_alias=True)
    assert raw["args_redacted"]["email"] == "pat.lee@acme.com" and raw["args_redacted"]["pin"] == "4821"
    turn_id, seq, session_id, ts, type_, step_id, payload = event_row(ev)
    assert (turn_id, seq, session_id, type_, step_id) == (TID, 1, SID, "step.started", f"{TID}:1")
    assert ts == raw["ts"]
    body = payload.obj  # psycopg Jsonb wrapper
    assert body["args_redacted"] == {"email": "p***@acme.com", "pin": "[PIN]"}
    assert "pat.lee" not in str(body) and not leaks(str(body["args_redacted"]), "4821")
    assert "482100" in body["ts"]  # the frozen unlucky timestamp is in the row, and the PIN probe ignores it


async def test_events_reach_the_writer_in_one_batch_and_tokens_never_do():
    pool = FakePool()
    w = TrajectoryWriter(pool=pool)
    s = TurnStream(meta(), sink=w.add)
    s.emit("turn.started", served_by="agent_a", input_kind="text", contract_version="x")
    s.emit("token", text="hello")
    s.emit("status", kind="slow", message="slow")
    await w.flush()
    assert len(pool.batches) == 1
    assert [r[4] for r in pool.batches[0]] == ["turn.started", "status"]
    assert [r[1] for r in pool.batches[0]] == [1, 3]


async def test_a_failed_batch_is_dropped_and_its_turns_marked_incomplete():
    marks = Incomplete()
    w = TrajectoryWriter(pool=FakePool(fail=OSError("connection refused")), on_incomplete=marks)
    s = TurnStream(meta(), sink=w.add)
    s.emit("status", kind="slow", message="slow")
    await w.flush()  # never raises
    assert marks.turn_ids == {TID}
    assert w.dropped == 1 and w.pending() == 0


async def test_a_full_buffer_drops_new_rows_and_marks_the_turn_incomplete():
    marks = Incomplete()
    w = TrajectoryWriter(pool=FakePool(), on_incomplete=marks, buffer_max=2)
    s = TurnStream(meta(), sink=w.add)
    for _ in range(3):
        s.emit("status", kind="slow", message="slow")
    assert w.pending() == 2 and w.dropped == 1
    await w.flush()
    assert marks.turn_ids == {TID}


async def test_a_sink_that_raises_never_breaks_the_stream():
    def boom(_: Any) -> None:
        raise RuntimeError("sink down")

    s = TurnStream(meta(), sink=boom)
    s.emit("status", kind="slow", message="slow")
    assert s.seq == 1 and len(s.events) == 1


async def test_background_loop_flushes_without_an_explicit_call_and_close_drains():
    pool = FakePool()
    w = TrajectoryWriter(pool=pool, flush_interval_s=0.01)
    await w.start()
    s = TurnStream(meta(), sink=w.add)
    s.emit("status", kind="slow", message="slow")
    for _ in range(100):
        if pool.batches:
            break
        await asyncio.sleep(0.01)
    assert pool.batches, "the loop must flush on its own"
    s.emit("status", kind="slow", message="again")
    await w.aclose()
    assert sum(len(b) for b in pool.batches) == 2 and pool.closed


async def test_turn_completes_with_events_complete_false_when_the_writer_points_at_a_closed_port():
    """Done when (CS-E-03.S8.T8): a real psycopg pool pointed at a closed port; the turn is not delayed and the
    row ends with events_complete=false."""
    dsn = f"postgresql://agent_app:x@127.0.0.1:{closed_port()}/quillstack"
    store = MemoryTurnStore(SessionRow(session_id=SID, visitor_id=VISITOR))
    writer = TrajectoryWriter(dsn, on_incomplete=store.mark_events_incomplete, timeout_s=0.5, flush_interval_s=0.05)
    await writer.start()

    async def body(stream: TurnStream, inp: IngressInput, row: TurnRow) -> TurnOutcome:
        stream.emit("status", kind="checking_policy", message="Checking")
        return TurnOutcome.fixed("Done.", end_reason="answered")

    ingress = Ingress(store, MemoryTurnLock(), served_by="agent_a", run_agent=body, trajectory=writer)
    t0 = time.monotonic()
    res = await ingress.handle(IngressInput(session_id=SID, visitor_id=VISITOR, client_turn_id=str(uuid.uuid4()),
                                            mode="chat", input_kind="text", text="hi"))
    assert isinstance(res, IngressStream) and res.task is not None
    frames = [f async for f in res.stream.frames()]
    elapsed = time.monotonic() - t0
    await res.task
    assert frames[-1].startswith("event: turn.completed")
    assert elapsed < 0.4, f"the writer must never delay the turn (took {elapsed:.2f} s)"
    turn_id = res.stream.meta.turn_id
    assert store.turns[turn_id].status == "completed"
    assert turn_id not in store.events_incomplete  # zero proven possible: nothing marked yet
    await writer.aclose()
    assert turn_id in store.events_incomplete, "events_complete must be false after the dropped batch"
    assert writer.dropped >= 3  # turn.started, status, turn.completed


def test_turn_run_config_uses_the_turn_id_as_the_langsmith_run_id():
    cfg = turn_run_config(SID, TID)
    assert cfg["run_id"] == uuid.UUID(TID)
    assert cfg["configurable"] == {"thread_id": SID}


def test_cost_for_model_matches_the_role_by_returned_model_name():
    reg = ModelRegistry(load_models_config(ROOT / "data" / "models.yaml"))
    u = token_usage(1_000_000, 1_000_000)
    assert reg.cost_for_model("claude-sonnet-5-5", u) == reg.cost("agent", u)
    assert reg.cost_for_model("gpt-5.4-mini-2026-03-17", u) == reg.cost("agent_fallback", u)
    assert reg.cost_for_model("unknown-model", u) == 0.0


class Fake(GenericFakeChatModel):
    """What: scripted non-streaming fake. Use when: usage tests. Inputs: messages. Output: replies.
    Side effects: none. Role: test double."""

    def bind_tools(self, tools: Any, **kw: Any) -> "Fake":
        """What: no-op. Use when: create_agent binds tools. Inputs: tools. Output: self. Side effects: none.
        Role: fake."""
        return self


async def test_turn_usage_carries_cost_from_models_yaml_prices_and_timings():
    reg = ModelRegistry(load_models_config(ROOT / "data" / "models.yaml"))
    reply = AIMessage("Here you go.", usage_metadata={"input_tokens": 1200, "output_tokens": 30, "total_tokens": 1230,
                                                      "input_token_details": {"cache_read": 1000}},
                      response_metadata={"model_name": "claude-sonnet-5-5"})
    fake = Fake(messages=iter([reply]), disable_streaming=True)
    agent = build_agent(fake, fallback_model=Fake(messages=iter([])), checkpointer=InMemorySaver())
    s = TurnStream(meta())
    out = await stream_agent_turn(s, agent, {"messages": [HumanMessage("hi")]}, turn_run_config(SID, TID),
                                  cost_of=reg.cost_for_model)
    u = out.usage
    assert (u.input_tokens, u.output_tokens, u.cache_read_tokens) == (1200, 30, 1000)
    expected = (200 * 2.00 + 1000 * 0.10 + 30 * 10.00) / 1_000_000
    assert abs(u.cost_usd_est - expected) < 1e-12 and u.cost_usd_est > 0
    assert u.model_calls == 1 and u.models_used == ["claude-sonnet-5-5"] and u.fallback_used is False


class RejectingPool(FakePool):
    """What: a pool whose server rejects any batch holding a row of one bad turn (an FK violation).
    Use when: per-turn isolation test. Inputs: bad turn id. Output: as FakePool. Side effects: records.
    Role: fake of a server-side error (psycopg DatabaseError) for one turn only."""

    def __init__(self, bad: str) -> None:
        """What: configure. Use when: tests. Inputs: bad turn id. Output: none. Side effects: none. Role: fake."""
        super().__init__()
        self.bad = bad

    async def executemany(self, sql: str, rows: list[tuple[Any, ...]]) -> None:
        import psycopg
        if any(r[0] == self.bad for r in rows):
            raise psycopg.errors.ForeignKeyViolation("turn row missing")
        await super().executemany(sql, rows)


async def test_a_server_rejected_batch_is_retried_per_turn_so_one_bad_turn_drops_only_its_own_rows():
    bad = "019a7c2e-5b20-7000-8000-0000000000ff"
    marks = Incomplete()
    pool = RejectingPool(bad)
    w = TrajectoryWriter(pool=pool, on_incomplete=marks)
    good_s = TurnStream(meta(), sink=w.add)
    bad_s = TurnStream(TurnMeta(session_id=SID, turn_id=bad, mode="chat", served_by="a", input_kind="text"), sink=w.add)
    good_s.emit("status", kind="slow", message="slow")
    bad_s.emit("status", kind="slow", message="slow")
    await w.flush()
    assert [r[0] for b in pool.batches for r in b] == [TID]
    assert marks.turn_ids == {bad} and w.written == 1 and w.dropped == 1


async def test_an_unreachable_database_drops_the_batch_without_per_turn_retries():
    import psycopg_pool
    marks = Incomplete()
    calls = {"n": 0}

    class Down(FakePool):
        async def __aenter__(self) -> "FakePool":
            calls["n"] += 1
            raise psycopg_pool.PoolTimeout("no connection")

    w = TrajectoryWriter(pool=Down(), on_incomplete=marks)
    TurnStream(meta(), sink=w.add).emit("status", kind="slow", message="a")
    TurnStream(TurnMeta(SID, "019a7c2e-5b20-7000-8000-0000000000ee", "chat", "a", "text"), sink=w.add).emit(
        "status", kind="slow", message="b")
    await w.flush()
    assert calls["n"] == 1 and w.dropped == 2 and len(marks.turn_ids) == 2


class FlakyMark(Incomplete):
    """What: on_incomplete that fails the first N calls (a database blip on the mark). Use when: retry tests.
    Inputs: failures before success. Output: callable. Side effects: records. Role: fake."""

    def __init__(self, failures: int) -> None:
        """What: set the failure count. Use when: tests. Inputs: int. Output: none. Side effects: none. Role: fake."""
        super().__init__()
        self.failures, self.calls = failures, 0

    async def __call__(self, turn_ids: set[str]) -> None:
        self.calls += 1
        if self.calls <= self.failures:
            raise OSError("database blip")
        await super().__call__(turn_ids)


async def test_a_failed_events_incomplete_mark_is_retried_on_the_next_flush():
    marks = FlakyMark(failures=1)
    w = TrajectoryWriter(pool=FakePool(fail=OSError("connection refused")), on_incomplete=marks)
    TurnStream(meta(), sink=w.add).emit("status", kind="slow", message="slow")
    await w.flush()
    assert marks.turn_ids == set()  # the mark failed once, not yet recorded
    await w.flush()
    assert marks.turn_ids == {TID}  # retried, not lost


async def test_close_retries_a_failed_mark_before_giving_up():
    marks = FlakyMark(failures=1)
    w = TrajectoryWriter(pool=FakePool(fail=OSError("connection refused")), on_incomplete=marks)
    TurnStream(meta(), sink=w.add).emit("status", kind="slow", message="slow")
    await w.aclose()
    assert marks.turn_ids == {TID}


async def test_pending_incomplete_marks_are_capped():
    marks = FlakyMark(failures=10 ** 6)
    w = TrajectoryWriter(pool=FakePool(), on_incomplete=marks)
    from agent import trajectory as t
    w._incomplete = {str(i + 1) for i in range(t.INCOMPLETE_CAP + 50)}
    await w.flush()
    assert len(w._incomplete) <= t.INCOMPLETE_CAP


def test_role_match_is_exact_or_dated_suffix_and_an_unknown_model_warns_once(caplog):
    reg = ModelRegistry(load_models_config(ROOT / "data" / "models.yaml"))
    assert reg.role_for_model("claude-sonnet-5-55") is None
    assert reg.role_for_model("claude-sonnet-5-5") == "agent"
    u = token_usage(10, 10)
    with caplog.at_level("WARNING"):
        reg.cost_for_model("mystery-model", u)
        reg.cost_for_model("mystery-model", u)
    assert [r.getMessage() for r in caplog.records].count("unknown model cost=0 model=mystery-model") == 1
