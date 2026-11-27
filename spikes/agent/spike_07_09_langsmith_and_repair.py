"""Spikes 7 and 9: caller-supplied LangSmith run id; dangling tool-call repair (doc 07 s14 rows 7, 9).

What: measures (7) that `astream(..., config={"run_id": turn_id})` makes the turn id the LangSmith root
    run id (read back through the LangSmith API), and (9) that the ingress step 10 repair (one
    ToolMessage `{"status":"not_run","reason":"turn_interrupted"}` per dangling tool call, written with
    `aupdate_state`) is accepted by both providers on the next call, while the unrepaired thread is not.
Use when: deciding the LangSmith mapping (T8) and ingress step 10 (CS-E-03.S7).
Inputs: fake models; real `claude-haiku-5-5` and `gpt-5.4-mini` for the `real` tests; LANGSMITH_API_KEY.
Output: pytest results; `-s` prints the evidence lines.
Side effects: real tests make provider calls (4 in total) and upload one LangSmith trace.
Role: day-1 spike CS-E-03.S6.T7 (spike 8 lives in services/agent/migrations, measured in Compose).
"""

from __future__ import annotations

import asyncio
import json
import os
from typing import Any

import pytest
from langchain.agents import create_agent
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.tools import tool
from langchain_core.tracers.context import collect_runs
from langchain_core.utils.uuid import uuid7
from langgraph.checkpoint.memory import InMemorySaver

from fakes import load_env, real_model, scripted, tool_call

REPAIR_CONTENT = json.dumps({"status": "not_run", "reason": "turn_interrupted"})


@tool
def list_tickets() -> str:
    """List the customer's support tickets."""
    return "[]"


def dangling_calls(messages: list[Any]) -> list[dict[str, Any]]:
    """Find tool calls in the last AI message that have no ToolMessage.

    What: the ingress step 10 detector.
    Use when: before anything touches the thread at the start of a turn.
    Inputs: thread messages.
    Output: the tool call dicts without a result (empty when the thread is clean).
    Side effects: none.
    Role: spike copy of the repair rule under test.
    """
    last_ai = next((i for i in range(len(messages) - 1, -1, -1) if isinstance(messages[i], AIMessage)), None)
    if last_ai is None:
        return []
    answered = {m.tool_call_id for m in messages[last_ai + 1:] if isinstance(m, ToolMessage)}
    return [tc for tc in (messages[last_ai].tool_calls or []) if tc["id"] not in answered]


async def repair_dangling(agent: Any, config: dict[str, Any]) -> int:
    """Append one not_run ToolMessage per dangling tool call (doc 07 s2.4 step 10).

    What: reads the thread and writes the repair with `aupdate_state(..., as_node="tools")`.
    Use when: at the start of a turn.
    Inputs: compiled agent, run config with thread_id.
    Output: number of ToolMessages written.
    Side effects: one checkpoint write when anything is dangling.
    Role: spike copy of the repair under test.
    """
    state = await agent.aget_state(config)
    missing = dangling_calls(state.values.get("messages", []))
    if not missing:
        return 0
    fixes = [ToolMessage(content=REPAIR_CONTENT, tool_call_id=tc["id"], name=tc["name"], status="error")
             for tc in missing]
    await agent.aupdate_state(config, {"messages": fixes}, as_node="tools")
    return len(fixes)


async def make_dangling_thread(saver: InMemorySaver, thread_id: str) -> dict[str, Any]:
    """Leave a thread whose last AI message has an unanswered tool call.

    What: runs a fake model that calls `list_tickets`, interrupted before the tools node (the same
        checkpoint a turn cancelled between the model and the tool leaves behind).
    Use when: spike 9 setup.
    Inputs: shared saver, thread id.
    Output: the run config.
    Side effects: writes checkpoints to the saver.
    Role: spike harness.
    """
    calls: list[str] = []
    fake = scripted("fake", tool_call("list_tickets", call_id="toolu_dangling1"), calls=calls)
    agent = create_agent(fake, tools=[list_tickets], checkpointer=saver, interrupt_before=["tools"])
    cfg = {"configurable": {"thread_id": thread_id}}
    await agent.ainvoke({"messages": [HumanMessage("Do I have any open tickets?")]}, cfg)
    return cfg


# ---------- spike 7 (fakes) ----------


async def test_spike7_run_id_in_config_becomes_the_root_run_id():
    turn_id = uuid7()
    agent = create_agent(scripted("fake", AIMessage("hi")), tools=[list_tickets])
    with collect_runs() as cb:
        async for _ in agent.astream({"messages": [HumanMessage("hi")]}, {"run_id": turn_id},
                                     stream_mode="messages"):
            pass
    graph_roots = [r for r in cb.traced_runs if r.name == "LangGraph"]
    assert [(r.id, r.trace_id, r.parent_run_id) for r in graph_roots] == [(turn_id, turn_id, None)]
    # Measured: under collect_runs the chat model run is reported as a SEPARATE root (parent None,
    # trace_id != turn_id). Whether LangSmith nests it under the turn is checked by the real test.
    model_runs = [r for r in cb.traced_runs if r.name != "LangGraph"]
    print(f"SPIKE7 fake: collector model runs parent={[r.parent_run_id for r in model_runs]} "
          f"trace_is_turn={[r.trace_id == turn_id for r in model_runs]}")


# ---------- spike 9 (fakes) ----------


async def test_spike9_detector_finds_the_dangling_call_and_repair_clears_it():
    saver = InMemorySaver()
    cfg = await make_dangling_thread(saver, "t-fake")
    agent = create_agent(scripted("fake", AIMessage("ok")), tools=[list_tickets], checkpointer=saver)
    before = (await agent.aget_state(cfg)).values["messages"]
    assert [tc["id"] for tc in dangling_calls(before)] == ["toolu_dangling1"]
    assert await repair_dangling(agent, cfg) == 1
    after = (await agent.aget_state(cfg)).values["messages"]
    assert dangling_calls(after) == []
    assert isinstance(after[-1], ToolMessage) and after[-1].content == REPAIR_CONTENT
    assert await repair_dangling(agent, cfg) == 0  # idempotent


async def test_spike9_next_turn_runs_the_model_once_with_the_repaired_history():
    saver = InMemorySaver()
    cfg = await make_dangling_thread(saver, "t-fake2")
    calls: list[str] = []
    model = scripted("next", AIMessage("You have no open tickets."), calls=calls)
    agent = create_agent(model, tools=[list_tickets], checkpointer=saver)
    await repair_dangling(agent, cfg)
    out = await agent.ainvoke({"messages": [HumanMessage("Hello again")]}, cfg)
    kinds = [type(m).__name__ for m in out["messages"]]
    assert kinds == ["HumanMessage", "AIMessage", "ToolMessage", "HumanMessage", "AIMessage"]
    assert calls == ["next"]  # the pending tools node did not run list_tickets; one model call


# ---------- real ----------


@pytest.mark.real
async def test_spike7_real_langsmith_root_run_id_equals_turn_id():
    assert load_env() and os.environ.get("LANGSMITH_API_KEY"), "keys missing: refuse to pass without calls"
    os.environ["LANGSMITH_TRACING"] = "true"
    os.environ.setdefault("LANGSMITH_PROJECT", "cs-voice-agent")
    from langchain_core.tracers.langchain import wait_for_all_tracers
    from langsmith import Client

    turn_id = uuid7()
    agent = create_agent(real_model("anthropic"), tools=[list_tickets])
    async for _ in agent.astream({"messages": [HumanMessage("Reply with the single word: ok")]},
                                 {"run_id": turn_id, "tags": ["spike:7"]}, stream_mode="messages"):
        pass
    wait_for_all_tracers()
    client, run = Client(), None
    for delay in (1, 2, 4, 8, 15):
        try:
            run = client.read_run(turn_id)
            break
        except Exception:  # not ingested yet
            await asyncio.sleep(delay)
    assert run is not None, "root run never appeared in LangSmith"
    print(f"SPIKE7 turn_id={turn_id} langsmith_run.id={run.id} trace_id={run.trace_id} "
          f"parent_run_id={run.parent_run_id} name={run.name} run_type={run.run_type}")
    assert str(run.id) == str(turn_id) and str(run.trace_id) == str(turn_id) and run.parent_run_id is None
    llm_runs: list[Any] = []
    for delay in (1, 2, 4, 8):
        llm_runs = list(client.list_runs(trace_id=turn_id, run_type="llm"))
        if llm_runs:
            break
        await asyncio.sleep(delay)
    print(f"SPIKE7 llm runs in trace {turn_id}: {len(llm_runs)} "
          f"{[(r.name, str(r.parent_run_id)[:8]) for r in llm_runs]}")
    assert len(llm_runs) == 1  # the model call is nested in the turn trace in LangSmith


@pytest.mark.real
async def test_spike9_real_negative_control_unrepaired_thread_is_rejected_by_anthropic():
    assert load_env(), "keys missing"
    saver = InMemorySaver()
    cfg = await make_dangling_thread(saver, "t-neg-anthropic")
    agent = create_agent(real_model("anthropic"), tools=[list_tickets], checkpointer=saver)
    with pytest.raises(Exception) as err:
        await agent.ainvoke({"messages": [HumanMessage("Hello again")]}, cfg)
    print(f"SPIKE9 negative anthropic: {type(err.value).__name__}: {str(err.value)[:220]}")


@pytest.mark.real
@pytest.mark.parametrize("provider", ["anthropic", "openai"])
async def test_spike9_real_repaired_thread_is_accepted(provider: str):
    assert load_env(), "keys missing"
    saver = InMemorySaver()
    cfg = await make_dangling_thread(saver, f"t-real-{provider}")
    agent = create_agent(real_model(provider), tools=[list_tickets], checkpointer=saver)
    assert await repair_dangling(agent, cfg) == 1
    out = await agent.ainvoke({"messages": [HumanMessage("Hello again. Reply in one short sentence.")]}, cfg)
    last = out["messages"][-1]
    print(f"SPIKE9 repaired {provider}: {type(last).__name__} tool_calls={len(last.tool_calls)} "
          f"text={str(last.content)[:120]!r}")
    assert isinstance(last, AIMessage)
