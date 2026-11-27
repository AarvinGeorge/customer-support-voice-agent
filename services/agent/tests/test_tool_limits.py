"""What: the combined tool limiter inside ToolGuard (D-046, D-048; review PR #6 I1): the spike 17 double-block
script must give exactly one ToolMessage per tool call id, 8 executed, search_kb 3, request_verification 1.
Use when: pytest (fake model and fake tools; no provider, no network).
Inputs: none. Output: pass/fail. Side effects: none.
Role: the FIRST failing test of CS-E-03.S8.T6 (the three built-in ToolCallLimitMiddleware instances gave two
ToolMessages for one call id, which both providers reject with 400)."""
from __future__ import annotations

from collections import Counter
from typing import Any

from langchain.agents import create_agent
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.tools import tool

from agent.middleware.tool_guard import ToolGuardMiddleware
from agent.turn_control import TurnControl, bind_turn_control

EXECUTED: list[str] = []


class Scripted(GenericFakeChatModel):
    """What: scripted fake model that accepts tool binding. Use when: create_agent tests. Inputs: messages.
    Output: scripted AIMessages. Side effects: none. Role: test double."""

    def bind_tools(self, tools: Any, **kw: Any) -> "Scripted":
        """What: no-op binding. Use when: create_agent binds tools. Inputs: tools. Output: self.
        Side effects: none. Role: fake plumbing."""
        return self


@tool
async def search_kb(query: str) -> str:
    """Search help articles (test stub)."""
    EXECUTED.append("search_kb")
    return '{"verdict":"pass"}'


@tool
async def request_verification(reason: str) -> str:
    """Request verification (test stub)."""
    EXECUTED.append("request_verification")
    return '{"status":"verification_requested"}'


@tool
async def create_ticket(summary: str) -> str:
    """Create a ticket (test stub)."""
    EXECUTED.append("create_ticket")
    return '{"ticket":"TCK-26118213"}'


def _call(name: str, call_id: str, **args: Any) -> dict[str, Any]:
    """What: one tool call dict. Use when: scripting. Inputs: name, id, args. Output: dict.
    Side effects: none. Role: helper."""
    return {"name": name, "args": args, "id": call_id, "type": "tool_call"}


async def test_combined_limiter_double_block_gives_one_tool_message_per_call_id():
    EXECUTED.clear()
    singles = [AIMessage(content="", tool_calls=[_call("search_kb", f"d{i}", query="refund")]) for i in range(3)]
    # Parallel batches run as separate tasks in no fixed order (create_agent sends one task per call), so the
    # script keeps every count order-independent: two parallel request_verification calls (one may run), four
    # create_ticket calls that spend the total of 8, then the spike 17c poison: `dup` is the 4th search_kb AND
    # arrives after the total is spent, so two limits block it; c4 is a 9th call.
    verify = AIMessage(content="", tool_calls=[_call("request_verification", "v1", reason="action"),
                                               _call("request_verification", "v2", reason="action")])
    tickets = AIMessage(content="", tool_calls=[_call("create_ticket", f"c{i}", summary="s") for i in range(4)])
    poison = AIMessage(content="", tool_calls=[_call("search_kb", "dup", query="refund"),
                                               _call("create_ticket", "c4", summary="s")])
    model = Scripted(messages=iter([*singles, verify, tickets, poison, AIMessage(content="done")]))
    agent = create_agent(model, tools=[search_kb, request_verification, create_ticket],
                         # the limiter alone: all three tools visible (D-070 hides create_ticket in V0, which is
                         # tested in test_router_visibility.py and test_ticket_requester.py)
                         middleware=[ToolGuardMiddleware(visible=lambda _state: ("create_ticket",
                                                                                 "request_verification", "search_kb"))])
    control = TurnControl(session_id="s1", turn_id="t1")
    bind_turn_control(control)
    out = await agent.ainvoke({"messages": [HumanMessage("q")]})

    tool_msgs = [m for m in out["messages"] if isinstance(m, ToolMessage)]
    ids = Counter(m.tool_call_id for m in tool_msgs)
    expected_ids = {"d0", "d1", "d2", "c0", "c1", "c2", "c3", "v1", "v2", "dup", "c4"}
    assert set(ids) == expected_ids and all(n == 1 for n in ids.values()), ids
    assert len(EXECUTED) == 8
    assert Counter(EXECUTED)["search_kb"] == 3
    assert Counter(EXECUTED)["request_verification"] == 1
    blocked = {m.tool_call_id for m in tool_msgs if m.status == "error"}
    assert len(blocked) == 3 and {"dup", "c4"} <= blocked and len(blocked & {"v1", "v2"}) == 1
    assert control.tool_calls == 8 and control.per_tool == {"search_kb": 3, "create_ticket": 4,
                                                             "request_verification": 1}
    assert out["messages"][-1].content == "done"  # a tool limit never ends the run (D-048)
