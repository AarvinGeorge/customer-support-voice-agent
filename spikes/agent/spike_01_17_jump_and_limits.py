"""Spikes 1 and 17: before_model jump to end; limit middleware exit shapes (doc 07 s14 rows 1, 17).

What: measures (1) `before_model` returning messages plus `jump_to: "end"` in the doc 07 s2.5
    order, and (17) what ModelCallLimitMiddleware(exit_behavior="end") and three
    ToolCallLimitMiddleware instances return at their limits, and whether run counts reset per turn.
Use when: deciding RouterMiddleware's return shape and ingress step 15 "limit_reached" detection.
Inputs: fake models only (framework behaviour).
Output: pytest results; `-s` prints the exact limit message texts.
Side effects: none (no provider calls).
Role: day-1 spike CS-E-03.S6.T2.
"""

from __future__ import annotations

from typing import Any

import pytest

from langchain.agents import create_agent
from langchain.agents.middleware import (
    AgentMiddleware,
    ModelCallLimitMiddleware,
    ToolCallLimitMiddleware,
    hook_config,
)
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.tools import tool
from langgraph.checkpoint.memory import InMemorySaver

from fakes import scripted, tool_call

FIXED = "Our help articles don't cover that. I've opened a ticket for our team."
EXECUTED: list[str] = []


@tool
def search_kb(query: str) -> str:
    """Search help articles (spike stub)."""
    EXECUTED.append("search_kb")
    return "chunks"


@tool
def request_verification(reason: str) -> str:
    """Request verification (spike stub)."""
    EXECUTED.append("request_verification")
    return "verification_requested"


@tool
def list_tickets() -> str:
    """List tickets (spike stub)."""
    EXECUTED.append("list_tickets")
    return "[]"


TOOLS = [search_kb, request_verification, list_tickets]


class RouterSpike(AgentMiddleware):
    """Routes to end when the flag in state context says so.

    What: returns a fixed AIMessage plus jump_to end on the Nth before_model call.
    Use when: spike 1.
    Inputs: `end_on_call`, which before_model call (1-based) ends the turn.
    Output: None or the route reply.
    Side effects: counts calls in `seen`.
    Role: spike harness (stand-in for RouterMiddleware).
    """

    def __init__(self, end_on_call: int) -> None:
        """Store the call number that ends the turn.

        What: constructor.
        Use when: building the spike agent.
        Inputs: end_on_call.
        Output: none.
        Side effects: none.
        Role: spike harness.
        """
        super().__init__()
        self.end_on_call = end_on_call
        self.seen = 0

    @hook_config(can_jump_to=["end"])
    def before_model(self, state: Any, runtime: Any) -> dict[str, Any] | None:
        """Return the fixed reply and jump to end on the chosen call.

        What: see class.
        Use when: framework calls it before each model call.
        Inputs: state, runtime.
        Output: route reply or None.
        Side effects: increments `seen`.
        Role: spike harness.
        """
        self.seen += 1
        if self.seen == self.end_on_call:
            return {"messages": [AIMessage(content=FIXED)], "jump_to": "end"}
        return None


class Probe(AgentMiddleware):
    """Records which hooks ran, to prove a jump skipped the rest.

    What: logs before_model and after_model calls into `log`.
    Use when: spike 1 ordering checks.
    Inputs: none.
    Output: None from every hook.
    Side effects: appends to `log`.
    Role: spike harness (stand-in for GroundingMiddleware and later hooks).
    """

    def __init__(self) -> None:
        """Create the hook log.

        What: constructor.
        Use when: building the spike agent.
        Inputs: none.
        Output: none.
        Side effects: none.
        Role: spike harness.
        """
        super().__init__()
        self.log: list[str] = []

    def before_model(self, state: Any, runtime: Any) -> None:
        """Log before_model.

        What: probe.
        Use when: framework calls it.
        Inputs: state, runtime.
        Output: None.
        Side effects: appends "before".
        Role: spike harness.
        """
        self.log.append("before")

    def after_model(self, state: Any, runtime: Any) -> None:
        """Log after_model.

        What: probe.
        Use when: framework calls it.
        Inputs: state, runtime.
        Output: None.
        Side effects: appends "after".
        Role: spike harness.
        """
        self.log.append("after")


def limit_stack() -> list[AgentMiddleware]:
    """The doc 07 s2.5 items 2 and 3 exactly.

    What: ModelCallLimit(6, end) plus the three ToolCallLimit instances.
    Use when: spike 17 cases.
    Inputs: none.
    Output: list of middleware.
    Side effects: none.
    Role: spike harness.
    """
    return [
        ModelCallLimitMiddleware(run_limit=6, exit_behavior="end"),
        ToolCallLimitMiddleware(run_limit=8),
        ToolCallLimitMiddleware(tool_name="search_kb", run_limit=3),
        ToolCallLimitMiddleware(tool_name="request_verification", run_limit=1),
    ]


def _msgs(result: dict) -> list:
    """Return the message list of an invoke result.

    What: shorthand.
    Use when: assertions.
    Inputs: invoke result.
    Output: messages.
    Side effects: none.
    Role: test helper.
    """
    return result["messages"]


# ---------- spike 1 ----------


def test_spike1_before_model_messages_plus_jump_end_skips_model_and_after_hooks():
    calls: list[str] = []
    router, probe = RouterSpike(end_on_call=1), Probe()
    model = scripted("agent", AIMessage(content="MODEL_RAN"), calls=calls)
    agent = create_agent(model, tools=TOOLS, middleware=[router, *limit_stack(), probe])
    out = agent.invoke({"messages": [HumanMessage("hi")]})
    assert calls == []
    assert _msgs(out)[-1].content == FIXED
    assert isinstance(_msgs(out)[-1], AIMessage)
    assert probe.log == []  # later before_model and every after_model skipped


def test_spike1_jump_end_after_a_tool_round_and_thread_persists_line():
    calls: list[str] = []
    router, probe = RouterSpike(end_on_call=2), Probe()
    model = scripted("agent", tool_call("list_tickets"), AIMessage(content="MODEL_RAN_AGAIN"), calls=calls)
    agent = create_agent(model, tools=TOOLS, middleware=[router, probe], checkpointer=InMemorySaver())
    cfg = {"configurable": {"thread_id": "s1"}}
    agent.invoke({"messages": [HumanMessage("hi")]}, cfg)
    assert calls == ["agent"]
    assert probe.log == ["before", "after"]
    stored = agent.get_state(cfg).values["messages"]
    assert [type(m).__name__ for m in stored] == ["HumanMessage", "AIMessage", "ToolMessage", "AIMessage"]
    assert stored[-1].content == FIXED
    # jump_to does not leak into the next turn: the next turn reaches the model normally.
    router.end_on_call = 99
    out2 = agent.invoke({"messages": [HumanMessage("again")]}, cfg)
    assert _msgs(out2)[-1].content == "MODEL_RAN_AGAIN"


def test_spike1_route_line_appears_in_messages_stream_with_router_node_name():
    """Record what the SSE adapter will see for a route line (it must not take it from node `model`)."""
    router = RouterSpike(end_on_call=1)
    agent = create_agent(scripted("agent", AIMessage(content="x")), tools=TOOLS, middleware=[router])
    seen = [
        (meta["langgraph_node"], msg.content)
        for msg, meta in agent.stream({"messages": [HumanMessage("hi")]}, stream_mode="messages")
    ]
    print("spike1 messages stream:", seen)
    assert seen == [("RouterSpike.before_model", FIXED)]


# ---------- spike 17 ----------


def test_spike17_three_tool_limit_instances_coexist():
    agent = create_agent(scripted("agent", AIMessage(content="ok")), tools=TOOLS, middleware=limit_stack())
    names = sorted(n for n in agent.get_graph().nodes if "Limit" in n)
    print("spike17 limit nodes:", names)
    assert "ToolCallLimitMiddleware.after_model" in names
    assert "ToolCallLimitMiddleware[search_kb].after_model" in names
    assert "ToolCallLimitMiddleware[request_verification].after_model" in names


def test_spike17_model_call_limit_end_injects_ai_message_without_raising():
    EXECUTED.clear()
    calls: list[str] = []
    replies = [tool_call("list_tickets", call_id=f"c{i}") for i in range(10)]
    agent = create_agent(scripted("agent", *replies, calls=calls), tools=TOOLS, middleware=limit_stack())
    out = agent.invoke({"messages": [HumanMessage("loop")]})
    last = _msgs(out)[-1]
    print("spike17 model limit final:", repr(last.content), "id:", last.id, "meta:", last.response_metadata)
    assert len(calls) == 6
    assert EXECUTED == ["list_tickets"] * 6
    assert isinstance(last, AIMessage) and not last.tool_calls
    assert last.content == "Model call limits exceeded: run limit (6/6)"
    assert last.response_metadata == {} and last.usage_metadata is None


def test_spike17_search_kb_limit_blocks_fourth_call_with_error_tool_message_and_continues():
    EXECUTED.clear()
    calls: list[str] = []
    replies = [tool_call("search_kb", {"query": "refund"}, call_id=f"s{i}") for i in range(4)]
    replies.append(AIMessage(content="final answer"))
    agent = create_agent(scripted("agent", *replies, calls=calls), tools=TOOLS, middleware=limit_stack())
    out = agent.invoke({"messages": [HumanMessage("q")]})
    blocked = [m for m in _msgs(out) if isinstance(m, ToolMessage) and m.status == "error"]
    print("spike17 search_kb blocked:", [(m.tool_call_id, m.content) for m in blocked])
    assert EXECUTED == ["search_kb"] * 3
    assert [m.tool_call_id for m in blocked] == ["s3"]
    assert blocked[0].content == "Tool call limit exceeded. Do not call 'search_kb' again."
    assert _msgs(out)[-1].content == "final answer"  # exit_behavior default "continue": model goes on
    assert len(calls) == 5


def test_spike17_request_verification_limit_blocks_second_parallel_call():
    EXECUTED.clear()
    two = AIMessage(
        content="",
        tool_calls=[
            {"name": "request_verification", "args": {"reason": "a"}, "id": "v1", "type": "tool_call"},
            {"name": "request_verification", "args": {"reason": "b"}, "id": "v2", "type": "tool_call"},
        ],
    )
    agent = create_agent(scripted("agent", two, AIMessage(content="done")), tools=TOOLS, middleware=limit_stack())
    out = agent.invoke({"messages": [HumanMessage("q")]})
    blocked = [m.tool_call_id for m in _msgs(out) if isinstance(m, ToolMessage) and m.status == "error"]
    assert EXECUTED == ["request_verification"]
    assert blocked == ["v2"]


def test_spike17_global_limit_counts_all_tools_and_composes_with_search_kb_limit():
    EXECUTED.clear()

    def batch(i: int) -> AIMessage:
        return AIMessage(
            content="",
            tool_calls=[
                {"name": "list_tickets", "args": {}, "id": f"g{i}a", "type": "tool_call"},
                {"name": "list_tickets", "args": {}, "id": f"g{i}b", "type": "tool_call"},
                {"name": "search_kb", "args": {"query": "abc"}, "id": f"g{i}c", "type": "tool_call"},
            ],
        )

    replies = [batch(i) for i in range(4)] + [AIMessage(content="done")]
    agent = create_agent(scripted("agent", *replies), tools=TOOLS, middleware=limit_stack())
    out = agent.invoke({"messages": [HumanMessage("q")]})
    blocked = [m.tool_call_id for m in _msgs(out) if isinstance(m, ToolMessage) and m.status == "error"]
    print("spike17 global executed:", EXECUTED, "blocked:", blocked)
    # 12 calls requested; 8 allowed in total; search_kb capped at 3 independently.
    assert len(EXECUTED) <= 8
    assert EXECUTED.count("search_kb") <= 3
    assert "g3c" in blocked  # the 4th search_kb is blocked whichever limiter fires first


def test_spike17_run_counts_reset_each_turn_on_the_same_thread():
    EXECUTED.clear()
    replies = []
    for turn in range(2):
        replies += [tool_call("search_kb", {"query": "q"}, call_id=f"t{turn}s{i}") for i in range(3)]
        replies.append(AIMessage(content=f"answer {turn}"))
    agent = create_agent(
        scripted("agent", *replies), tools=TOOLS, middleware=limit_stack(), checkpointer=InMemorySaver()
    )
    cfg = {"configurable": {"thread_id": "s17"}}
    agent.invoke({"messages": [HumanMessage("turn 1")]}, cfg)
    out = agent.invoke({"messages": [HumanMessage("turn 2")]}, cfg)
    blocked = [m for m in _msgs(out) if isinstance(m, ToolMessage) and m.status == "error"]
    assert EXECUTED == ["search_kb"] * 6
    assert blocked == []
    assert _msgs(out)[-1].content == "answer 1"


def test_spike17_double_block_gives_two_tool_messages_for_one_call_id():
    """Two limiters blocking the same call each inject an error ToolMessage with the same id."""
    EXECUTED.clear()
    replies = [tool_call("search_kb", {"query": "abc"}, call_id=f"d{i}") for i in range(3)]
    many = AIMessage(
        content="",
        tool_calls=[{"name": "list_tickets", "args": {}, "id": f"m{i}", "type": "tool_call"} for i in range(5)]
        + [{"name": "search_kb", "args": {"query": "abc"}, "id": "dup", "type": "tool_call"}],
    )
    agent = create_agent(scripted("agent", *replies, many, AIMessage(content="done")), tools=TOOLS, middleware=limit_stack())
    out = agent.invoke({"messages": [HumanMessage("q")]})
    dup = [m for m in _msgs(out) if isinstance(m, ToolMessage) and m.tool_call_id == "dup"]
    print("spike17 duplicate tool messages:", [m.content for m in dup])
    assert len(dup) == 2


@pytest.mark.real
@pytest.mark.parametrize("provider", ["anthropic", "openai"])
def test_spike17_real_provider_on_duplicate_tool_results(provider):
    """Does the next call accept a history with two results for one tool call id?"""
    from fakes import real_model

    history = [
        HumanMessage("Search for refund policy."),
        tool_call("search_kb", {"query": "refund policy"}, call_id="toolu_dup0001"),
        ToolMessage("Tool call limit exceeded. Do not make additional tool calls.", tool_call_id="toolu_dup0001", status="error"),
        ToolMessage("Tool call limit exceeded. Do not call 'search_kb' again.", tool_call_id="toolu_dup0001", status="error"),
    ]
    model = real_model(provider).bind_tools(TOOLS)
    try:
        reply = model.invoke(history)
        outcome = f"accepted: {reply.content!r:.80}"
    except Exception as exc:  # recorded, not hidden: this is the measurement
        outcome = f"rejected: {type(exc).__name__}: {str(exc)[:200]}"
    print(f"spike17 {provider} duplicate tool results -> {outcome}")
    # Measured 2026-11-27: both providers answer 400. If this ever flips, revisit the decision.
    assert outcome.startswith("rejected: ") and "400" in outcome
