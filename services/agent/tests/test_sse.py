"""What: tests for the SSE adapter (doc 07 s6.1, s6.2, s6.4, s8.1; CS-E-03.S8.T7).
Use when: pytest (fake models, no provider). Inputs: none. Output: pass/fail. Side effects: none.
Role: every emitted frame is parsed back with the contracts models (fixture format)."""
from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.tools import tool
from langgraph.checkpoint.memory import InMemorySaver

from contracts.events import KEEPALIVE, TurnCompletedEvent, parse_event
from agent.assembly import build_agent, run_config
from agent.sse import (
    LIMIT_PREFIX,
    TurnMeta,
    TurnOutcome,
    TurnStream,
    chunk_text,
    start_turn,
    stream_agent_turn,
)

SID, TID = "019a7c2e-5b10-7000-8000-00000000a001", "019a7c2e-5b20-7000-8000-0000000000c1"


def meta(mode: str = "chat") -> TurnMeta:
    """What: fixed turn metadata. Use when: tests. Inputs: mode. Output: TurnMeta. Side effects: none. Role: helper."""
    return TurnMeta(session_id=SID, turn_id=TID, mode=mode, served_by="agent_a", input_kind="text")


class Fake(GenericFakeChatModel):
    """What: scripted fake model; optional per-call delay. Use when: tests. Inputs: messages, delay_s.
    Output: scripted replies. Side effects: sleeps when delay_s. Role: test double."""

    delay_s: float = 0.0

    def bind_tools(self, tools: Any, **kw: Any) -> "Fake":
        """What: no-op. Use when: create_agent binds tools. Inputs: tools. Output: self. Side effects: none. Role: fake."""
        return self

    async def _agenerate(self, messages, stop=None, run_manager=None, **kw):  # type: ignore[override]
        """What: optional delay then the next reply. Use when: model call. Inputs: messages.
        Output: ChatResult. Side effects: sleep. Role: fake."""
        if self.delay_s:
            await asyncio.sleep(self.delay_s)
        return self._generate(messages, stop=stop, run_manager=None, **kw)


def fake(*replies: AIMessage, delay_s: float = 0.0) -> Fake:
    """What: non-streaming fake (tool-call replies cannot stream from GenericFakeChatModel).
    Use when: tests. Inputs: replies, delay. Output: Fake. Side effects: none. Role: helper."""
    return Fake(messages=iter(replies), disable_streaming=True, delay_s=delay_s)


@tool
def list_tickets() -> str:
    """List the customer's tickets."""
    return "[]"


async def collect(stream: TurnStream) -> list[str]:
    """What: drain frames. Use when: tests. Inputs: stream. Output: frames. Side effects: none. Role: helper."""
    return [f async for f in stream.frames()]


def parse_frames(frames: list[str]) -> list[Any]:
    """What: parse SSE frames into contracts events, checking the wire format of each.
    Use when: tests. Inputs: frames. Output: events (keepalives skipped). Side effects: none. Role: validator."""
    out = []
    for f in frames:
        if f == KEEPALIVE:
            continue
        lines = f.split("\n")
        assert f.endswith("\n\n") and lines[0].startswith("event: ") and lines[1].startswith("id: ")
        assert lines[2].startswith("data: ")
        ev: Any = parse_event(lines[2][len("data: "):])
        assert lines[0] == f"event: {ev.type}" and lines[1] == f"id: {ev.seq}"
        out.append(ev)
    return out


def check_turn(events: list[Any]) -> None:
    """What: invariants of one turn: seq 1..n, envelope, first turn.started, one turn.completed last.
    Use when: every stream test. Inputs: events. Output: none (asserts). Side effects: none. Role: checker."""
    assert [e.seq for e in events] == list(range(1, len(events) + 1))
    assert all((e.session_id, e.turn_id, e.trace_id, e.v) == (SID, TID, TID, 1) for e in events)
    assert events[0].type == "turn.started"
    assert [e.type for e in events].count("turn.completed") == 1 and events[-1].type == "turn.completed"


async def run_agent(agent: Any, text: str = "hi", **kw: Any) -> tuple[list[Any], TurnOutcome, list[TurnOutcome]]:
    """What: run one agent turn through start_turn and the agent body. Use when: tests.
    Inputs: agent, text, adapter kwargs. Output: (events, outcome, written rows). Side effects: none. Role: helper."""
    stream = TurnStream(meta())
    written: list[TurnOutcome] = []

    async def body(s: TurnStream) -> TurnOutcome:
        """What: agent body. Use when: start_turn. Inputs: stream. Output: outcome. Side effects: none. Role: helper."""
        return await stream_agent_turn(s, agent, {"messages": [HumanMessage(text)]}, run_config(SID), **kw)

    async def write_final(o: TurnOutcome) -> None:
        """What: record. Use when: start_turn. Inputs: outcome. Output: none. Side effects: appends. Role: fake."""
        written.append(o)

    task = start_turn(stream, body, write_final=write_final)
    frames = await collect(stream)
    outcome = await task
    return parse_frames(frames), outcome, written


async def test_frames_match_the_contract_and_the_draft_flushes_in_40_char_tokens():
    answer = "You have no open tickets right now. Anything else I can help you with today?"
    agent = build_agent(fake(AIMessage("", tool_calls=[{"name": "list_tickets", "args": {}, "id": "c1",
                                                         "type": "tool_call"}]), AIMessage(answer)),
                        fallback_model=fake(AIMessage("fb")), checkpointer=InMemorySaver(), tools=[list_tickets])
    events, outcome, written = await run_agent(agent)
    check_turn(events)
    tokens = [e.text for e in events if e.type == "token"]
    assert "".join(tokens) == answer and [len(t) for t in tokens] == [40, len(answer) - 40]
    steps = [(e.type, getattr(e, "kind", None)) for e in events if e.type.startswith("step.")]
    assert steps == [("step.started", "model"), ("step.completed", None)] * 2
    ids = [e.step_id.rsplit(":", 1)[-1] for e in events if e.type.startswith("step.")]
    assert ids == ["1", "1", "2", "2"], ids  # review PR #17 M1: no skipped step numbers
    done = events[-1]
    assert isinstance(done, TurnCompletedEvent) and done.final.text == answer and done.end_reason == "answered"
    assert done.usage.model_calls == 2 and done.usage.tool_calls == 1 and done.replayed is False
    assert done.usage.ttft_ms is not None and done.usage.first_status_ms is not None
    assert written == [outcome]


async def test_a_tool_call_message_never_streams_as_tokens():
    agent = build_agent(fake(AIMessage("let me look", tool_calls=[{"name": "list_tickets", "args": {}, "id": "c1",
                                                                   "type": "tool_call"}]), AIMessage("Done.")),
                        fallback_model=fake(AIMessage("fb")), checkpointer=InMemorySaver(), tools=[list_tickets])
    events, _, _ = await run_agent(agent)
    assert "".join(e.text for e in events if e.type == "token") == "Done."


async def test_status_slow_once_when_no_chunk_within_the_threshold():
    agent = build_agent(fake(AIMessage("Slow but fine."), delay_s=0.3), fallback_model=fake(AIMessage("fb")),
                        checkpointer=InMemorySaver())
    events, _, _ = await run_agent(agent, slow_after_s=0.1)
    check_turn(events)
    slow = [e for e in events if e.type == "status"]
    assert [(e.kind) for e in slow] == ["slow"]
    types = [e.type for e in events]
    assert types.index("status") < types.index("step.completed")


async def test_no_slow_status_for_a_fast_model():
    agent = build_agent(fake(AIMessage("Quick.")), fallback_model=fake(AIMessage("fb")), checkpointer=InMemorySaver())
    events, _, _ = await run_agent(agent, slow_after_s=0.2)
    assert not [e for e in events if e.type == "status"]


async def test_model_call_limit_line_is_limit_reached_not_an_answer():
    agent = build_agent(fake(AIMessage("unused")), fallback_model=fake(AIMessage("fb")), checkpointer=InMemorySaver())
    stream = TurnStream(meta())
    cfg = run_config(SID)
    # Seed a thread whose last AI message is the ModelCallLimitMiddleware line (spike 17a shape).
    await agent.aupdate_state(cfg, {"messages": [HumanMessage("x"), AIMessage(f"{LIMIT_PREFIX} run limit (6/6)")]})

    async def body(s: TurnStream) -> TurnOutcome:
        """What: classify the seeded state. Use when: this test. Inputs: s. Output: outcome. Side effects: none. Role: helper."""
        from agent.sse import classify_final
        state = await agent.aget_state(cfg)
        return classify_final(s, state.values["messages"], model_ids=set())

    task = start_turn(stream, body, write_final=lambda o: asyncio.sleep(0))
    events = parse_frames(await collect(stream))
    out = await task
    assert out.end_reason == "limit_reached" and not [e for e in events if e.type == "token"]
    assert LIMIT_PREFIX not in out.final.text


async def test_exactly_one_turn_completed_and_a_second_is_refused():
    stream = TurnStream(meta())

    async def body(s: TurnStream) -> TurnOutcome:
        """What: trivial body. Use when: this test. Inputs: s. Output: outcome. Side effects: none. Role: helper."""
        return TurnOutcome.fixed("Hello.", end_reason="answered")

    task = start_turn(stream, body, write_final=lambda o: asyncio.sleep(0))
    events = parse_frames(await collect(stream))
    await task
    check_turn(events)
    with pytest.raises(RuntimeError):
        stream.emit("turn.completed", final=events[-1].final.model_dump(mode="json"),
                    usage=events[-1].usage.model_dump(mode="json"), end_reason="answered", replayed=False)


async def test_body_exception_gives_error_then_turn_completed_and_failed_row():
    stream = TurnStream(meta())
    written: list[TurnOutcome] = []

    async def body(s: TurnStream) -> TurnOutcome:
        """What: fails. Use when: this test. Inputs: s. Output: never. Side effects: raises. Role: helper."""
        raise RuntimeError("boom internals")

    async def write_final(o: TurnOutcome) -> None:
        """What: record. Use when: start_turn. Inputs: o. Output: none. Side effects: appends. Role: fake."""
        written.append(o)

    task = start_turn(stream, body, write_final=write_final)
    events = parse_frames(await collect(stream))
    await task
    check_turn(events)
    assert [e.type for e in events][-2:] == ["error", "turn.completed"]
    assert events[-1].end_reason == "error" and "boom" not in json.dumps(events[-2].model_dump(mode="json"))
    assert written[0].status == "failed" and written[0].error is not None


async def test_keepalive_comment_when_idle():
    stream = TurnStream(meta(), keepalive_s=0.05)
    gate = asyncio.Event()

    async def body(s: TurnStream) -> TurnOutcome:
        """What: waits. Use when: this test. Inputs: s. Output: outcome. Side effects: none. Role: helper."""
        await gate.wait()
        return TurnOutcome.fixed("ok", end_reason="answered")

    task = start_turn(stream, body, write_final=lambda o: asyncio.sleep(0))
    frames: list[str] = []
    async for f in stream.frames():
        frames.append(f)
        if f == KEEPALIVE:
            gate.set()
    await task
    assert KEEPALIVE in frames
    check_turn(parse_frames(frames))


async def test_client_abort_mid_turn_still_writes_the_final_row():
    stream = TurnStream(meta())
    gate, written = asyncio.Event(), []

    async def body(s: TurnStream) -> TurnOutcome:
        """What: waits for the gate. Use when: this test. Inputs: s. Output: outcome. Side effects: none. Role: helper."""
        await gate.wait()
        return TurnOutcome.fixed("Finished after the client left.", end_reason="answered")

    async def write_final(o: TurnOutcome) -> None:
        """What: record. Use when: start_turn. Inputs: o. Output: none. Side effects: appends. Role: fake."""
        written.append(o)

    task = start_turn(stream, body, write_final=write_final)
    gen = stream.frames()
    first = await gen.__anext__()
    assert parse_frames([first])[0].type == "turn.started"
    await gen.aclose()  # the client went away (Starlette closes the response iterator)
    gate.set()
    outcome = await asyncio.wait_for(task, 2)
    assert written == [outcome] and outcome.final.text == "Finished after the client left."
    assert isinstance(stream.events[-1], TurnCompletedEvent)


def test_chunk_text_is_about_40_chars_and_lossless():
    text = "a" * 95
    assert [len(c) for c in chunk_text(text)] == [40, 40, 15]
    assert chunk_text("") == []


async def test_emitted_events_have_the_same_keys_as_the_contracts_fixtures():
    from pathlib import Path
    fixtures = Path(__file__).resolve().parents[3] / "packages" / "contracts" / "fixtures"
    keys_by_type: dict[str, set[str]] = {}
    for f in fixtures.glob("*.jsonl"):
        for line in f.read_text().splitlines():
            row = json.loads(line)
            keys_by_type.setdefault(row["type"], set()).update(row)
    answer = "x" * 50
    agent = build_agent(fake(AIMessage(answer), delay_s=0.2), fallback_model=fake(AIMessage("fb")),
                        checkpointer=InMemorySaver())
    events, _, _ = await run_agent(agent, slow_after_s=0.05)
    seen = set()
    for e in events:
        dumped = set(e.model_dump(mode="json", by_alias=True))
        assert dumped <= keys_by_type[e.type], (e.type, dumped - keys_by_type[e.type])
        seen.add(e.type)
    assert seen == {"turn.started", "step.started", "step.completed", "status", "token", "turn.completed"}
