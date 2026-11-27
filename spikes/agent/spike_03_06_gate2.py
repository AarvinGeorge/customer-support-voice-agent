"""Spikes 3 and 6: Gate 2 draft removal and verifier token filtering (doc 07 s14 rows 3, 6; s6.4).

What: (3) an `after_model` hook returns `RemoveMessage(draft.id)` plus the fixed clarifying line
    and `jump_to: "end"`; the checkpoint is read back. (6) a verifier model called inside that
    hook with tag `internal:gate` is streamed with `stream_mode="messages"`; the metadata is
    checked for node name and tags.
Use when: deciding GroundingMiddleware's update shape and the SSE adapter's token filter.
Inputs: fake streaming models; InMemorySaver (same `add_messages` reducer as AsyncPostgresSaver).
Output: pytest results; `-s` prints the stream metadata seen.
Side effects: none (no provider calls).
Role: day-1 spike CS-E-03.S6.T5.
"""

from __future__ import annotations

from typing import Any

from langchain.agents import create_agent
from langchain.agents.middleware import AgentMiddleware, ModelCallLimitMiddleware, hook_config
from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage, RemoveMessage
from langgraph.checkpoint.memory import InMemorySaver

from fakes import scripted

CLARIFY = "I want to be sure I give you the right answer. Could you tell me a little more about what you need?"
DRAFT = "Refunds are available within 90 days for any reason."
GATE_TAG = "internal:gate"


class GroundingSpike(AgentMiddleware):
    """Stand-in for GroundingMiddleware: fails every final draft after asking a verifier.

    What: on a final draft (no tool calls), calls `verifier` tagged `internal:gate`, then removes
        the draft and adds the fixed clarifying line, jumping to end.
    Use when: spikes 3 and 6.
    Inputs: verifier model; `extra_tags` for the verifier call.
    Output: state update or None.
    Side effects: one verifier call per final draft; records the removed draft id.
    Role: spike harness.
    """

    def __init__(self, verifier: Any, extra_tags: list[str] | None = None) -> None:
        """Store the verifier.

        What: constructor.
        Use when: building the agent.
        Inputs: verifier, extra tags.
        Output: none.
        Side effects: none.
        Role: spike harness.
        """
        super().__init__()
        self.verifier = verifier
        self.tags = [GATE_TAG, *(extra_tags or [])]
        self.removed_id: str | None = None

    @hook_config(can_jump_to=["end"])
    async def aafter_model(self, state: Any, runtime: Any) -> dict[str, Any] | None:
        """Gate the final draft.

        What: see class.
        Use when: framework calls it after each model call.
        Inputs: state, runtime.
        Output: removal update plus jump, or None.
        Side effects: verifier call.
        Role: spike harness.
        """
        draft = state["messages"][-1]
        if not isinstance(draft, AIMessage) or draft.tool_calls:
            return None
        await self.verifier.ainvoke([HumanMessage(f"Check: {draft.text}")], config={"tags": self.tags})
        self.removed_id = draft.id
        return {"messages": [RemoveMessage(id=draft.id), AIMessage(content=CLARIFY)], "jump_to": "end"}


def build(gate: GroundingSpike, *drafts: str):
    """Agent with the limit middleware before Gate 2 (doc 07 s2.5 order) and a checkpointer.

    What: create_agent with ModelCallLimit and the gate.
    Use when: every case.
    Inputs: gate, draft texts for the agent model.
    Output: compiled agent.
    Side effects: none.
    Role: spike harness.
    """
    model = scripted("agent", *[AIMessage(content=d) for d in drafts])
    return create_agent(
        model,
        tools=[],
        middleware=[ModelCallLimitMiddleware(run_limit=6, exit_behavior="end"), gate],
        checkpointer=InMemorySaver(),
    )


def _verifier() -> Any:
    """Streaming fake verifier.

    What: replies with a JSON-ish verdict.
    Use when: gate cases.
    Inputs: none.
    Output: ScriptedModel.
    Side effects: none.
    Role: spike harness.
    """
    return scripted("verifier", AIMessage(content='{"supported": false, "claims": 1}'))


# ---------- spike 3 ----------


async def test_spike3_remove_message_from_after_model_removes_draft_from_checkpoint():
    gate = GroundingSpike(_verifier())
    agent = build(gate, DRAFT)
    cfg = {"configurable": {"thread_id": "g1"}}
    out = await agent.ainvoke({"messages": [HumanMessage("Can I get a refund?")]}, cfg)
    stored = (await agent.aget_state(cfg)).values["messages"]
    print("spike3 stored:", [(type(m).__name__, m.text[:40]) for m in stored])
    assert gate.removed_id
    assert gate.removed_id not in [m.id for m in stored]
    assert DRAFT not in [m.text for m in stored]
    assert [type(m).__name__ for m in stored] == ["HumanMessage", "AIMessage"]
    assert stored[-1].text == CLARIFY
    assert out["messages"][-1].text == CLARIFY


async def test_spike3_next_turn_model_input_excludes_the_draft():
    seen_inputs: list[list[str]] = []

    class Spy(AgentMiddleware):
        """Records the messages each model call receives.

        What: wrap_model_call spy.
        Use when: this test.
        Inputs: request.
        Output: handler result.
        Side effects: appends message texts to seen_inputs.
        Role: spike harness.
        """

        async def awrap_model_call(self, request: Any, handler: Any) -> Any:
            """Record and pass through.

            What: spy.
            Use when: each model call.
            Inputs: request, handler.
            Output: ModelResponse.
            Side effects: appends.
            Role: spike harness.
            """
            seen_inputs.append([m.text for m in request.messages])
            return await handler(request)

    gate = GroundingSpike(scripted("verifier", AIMessage(content="v1"), AIMessage(content="v2")))
    model = scripted("agent", AIMessage(content=DRAFT), AIMessage(content="second draft"))
    agent = create_agent(model, tools=[], middleware=[Spy(), gate], checkpointer=InMemorySaver())
    cfg = {"configurable": {"thread_id": "g2"}}
    await agent.ainvoke({"messages": [HumanMessage("Can I get a refund?")]}, cfg)
    await agent.ainvoke({"messages": [HumanMessage("Annual plan, bought last week.")]}, cfg)
    assert DRAFT not in seen_inputs[1]
    assert CLARIFY in seen_inputs[1]


async def test_spike3_checkpoint_history_still_holds_the_draft():
    """Records a limit: earlier checkpoints (history) keep the draft; only the latest state drops it."""
    agent = build(GroundingSpike(_verifier()), DRAFT)
    cfg = {"configurable": {"thread_id": "g3"}}
    await agent.ainvoke({"messages": [HumanMessage("Can I get a refund?")]}, cfg)
    history = [s async for s in agent.aget_state_history(cfg)]
    in_history = any(DRAFT in [m.text for m in s.values.get("messages", [])] for s in history)
    print(f"spike3 checkpoints={len(history)} draft_in_history={in_history}")
    assert in_history


# ---------- spike 6 ----------


async def _stream(agent, cfg) -> list[tuple[str, list[str], str]]:
    """Collect (node, tags, text) for every `messages` stream item.

    What: what the SSE adapter will see.
    Use when: spike 6 cases.
    Inputs: agent, config.
    Output: list of tuples.
    Side effects: runs the agent.
    Role: spike harness.
    """
    seen = []
    async for msg, meta in agent.astream(
        {"messages": [HumanMessage("Can I get a refund?")]}, cfg, stream_mode="messages"
    ):
        seen.append((meta["langgraph_node"], meta.get("tags", []), type(msg).__name__, msg.text))
    return seen


async def test_spike6_verifier_tokens_carry_node_and_gate_tag():
    agent = build(GroundingSpike(_verifier()), DRAFT)
    seen = await _stream(agent, {"configurable": {"thread_id": "s6a"}})
    for row in seen:
        print("spike6", row)
    gate_rows = [r for r in seen if GATE_TAG in r[1]]
    model_rows = [r for r in seen if r[0] == "model"]
    assert gate_rows, "verifier output must be visible in the stream for the filter to matter"
    assert all(r[0] == "GroundingSpike.after_model" for r in gate_rows)
    assert all(GATE_TAG not in r[1] for r in model_rows)
    # The adapter rule "text only from node `model`, drop any chunk tagged internal:gate"
    # keeps exactly the agent draft text.
    kept = "".join(r[3] for r in seen if r[0] == "model" and GATE_TAG not in r[1] and r[2] == "AIMessageChunk")
    assert kept == DRAFT


async def test_spike6_nostream_tag_hides_verifier_tokens_entirely():
    agent = build(GroundingSpike(_verifier(), extra_tags=["nostream"]), DRAFT)
    seen = await _stream(agent, {"configurable": {"thread_id": "s6b"}})
    print("spike6 nostream nodes:", sorted({(r[0], r[2]) for r in seen}))
    assert not any(GATE_TAG in r[1] for r in seen)
    assert not any(r[0] == "GroundingSpike.after_model" and r[2] == "AIMessageChunk" for r in seen)
