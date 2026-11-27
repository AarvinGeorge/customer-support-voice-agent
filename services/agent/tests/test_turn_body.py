"""What: the turn body (TurnRunner) end to end through Ingress: repair, memory refresh and the turn message with
the untrusted account context (doc 07 s2.4 steps 10, 11, 14; s2.6; s5.3), and a core 401 that makes the model
see V0 on the same turn.
Use when: pytest (in-memory store and lock, contracts core stub over ASGI, recording fake model).
Inputs: none. Output: pass/fail. Side effects: none.
Role: Done when of CS-E-03.S7.T5 through the real ingress entry."""
from __future__ import annotations

import uuid
from typing import Any

import httpx
from contracts.stubs import examples as ex
from contracts.stubs.core_stub import create_app
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.checkpoint.memory import InMemorySaver

from agent.assembly import build_agent, run_config
from agent.clients.core_agent import CoreAgentClient
from agent.clients.knowledge import KnowledgeClient
from agent.ingress import Ingress, IngressInput, IngressStream, SessionRow
from agent.tools import build_tools
from agent.turn_body import TurnRunner
from ingress_fakes import MemoryTurnLock, MemoryTurnStore

SID, VISITOR = ex.SESSION_ID, "visitor-1"
OWNER_STATE = {"verified": True, "claims_snapshot": {"role": "owner", "tier": "pro"},
               "context_snapshot": {"generated_at": "old", "pending_action": None}}


class Recording(GenericFakeChatModel):
    """What: scripted fake model that records the tool names it was bound with and the messages it saw.
    Use when: asserting visibility and the turn message. Inputs: messages. Output: replies. Side effects: records.
    Role: test double."""

    bound: list[list[str]] = []
    seen: list[list[Any]] = []

    def bind_tools(self, tools: Any, **kw: Any) -> "Recording":
        """What: record the visible tool names. Use when: the model node binds tools. Inputs: tools.
        Output: self. Side effects: appends to `bound`. Role: probe."""
        Recording.bound.append(sorted(getattr(t, "name", None) or t.get("name") for t in tools))
        return self

    async def _agenerate(self, messages: Any, *args: Any, **kwargs: Any) -> Any:
        """What: record the prompt, then answer from the script. Use when: each model call. Inputs: messages.
        Output: ChatResult. Side effects: appends to `seen`. Role: probe."""
        Recording.seen.append(list(messages))
        return await super()._agenerate(messages, *args, **kwargs)


class World:
    """What: ingress, the runner, the stub and a checkpointer for one session. Use when: each test.
    Inputs: core transport (default: the stub), replies, initial thread state. Output: helpers.
    Side effects: none. Role: wiring."""

    def __init__(self, *, replies: list[AIMessage] | None = None,
                 core_transport: httpx.AsyncBaseTransport | None = None, stub_verified: bool = True) -> None:
        """What: build it. Use when: tests. Inputs: see class. Output: none. Side effects: none. Role: constructor."""
        Recording.bound, Recording.seen = [], []
        self.stub = create_app()
        if stub_verified:
            self.stub.state.verified.add(SID)
        http = httpx.AsyncClient(transport=core_transport or httpx.ASGITransport(app=self.stub))
        self.core = CoreAgentClient("http://core:8000", "agent-tok", http=http)
        knowledge = KnowledgeClient("http://knowledge:8000", "kn-tok",
                                    http=httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(503))))
        self.agent = build_agent(Recording(messages=iter(replies or [AIMessage("Here you go.")]), disable_streaming=True),
                                 fallback_model=Recording(messages=iter([])), checkpointer=InMemorySaver(),
                                 tools=build_tools(self.core, knowledge))
        self.store = MemoryTurnStore(SessionRow(session_id=SID, visitor_id=VISITOR))
        self.ingress = Ingress(self.store, MemoryTurnLock(), served_by="agent_a",
                               run_agent=TurnRunner(self.agent, self.core))

    async def seed(self, values: dict[str, Any]) -> None:
        """What: write initial thread state. Use when: arranging. Inputs: values. Output: none.
        Side effects: checkpoint write. Role: helper."""
        await self.agent.aupdate_state(run_config(SID), values)

    async def turn(self, text: str) -> tuple[Any, list[Any]]:
        """What: one chat text turn through Ingress. Use when: tests. Inputs: text. Output: (outcome, events).
        Side effects: as a turn. Role: helper."""
        res = await self.ingress.handle(IngressInput(session_id=SID, visitor_id=VISITOR, client_turn_id=str(uuid.uuid4()),
                                                     mode="chat", input_kind="text", text=text))
        assert isinstance(res, IngressStream) and res.task is not None
        _ = [f async for f in res.stream.frames()]
        return await res.task, res.stream.events

    async def values(self) -> dict[str, Any]:
        """What: the thread values. Use when: asserting. Inputs: none. Output: dict. Side effects: none. Role: helper."""
        return (await self.agent.aget_state(run_config(SID))).values


async def test_verified_turn_carries_the_wrapped_account_context_and_claims() -> None:
    w = World()
    await w.seed({"messages": [HumanMessage("earlier"), AIMessage("ok", id="a0")], **OWNER_STATE})
    outcome, _ = await w.turn("what plan am I on?")
    assert outcome.end_reason == "answered"
    assert "list_charges" in Recording.bound[-1]  # V2 here, so the V0 assertion below can fail
    human = [m for m in Recording.seen[-1] if isinstance(m, HumanMessage)][-1]
    assert '<account_context untrusted="true">' in human.content and "verified: true" in human.content
    assert "account_context_stale" not in human.content and "Acme Analytics" in human.content
    v = await w.values()
    assert v["context_snapshot"]["claims"]["session_id"] == SID and v["claims_snapshot"]["role"] == "owner"


async def test_failed_refresh_marks_the_previous_snapshot_stale_in_the_turn_context() -> None:
    def down(_: httpx.Request) -> httpx.Response:
        """What: core is down. Use when: transport. Inputs: request. Output: none. Side effects: raises.
        Role: fake."""
        raise httpx.ConnectError("down")

    w = World(core_transport=httpx.MockTransport(down))
    await w.seed({"messages": [HumanMessage("earlier"), AIMessage("ok", id="a0")], **OWNER_STATE})
    await w.turn("what plan am I on?")
    human = [m for m in Recording.seen[-1] if isinstance(m, HumanMessage)][-1]
    assert "account_context_stale: true" in human.content and '"generated_at":"old"' in human.content


async def test_core_401_makes_the_next_model_call_use_v0() -> None:
    w = World(stub_verified=False)  # claims expired at core
    await w.seed({"messages": [HumanMessage("earlier"), AIMessage("ok", id="a0")], **OWNER_STATE})
    await w.turn("show my charges")
    assert Recording.bound[-1] == ["create_ticket", "request_verification", "search_kb"]
    human = [m for m in Recording.seen[-1] if isinstance(m, HumanMessage)][-1]
    assert "verified: false" in human.content and "account_context" not in human.content
    v = await w.values()
    assert v["verified"] is False and v["claims_snapshot"] is None and v["context_snapshot"] is None


async def test_interrupted_thread_is_repaired_before_the_model_runs() -> None:
    w = World()
    calls = [{"name": "list_charges", "args": {}, "id": "c9", "type": "tool_call"}]
    await w.seed({"messages": [HumanMessage("charges?"), AIMessage(content="", tool_calls=calls, id="ai9")],
                  **OWNER_STATE})
    outcome, events = await w.turn("hello again")
    assert outcome.end_reason == "answered"
    first_prompt = Recording.seen[0]
    repaired = [m for m in first_prompt if isinstance(m, ToolMessage) and m.tool_call_id == "c9"]
    assert len(repaired) == 1 and '"status":"not_run"' in repaired[0].content
    assert [e.kind for e in events if e.type == "step.started"][:2] == ["repair", "memory"]
