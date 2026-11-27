"""What: tests for the ingress core path (doc 07 s2.3, s2.4 steps 1 to 9, 15, 16, s8.1; CS-E-03.S7.T3).
Use when: pytest (in-memory store and lock, fake agent body). Inputs: none. Output: pass/fail.
Side effects: none. Role: Done when: replay gives replayed true; a concurrent turn gets 409
TURN_IN_PROGRESS; the 41st turn gets the cap line with no model call."""
from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from contracts.events import KEEPALIVE, parse_event
from agent.ingress import (
    CAP_LINE,
    Ingress,
    IngressError,
    IngressInput,
    IngressStream,
    RedisTurnLock,
    SessionRow,
    TurnRow,
    parse_turn_request,
)
from agent.sse import TurnOutcome, TurnStream
from ingress_fakes import MemoryTurnLock, MemoryTurnStore

SID, VISITOR = "019a7c2e-5b10-7000-8000-00000000a001", "visitor-1"


class Agent:
    """What: fake agent body that records calls and can wait on a gate. Use when: tests.
    Inputs: reply text, optional gate, optional sleep. Output: TurnOutcome. Side effects: records. Role: fake."""

    def __init__(self, text: str = "Here you go.", gate: asyncio.Event | None = None, sleep_s: float = 0) -> None:
        """What: configure. Use when: tests. Inputs: see class. Output: none. Side effects: none. Role: fake."""
        self.text, self.gate, self.sleep_s, self.calls = text, gate, sleep_s, []

    async def __call__(self, stream: TurnStream, inp: IngressInput, row: TurnRow) -> TurnOutcome:
        """What: run. Use when: ingress step 14. Inputs: stream, input, row. Output: outcome.
        Side effects: records the call. Role: fake."""
        self.calls.append((inp.client_turn_id, row.turn_id))
        if self.gate:
            await self.gate.wait()
        if self.sleep_s:
            await asyncio.sleep(self.sleep_s)
        stream.emit("token", text=self.text)
        return TurnOutcome.fixed(self.text, end_reason="answered")


def make(user_turns: int = 0, agent: Agent | None = None, lock: MemoryTurnLock | None = None,
         **kw: Any) -> tuple[Ingress, MemoryTurnStore, MemoryTurnLock, Agent]:
    """What: build an Ingress over fakes. Use when: tests. Inputs: session turn count, fakes.
    Output: (ingress, store, lock, agent). Side effects: none. Role: helper."""
    store = MemoryTurnStore(SessionRow(session_id=SID, visitor_id=VISITOR, user_turns=user_turns))
    lock = lock or MemoryTurnLock()
    agent = agent or Agent()
    return Ingress(store, lock, served_by="agent_a", run_agent=agent, **kw), store, lock, agent


def inp(client_turn_id: str | None = None, text: str = "hello", mode: str = "chat", **kw: Any) -> IngressInput:
    """What: a text turn input. Use when: tests. Inputs: ids, text, mode. Output: IngressInput.
    Side effects: none. Role: helper."""
    return IngressInput(session_id=SID, visitor_id=VISITOR, client_turn_id=client_turn_id or str(uuid.uuid4()),
                        mode=mode, input_kind="voice" if mode == "voice" else "text", text=text, **kw)


async def drain(result: IngressStream) -> list[Any]:
    """What: read every frame, await the task, parse events. Use when: tests. Inputs: stream result.
    Output: events. Side effects: none. Role: helper."""
    frames = [f async for f in result.stream.frames()]
    if result.task is not None:
        await result.task
    return [parse_event(f.split("\n")[2][6:]) for f in frames if f != KEEPALIVE]


async def test_normal_turn_writes_running_row_then_final_row_and_releases_the_lock():
    ing, store, lock, agent = make()
    before = datetime.now(UTC)
    res = await ing.handle(inp("c-1"))
    assert isinstance(res, IngressStream)
    row = next(iter(store.turns.values()))
    assert row.status == "running" and row.input_kind == "text" and row.mode == "chat"
    assert timedelta(seconds=44) < row.deadline_at - before <= timedelta(seconds=46)
    events = await drain(res)
    assert [e.type for e in events] == ["turn.started", "token", "turn.completed"]
    assert events[0].turn_id == row.turn_id == events[0].trace_id
    assert uuid.UUID(row.turn_id).version == 7
    assert store.turns[row.turn_id].status == "completed" and store.turns[row.turn_id].end_reason == "answered"
    assert store.sessions[SID].user_turns == 1 and lock.held == {} and len(agent.calls) == 1
    assert lock.log[0] == ("acquire", 60_000)


async def test_voice_deadline_is_20_seconds():
    ing, store, _, _ = make()
    before = datetime.now(UTC)
    res = await ing.handle(inp(mode="voice", realtime_call_id="call_1"))
    row = next(iter(store.turns.values()))
    assert timedelta(seconds=19) < row.deadline_at - before <= timedelta(seconds=21)
    assert row.realtime_call_id == "call_1"
    await drain(res)


async def test_replay_of_a_completed_turn_returns_replayed_true_without_a_model_call():
    ing, store, lock, agent = make()
    first = await drain(await ing.handle(inp("c-1")))
    res = await ing.handle(inp("c-1"))
    events = await drain(res)
    assert [e.type for e in events] == ["turn.completed"]
    done = events[0]
    assert done.replayed is True and done.turn_id == first[-1].turn_id
    assert done.final == first[-1].final and done.end_reason == "answered"
    assert len(agent.calls) == 1 and len(store.turns) == 1 and store.sessions[SID].user_turns == 1
    assert [x for x in lock.log if x[0] == "acquire"] == [("acquire", 60_000)]  # replay takes no lock


async def test_replay_of_a_failed_turn_replays_error_then_turn_completed():
    ing, store, _, _ = make(agent=Agent(sleep_s=0.3), turn_deadline_s={"chat": 0.05, "voice": 0.05},
                            reserve_s=0)
    first = await drain(await ing.handle(inp("c-1")))
    assert [e.type for e in first][-2:] == ["error", "turn.completed"]
    events = await drain(await ing.handle(inp("c-1")))
    assert [e.type for e in events] == ["error", "turn.completed"]
    assert events[0].code == "TURN_DEADLINE" and events[1].replayed is True


async def test_voice_replay_by_realtime_call_id():
    ing, _, _, agent = make()
    await drain(await ing.handle(inp("c-1", mode="voice", realtime_call_id="call_9")))
    events = await drain(await ing.handle(inp("c-2", mode="voice", realtime_call_id="call_9")))
    assert events[-1].replayed is True and len(agent.calls) == 1


async def test_same_client_turn_id_while_running_is_409():
    gate = asyncio.Event()
    ing, _, _, _ = make(agent=Agent(gate=gate))
    first = await ing.handle(inp("c-1"))
    second = await ing.handle(inp("c-1"))
    assert isinstance(second, IngressError) and (second.status, second.code) == (409, "TURN_IN_PROGRESS")
    gate.set()
    await drain(first)


async def test_concurrent_turn_with_another_client_turn_id_gets_409_turn_in_progress():
    gate = asyncio.Event()
    ing, store, lock, agent = make(agent=Agent(gate=gate))
    first = await ing.handle(inp("c-1"))
    second = await ing.handle(inp("c-2"))
    assert isinstance(second, IngressError)
    assert (second.status, second.class_, second.code, second.retryable) == (409, "core_conflict",
                                                                            "TURN_IN_PROGRESS", False)
    assert len(store.turns) == 1  # the refused turn wrote nothing
    gate.set()
    await drain(first)
    third = await ing.handle(inp("c-3"))  # lock released at the end of the first
    assert isinstance(third, IngressStream)
    await drain(third)
    assert len(agent.calls) == 2


async def test_the_41st_turn_gets_the_cap_line_with_no_model_call():
    ing, store, lock, agent = make(user_turns=40)
    res = await ing.handle(inp("c-41"))
    events = await drain(res)
    assert [e.type for e in events] == ["turn.started", "turn.completed"]
    assert events[-1].final.text == CAP_LINE and events[-1].end_reason == "cap_reached"
    assert agent.calls == [] and store.sessions[SID].user_turns == 40  # not counted
    assert lock.held == {}


async def test_the_40th_turn_still_runs():
    ing, store, _, agent = make(user_turns=39)
    await drain(await ing.handle(inp()))
    assert len(agent.calls) == 1 and store.sessions[SID].user_turns == 40


@pytest.mark.parametrize("sid,visitor", [("019a7c2e-0000-7000-8000-000000000000", VISITOR), (SID, "someone-else")])
async def test_unknown_or_foreign_session_is_404(sid, visitor):
    ing, _, _, _ = make()
    res = await ing.handle(IngressInput(session_id=sid, visitor_id=visitor, client_turn_id="c", mode="chat",
                                        input_kind="text", text="hi"))
    assert isinstance(res, IngressError) and (res.status, res.code, res.class_) == (404, "NOT_FOUND", "not_found")


async def test_sweep_marks_a_stale_running_turn_before_the_new_turn():
    ing, store, _, _ = make()
    stale = TurnRow(turn_id="019a7c2e-0000-7000-8000-0000000000aa", session_id=SID, client_turn_id="old",
                    mode="chat", input_kind="text", status="running",
                    deadline_at=datetime.now(UTC) - timedelta(seconds=11), started_at=datetime.now(UTC))
    store.turns[stale.turn_id] = stale
    await drain(await ing.handle(inp("new")))
    assert store.turns[stale.turn_id].status == "failed" and store.turns[stale.turn_id].end_reason == "swept"
    assert store.swept_calls == [SID]


async def test_deadline_gives_error_event_and_failed_row():
    ing, store, lock, _ = make(agent=Agent(sleep_s=0.3), turn_deadline_s={"chat": 0.05, "voice": 0.05}, reserve_s=0)
    events = await drain(await ing.handle(inp("c-1")))
    assert [e.type for e in events] == ["turn.started", "error", "turn.completed"]
    assert events[1].class_ == "turn_deadline_exceeded" and events[-1].end_reason == "deadline"
    row = next(iter(store.turns.values()))
    assert row.status == "failed" and row.error["code"] == "TURN_DEADLINE" and lock.held == {}


async def test_redis_down_fails_closed_with_503():
    ing, store, _, agent = make(lock=MemoryTurnLock(fail=ConnectionError("redis down")))
    res = await ing.handle(inp())
    assert isinstance(res, IngressError)
    assert (res.status, res.code, res.class_, res.retryable) == (503, "REDIS_UNAVAILABLE", "redis_unavailable", True)
    assert store.turns == {} and agent.calls == []


async def test_a_failed_final_row_write_still_completes_the_stream_and_releases():
    ing, store, lock, _ = make()

    async def broken(*a: Any) -> None:
        """What: fails. Use when: this test. Inputs: any. Output: none. Side effects: raises. Role: fake."""
        raise ConnectionError("db down")

    store.finish_turn = broken  # type: ignore[method-assign]
    events = await drain(await ing.handle(inp()))
    assert events[-1].type == "turn.completed" and lock.held == {}


def test_parse_turn_request_needs_exactly_one_input():
    ok = parse_turn_request({"client_turn_id": str(uuid.uuid4()), "text": "hi"})
    assert not isinstance(ok, IngressError) and ok.text == "hi"
    for bad in ({"client_turn_id": str(uuid.uuid4())},
                {"client_turn_id": str(uuid.uuid4()), "text": "hi", "ticket_offer": {"accept": True}},
                {"client_turn_id": str(uuid.uuid4()), "text": "x" * 2001},
                {"client_turn_id": "nope", "text": "hi"}):
        err = parse_turn_request(bad)
        assert isinstance(err, IngressError) and (err.status, err.code) == (422, "VALIDATION_ERROR")


class FakeRedis:
    """What: minimal async Redis for RedisTurnLock (SET NX PX and the compare-and-delete script).
    Use when: lock unit test. Inputs: none. Output: set, eval. Side effects: dict. Role: fake."""

    def __init__(self) -> None:
        """What: empty. Use when: tests. Inputs: none. Output: none. Side effects: none. Role: fake."""
        self.kv: dict[str, str] = {}
        self.calls: list[Any] = []

    async def set(self, key: str, value: str, nx: bool = False, px: int | None = None) -> bool | None:
        """What: SET. Use when: acquire. Inputs: key, value, flags. Output: True or None. Side effects: dict. Role: fake."""
        self.calls.append(("set", key, nx, px))
        if nx and key in self.kv:
            return None
        self.kv[key] = value
        return True

    async def eval(self, script: str, numkeys: int, key: str, token: str) -> int:
        """What: emulate the release script. Use when: release. Inputs: script, key, token. Output: 1 or 0.
        Side effects: dict. Role: fake."""
        assert "redis.call('get', KEYS[1]) == ARGV[1]" in script and numkeys == 1
        if self.kv.get(key) == token:
            del self.kv[key]
            return 1
        return 0


async def test_redis_lock_uses_the_doc07_key_ttl_and_owner_token():
    r = FakeRedis()
    lock = RedisTurnLock(r)
    assert await lock.acquire(SID, "tok-a", 60_000) is True
    assert r.calls[0] == ("set", f"lock:turn:{SID}", True, 60_000)
    assert await lock.acquire(SID, "tok-b", 60_000) is False
    assert await lock.release(SID, "tok-b") is False and f"lock:turn:{SID}" in r.kv
    assert await lock.release(SID, "tok-a") is True and r.kv == {}
