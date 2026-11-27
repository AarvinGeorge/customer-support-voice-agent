"""Spike 15: TurnControl context propagation (doc 07 s14 row 15, s2.5 item 1).

What: proves a tool (or a wrap_tool_call wrapper) that mutates the TurnControl object bound
    once before `astream` is seen by the next `before_model`, and that `.set()` inside a tool is not.
Use when: deciding whether `route` lives on TurnControl or in graph state via `Command`.
Inputs: fake models; real claude-haiku-5-5 and gpt-5.4-mini for the provider rows (marker `real`).
Output: pytest results.
Side effects: real-marked tests make one provider call each.
Role: day-1 spike CS-E-03.S6.T1.
"""

from __future__ import annotations

from typing import Any

import pytest
from langchain.agents import create_agent
from langchain.agents.middleware import AgentMiddleware, hook_config
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.tools import tool

from fakes import TURN_CONTROL, TurnControl, load_env, real_model, scripted, tool_call

VERIFY_LINE = "To look at your account I need to verify you."


class RouterMiddleware(AgentMiddleware):
    """Spike copy of RouterMiddleware: ends the turn in code when TurnControl.route is set.

    What: `before_model` reads TURN_CONTROL.get().route and returns the fixed line plus jump_to end.
    Use when: first in the middleware list (doc 07 s2.5 item 1).
    Inputs: agent state (unused), the bound TurnControl.
    Output: None, or {"messages": [AIMessage], "jump_to": "end"}.
    Side effects: records "router_saw:<route>" in TurnControl.events.
    Role: spike harness.
    """

    @hook_config(can_jump_to=["end"])
    async def abefore_model(self, state: Any, runtime: Any) -> dict[str, Any] | None:
        """Route check before each model call.

        What: see class.
        Use when: framework calls it before every model call.
        Inputs: state, runtime.
        Output: route reply or None.
        Side effects: appends to TurnControl.events.
        Role: spike harness.
        """
        tc = TURN_CONTROL.get()
        tc.events.append(f"router_saw:{tc.route}")
        if tc.route == "verify":
            return {"messages": [AIMessage(content=VERIFY_LINE)], "jump_to": "end"}
        return None


@tool
async def request_verification(reason: str) -> dict:
    """Ask the customer to verify (spike: async tool mutating TurnControl)."""
    TURN_CONTROL.get().route = "verify"
    return {"status": "verification_requested"}


@tool
def request_verification_sync(reason: str) -> dict:
    """Ask the customer to verify (spike: sync tool, runs in an executor thread)."""
    TURN_CONTROL.get().route = "verify"
    return {"status": "verification_requested"}


@tool
async def request_verification_with_set(reason: str) -> dict:
    """Negative control: replaces the context variable instead of mutating the object."""
    TURN_CONTROL.set(TurnControl(route="verify"))
    return {"status": "verification_requested"}


@tool
async def plain_tool(reason: str) -> dict:
    """A tool that does nothing; the wrapper sets the route."""
    return {"status": "ok"}


class ToolGuardSpike(AgentMiddleware):
    """Spike copy of ToolGuard: sets the route from `awrap_tool_call`.

    What: mutates TurnControl.route inside the tool wrapper, as ToolGuard will (doc 07 s6.3 step 2).
    Use when: last in the list (doc 07 s2.5 item 10).
    Inputs: tool call request and handler.
    Output: the tool result.
    Side effects: sets TurnControl.route = "verify".
    Role: spike harness.
    """

    async def awrap_tool_call(self, request: Any, handler: Any) -> Any:
        """Run the tool, then set the route.

        What: see class.
        Use when: framework wraps each tool call.
        Inputs: request, handler.
        Output: ToolMessage from handler.
        Side effects: mutates TurnControl.
        Role: spike harness.
        """
        result = await handler(request)
        TURN_CONTROL.get().route = "verify"
        return result


async def _run(model, tools, middleware=None) -> tuple[TurnControl, list]:
    """Bind a fresh TurnControl once, run `astream`, return it and the final messages.

    What: mirrors ingress step 9 then step 14 for the spike.
    Use when: every spike 15 case.
    Inputs: model, tools, extra middleware.
    Output: (TurnControl, final messages).
    Side effects: sets TURN_CONTROL in the current context (once, before the run).
    Role: spike harness.
    """
    agent = create_agent(model, tools=tools, middleware=[RouterMiddleware(), *(middleware or [])])
    tc = TurnControl()
    TURN_CONTROL.set(tc)
    final: dict = {}
    async for mode, chunk in agent.astream(
        {"messages": [HumanMessage("Why was I charged twice? Call request_verification now.")]},
        stream_mode=["updates", "values"],
    ):
        if mode == "values":
            final = chunk
    return tc, final["messages"]


def _fake(calls: list[str], tool_name: str):
    """Fake model: first call asks for the tool, second call would answer.

    What: the second reply exists so a missed route is visible (the model would be called again).
    Use when: fake spike 15 cases.
    Inputs: call log, tool name.
    Output: ScriptedModel.
    Side effects: none.
    Role: spike harness.
    """
    return scripted(
        "agent",
        tool_call(tool_name, {"reason": "account_lookup"}),
        AIMessage(content="MODEL_CALLED_AGAIN"),
        calls=calls,
    )


async def test_async_tool_mutation_visible_to_next_before_model():
    calls: list[str] = []
    tc, msgs = await _run(_fake(calls, "request_verification"), [request_verification])
    assert tc.events == ["router_saw:None", "router_saw:verify"]
    assert msgs[-1].content == VERIFY_LINE
    assert calls == ["agent"]  # the second model call never happened


async def test_sync_tool_mutation_visible_to_next_before_model():
    calls: list[str] = []
    tc, msgs = await _run(_fake(calls, "request_verification_sync"), [request_verification_sync])
    assert tc.events == ["router_saw:None", "router_saw:verify"]
    assert msgs[-1].content == VERIFY_LINE
    assert calls == ["agent"]


async def test_wrap_tool_call_mutation_visible_to_next_before_model():
    calls: list[str] = []
    tc, msgs = await _run(_fake(calls, "plain_tool"), [plain_tool], [ToolGuardSpike()])
    assert tc.events == ["router_saw:None", "router_saw:verify"]
    assert msgs[-1].content == VERIFY_LINE


async def test_negative_control_contextvar_set_inside_tool_is_invisible():
    """Proves the measurement can fail: `.set()` in a tool does not reach before_model."""
    calls: list[str] = []
    tc, msgs = await _run(_fake(calls, "request_verification_with_set"), [request_verification_with_set])
    assert tc.route is None
    assert tc.events == ["router_saw:None", "router_saw:None"]
    assert msgs[-1].content == "MODEL_CALLED_AGAIN"
    assert calls == ["agent", "agent"]


@pytest.mark.real
@pytest.mark.parametrize("provider", ["anthropic", "openai"])
async def test_real_provider_tool_sets_route(provider):
    if not load_env():
        pytest.fail("provider keys missing: real spike cannot run")
    tc, msgs = await _run(real_model(provider), [request_verification])
    print(f"{provider}: events={tc.events} last={msgs[-1].content[:60]!r}")
    tool_calls = [m for m in msgs if isinstance(m, AIMessage) and m.tool_calls]
    assert tool_calls and tool_calls[0].tool_calls[0]["name"] == "request_verification"
    assert tc.events == ["router_saw:None", "router_saw:verify"]
    assert msgs[-1].content == VERIFY_LINE
