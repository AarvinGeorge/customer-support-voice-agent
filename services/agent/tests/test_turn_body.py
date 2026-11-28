"""What: the turn body (TurnRunner) end to end through Ingress: repair, memory refresh and the turn message with
the untrusted account context (doc 07 s2.4 steps 10, 11, 14; s2.6; s5.3), and a core 401 that makes the model
see V0 on the same turn.
Use when: pytest (in-memory store and lock, contracts core stub over ASGI, recording fake model).
Inputs: none. Output: pass/fail. Side effects: none.
Role: Done when of CS-E-03.S7.T5 through the real ingress entry."""
from __future__ import annotations

import json
import uuid
from typing import Any

import httpx
from contracts.stubs import examples as ex
from contracts.stubs.core_stub import create_app
from contracts.agent_api import VoiceAskRequest
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.checkpoint.memory import InMemorySaver

from agent.assembly import build_agent, run_config
from agent.clients.core_agent import CoreAgentClient
from agent.clients.knowledge import KnowledgeClient
from agent.ingress import Ingress, IngressInput, IngressStream, SessionRow
from agent.tools import build_tools
from agent.turn_body import StructuredResult, TurnRunner
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
                 core_transport: httpx.AsyncBaseTransport | None = None, stub_verified: bool = True,
                 knowledge_seen: list[httpx.Request] | None = None, structured: Any = None,
                 session: SessionRow | None = None) -> None:
        """What: build it. Use when: tests. Inputs: see class. Output: none. Side effects: none. Role: constructor."""
        Recording.bound, Recording.seen = [], []
        self.stub = create_app()
        if stub_verified:
            self.stub.state.verified.add(SID)
        http = httpx.AsyncClient(transport=core_transport or httpx.ASGITransport(app=self.stub))
        self.core = CoreAgentClient("http://core:8000", "agent-tok", http=http)
        def kn(r: httpx.Request) -> httpx.Response:
            """What: knowledge is down; records the request. Use when: transport. Inputs: request. Output: 503.
            Side effects: appends to knowledge_seen. Role: fake."""
            if knowledge_seen is not None:
                knowledge_seen.append(r)
            return httpx.Response(503)

        knowledge = KnowledgeClient("http://knowledge:8000", "kn-tok",
                                    http=httpx.AsyncClient(transport=httpx.MockTransport(kn)))
        self.agent = build_agent(Recording(messages=iter(replies or [AIMessage("Here you go.")]), disable_streaming=True),
                                 fallback_model=Recording(messages=iter([])), checkpointer=InMemorySaver(),
                                 tools=build_tools(self.core, knowledge))
        self.store = MemoryTurnStore(session or SessionRow(session_id=SID, visitor_id=VISITOR))
        self.ingress = Ingress(self.store, MemoryTurnLock(), served_by="agent_a",
                               run_agent=TurnRunner(self.agent, self.core, structured=structured))

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

    async def voice(self, question: str, transcript: str | None, final: bool) -> tuple[Any, list[Any]]:
        """What: one voice/ask turn (the Realtime relay) through Ingress. Use when: voice tests. Inputs: the Realtime
        model's question, the customer transcript, transcript_final. Output: (outcome, events). Side effects: as a turn.
        Role: helper."""
        req = VoiceAskRequest(client_turn_id=uuid.uuid4(), voice_session_id="vs-1", realtime_call_id="call-1",
                              question=question, user_item_id="item-u1", user_transcript=transcript,
                              transcript_final=final, last_assistant_interrupted=False)
        res = await self.ingress.handle(IngressInput(session_id=SID, visitor_id=VISITOR,
                                                     client_turn_id=str(req.client_turn_id), mode="voice",
                                                     input_kind="voice", text=req.question,
                                                     realtime_call_id=req.realtime_call_id, body=req))
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
    assert Recording.bound[-1] == ["request_verification", "search_kb"]  # V0 (D-070: no create_ticket)
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


async def test_search_kb_sends_the_customers_own_text_for_gate_1() -> None:
    # CS-E-02.F11: Gate 1 scores the code-held customer message as well as the model's query (rca-gate1-weak-band.md).
    seen: list[httpx.Request] = []
    search = AIMessage("", tool_calls=[{"name": "search_kb", "args": {"query": "Android app for Quillstack"}, "id": "c1",
                                        "type": "tool_call"}])
    w = World(replies=[search, AIMessage("Here you go.")], knowledge_seen=seen)
    await w.seed({"messages": [HumanMessage("earlier"), AIMessage("ok", id="a0")], **OWNER_STATE})
    await w.turn("Do you have an Android app for Quillstack?")
    assert seen, "search_kb never reached knowledge"
    for r in seen:
        body = json.loads(r.content)
        assert body["query"] == "Android app for Quillstack"
        assert body["customer_message"] == "Do you have an Android app for Quillstack?"


def _search_world(seen: list[httpx.Request], **kw: Any) -> World:
    """What: a World whose model searches once, then answers. Use when: the Gate 1 input tests. Inputs: the request
    list, World options. Output: World. Side effects: none. Role: helper."""
    search = AIMessage("", tool_calls=[{"name": "search_kb", "args": {"query": "Android app"}, "id": "c1",
                                        "type": "tool_call"}])
    return World(replies=[search, AIMessage("Here you go.")], knowledge_seen=seen, **kw)


def _messages(seen: list[httpx.Request]) -> list[Any]:
    """What: the customer_message of each knowledge request (None when absent). Use when: asserting. Inputs: the
    recorded requests. Output: list. Side effects: none. Role: helper."""
    assert seen, "search_kb never reached knowledge"
    return [json.loads(r.content).get("customer_message") for r in seen]


async def test_voice_sends_the_customers_final_transcript_never_the_realtime_question() -> None:
    # Review PR #86 I1: in voice the question is written by the Realtime model; only the transcript is the customer's.
    seen: list[httpx.Request] = []
    w = _search_world(seen)
    await w.seed({"messages": [HumanMessage("earlier"), AIMessage("ok", id="a0")], **OWNER_STATE})
    await w.voice("Android app availability", "do you have an android app", final=True)
    assert set(_messages(seen)) == {"do you have an android app"}


async def test_voice_without_a_final_transcript_sends_no_customer_message() -> None:
    for transcript, final in (("do you have an android app", False), (None, True), ("   ", True)):
        seen: list[httpx.Request] = []
        w = _search_world(seen)
        await w.seed({"messages": [HumanMessage("earlier"), AIMessage("ok", id="a0")], **OWNER_STATE})
        await w.voice("Android app availability", transcript, final=final)
        assert set(_messages(seen)) == {None}, (transcript, final)


async def test_a_code_line_turn_sends_no_customer_message() -> None:
    # Review PR #86 I5 (mutation M9 survived): the structured result's model_text is a code line, never the customer.
    async def handler(stream: Any, inp: Any, prep: Any) -> StructuredResult:
        """What: a structured handler that hands the model a code line. Use when: this test. Inputs: stream, input,
        prep. Output: StructuredResult with model_text. Side effects: none. Role: fake."""
        return StructuredResult(model_text="The customer verified on the card.")

    seen: list[httpx.Request] = []
    w = _search_world(seen, structured=handler)
    await w.seed({"messages": [HumanMessage("earlier"), AIMessage("ok", id="a0")], **OWNER_STATE})
    await w.turn("anything")
    assert set(_messages(seen)) == {None}


async def test_a_pin_in_the_customer_text_is_redacted_before_it_reaches_knowledge() -> None:
    # Review PR #86 I5: awaiting verification, a bare 4-digit number is a PIN (doc 07 s11.1 condition 1).
    seen: list[httpx.Request] = []
    w = _search_world(seen, session=SessionRow(session_id=SID, visitor_id=VISITOR, awaiting_verification=True))
    await w.seed({"messages": [HumanMessage("earlier"), AIMessage("ok", id="a0")], **OWNER_STATE})
    await w.turn("it is 4821 what is the refund policy")
    for m in _messages(seen):
        assert m is not None and "4821" not in m and "[PIN]" in m
