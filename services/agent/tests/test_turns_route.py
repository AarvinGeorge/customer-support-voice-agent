"""What: tests for POST /api/sessions/{sid}/turns and the composition root (agent.composition, agent.routes.turns):
the route runs the shared Ingress with the create_agent graph (registry models, tools on the token-split core and
knowledge clients, checkpointer), streams contract events, and the same Ingress answers voice/ask.
Use when: pytest (in-process ASGI; fake chat models through the real create_agent graph; MockTransport knowledge;
in-memory turn store, lock and voice stores). Inputs: none. Output: pass or fail. Side effects: none.
Role: Done when of the composition gap (doc 07 s2.2, s2.3, s2.4, s2.6; prerequisite of CS-E-08.S5.T1)."""
from __future__ import annotations

import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from contracts.errors import ErrorEnvelope
from contracts.events import KEEPALIVE, parse_event
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.checkpoint.memory import InMemorySaver

from agent import composition
from agent.access import sign_cookie
from agent.ingress import Ingress, IngressInput, PgTurnStore, RedisTurnLock, SessionRow, TurnRow
from agent.main import create_app
from agent.models.config import load_models_config
from agent.realtime import SessionSummary
from agent.settings import Settings
from agent.sse import TurnOutcome, TurnStream
from agent.tools.catalog import TOOL_NAMES
from agent.voice import RedisCallKeys, TranscriptMerger, VoiceDeps, VoiceSessionRow
from ingress_fakes import MemoryTurnLock, MemoryTurnStore
from voice_fakes import FakeMinter, MemoryCallKeys, MemoryVoiceStore

KEY = "k" * 32
VISITOR = "visitor-1"
SID = "019a7c2e-5b10-7000-8000-00000000b001"
OTHER_SID = "019a7c2e-5b10-7000-8000-00000000b002"
ORIGIN = "https://test"
MODELS = Path(__file__).resolve().parents[3] / "data" / "models.yaml"
ANSWER = "Refunds on Pro are allowed within 30 days of the charge, per our refund policy."


class Fake(GenericFakeChatModel):
    """What: scripted fake chat model that records the messages of each call. Use when: tests.
    Inputs: messages, seen. Output: scripted replies. Side effects: appends to seen. Role: test double."""

    seen: list[Any] = []

    def bind_tools(self, tools: Any, **kw: Any) -> "Fake":
        """What: no-op. Use when: create_agent binds tools. Inputs: tools. Output: self. Side effects: none.
        Role: fake plumbing."""
        return self

    async def _agenerate(self, messages, stop=None, run_manager=None, **kw):  # type: ignore[override]
        """What: record then reply. Use when: model call. Inputs: messages. Output: ChatResult.
        Side effects: records. Role: fake."""
        self.seen.append(list(messages))
        return self._generate(messages, stop=stop, run_manager=None, **kw)


class FakeRegistry:
    """What: ModelRegistry stand-in: build(role) returns the scripted fake for agent and agent_fallback.
    Use when: build_agent_runtime in tests. Inputs: replies of the primary. Output: build. Side effects: records roles.
    Role: fake (the real registry is covered by test_registry)."""

    def __init__(self, *replies: AIMessage) -> None:
        """What: script the primary. Use when: tests. Inputs: replies. Output: none. Side effects: none. Role: fake."""
        self.primary = Fake(messages=iter(replies), disable_streaming=True, seen=[])
        self.roles: list[str] = []

    def build(self, role: str) -> Fake:
        """What: model for a role. Use when: build_agent_from_registry. Inputs: role. Output: Fake.
        Side effects: records. Role: fake."""
        self.roles.append(role)
        return self.primary if role == "agent" else Fake(messages=iter([]), seen=[])


def search_body() -> dict[str, Any]:
    """What: a contract-shaped SearchResponse with one relevant chunk. Use when: the fake knowledge service.
    Inputs: none. Output: dict. Side effects: none. Role: fixture."""
    chunk = {"chunk_id": "refund-policy#2", "article_slug": "refund-policy", "article_title": "Refund policy",
             "section_path": ["Refund policy", "Pro"], "text": "Refunds on Pro are allowed within 30 days.",
             "tiers": ["all"], "error_codes": [], "flags": [], "last_updated": "2026-11-20", "doc_version": 1,
             "content_hash": "h", "source_url": "https://notion.so/x", "relevant": True,
             "scores": {"cosine": 0.7, "vector_rank": 1, "keyword_rank": 1, "rrf_score": 0.03, "fused_rank": 1},
             "citation": {"ref": "refund-policy#2", "label": "Refund policy", "spoken": "our refund policy"}}
    return {"request_id": "r", "trace_id": "t", "corpus_version": "v1", "embedding_model": "m",
            "query": {"raw": "q", "normalized": "q", "codes": [], "unknown_codes": []}, "allowed_tiers": ["all"],
            "chunks": [chunk],
            "confidence": {"verdict": "pass", "reason": "above_threshold", "top_cosine": 0.7, "tau_low": 0.3,
                           "tau_high": 0.45, "support_count": 1, "code_match": False,
                           "legs": {"vector": "ok", "keyword": "ok"}, "calibration_id": None,
                           "calibration_status": "provisional", "calibration_stale": False},
            "timing_ms": {"embed": 1, "sql": 1, "total": 2}}


def settings(**kw: Any) -> Settings:
    """What: test settings with the token split. Use when: every test. Inputs: overrides. Output: Settings.
    Side effects: none. Role: helper."""
    base: dict[str, Any] = dict(served_by="agent_a", agent_db_url="postgresql://x", redis_url="redis://x",
                                core_url="http://core.test:8000", knowledge_url="http://knowledge.test:8000",
                                demo_faults_enabled=False, site_origin=ORIGIN, session_signing_key=KEY,
                                demo_access_code="c", core_token_agent="core-agent-tok",
                                knowledge_token_agent="knowledge-agent-tok")
    base.update(kw)
    return Settings(**base)


class Sessions(MemoryTurnStore):
    """What: MemoryTurnStore that also satisfies SessionStore. Use when: create_app(session_store=...).
    Inputs: sessions. Output: store. Side effects: none. Role: fake."""

    async def create_session(self, visitor_id: str, ip_hash: str | None) -> Any:
        """What: not used here. Use when: never. Inputs: ids. Output: raises. Side effects: none. Role: filler."""
        raise NotImplementedError


class World:
    """What: an app whose /turns and voice/ask run the composed Ingress over the real create_agent graph.
    Use when: each test. Inputs: primary replies; wire (False: no Ingress, the route answers 503).
    Output: attributes. Side effects: none. Role: test harness."""

    def __init__(self, *replies: AIMessage, wire: bool = True) -> None:
        """What: build. Use when: tests. Inputs: see class. Output: none. Side effects: none. Role: harness."""
        self.cfg = settings()
        self.turns = Sessions(SessionRow(session_id=SID, visitor_id=VISITOR),
                              SessionRow(session_id=OTHER_SID, visitor_id="visitor-2"))
        self.lock, self.keys, self.vstore = MemoryTurnLock(), MemoryCallKeys(), MemoryVoiceStore()
        self.knowledge_calls: list[httpx.Request] = []

        def knowledge(request: httpx.Request) -> httpx.Response:
            """What: fake knowledge /v1/search. Use when: search_kb. Inputs: request. Output: response.
            Side effects: records. Role: fake."""
            self.knowledge_calls.append(request)
            return httpx.Response(200, json=search_body())

        self.http = httpx.AsyncClient(transport=httpx.MockTransport(knowledge))
        self.registry = FakeRegistry(*replies)
        self.runtime = composition.build_agent_runtime(self.cfg, http=self.http, checkpointer=InMemorySaver(),
                                                       registry=self.registry)
        self.ingress = composition.build_ingress(self.runtime.agent, store=self.turns, lock=self.lock,
                                                 served_by="agent_a", call_keys=self.keys,
                                                 merge_transcript=TranscriptMerger(self.vstore))

        async def summary(sid: str) -> SessionSummary:
            """What: fake thread summary. Use when: voice/token. Inputs: sid. Output: summary. Side effects: none.
            Role: fake."""
            return SessionSummary()

        self.voice = VoiceDeps(store=self.vstore, minter=FakeMinter(),
                               voice_spec=load_models_config(MODELS).roles["voice"], summary=summary)
        if wire:
            self.voice.ingress = self.ingress

        async def ok() -> str:
            """What: healthy probe. Use when: readyz. Inputs: none. Output: ok. Side effects: none. Role: fake."""
            return "ok"

        self.app = create_app(self.cfg, probes={k: ok for k in ("redis", "postgres", "core", "knowledge")},
                              session_store=self.turns, voice=self.voice, ingress=self.ingress if wire else None)

    def client(self, cookie: bool = True) -> httpx.AsyncClient:
        """What: in-process client, with the visitor cookie unless told otherwise. Use when: tests.
        Inputs: cookie flag. Output: client. Side effects: none. Role: helper."""
        c = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url=ORIGIN)
        if cookie:
            c.cookies.set("qs_access", sign_cookie(KEY, visitor_id=VISITOR))
        return c


def events_of(resp: httpx.Response) -> list[Any]:
    """What: parse an SSE body with the contracts models (wire format checked). Use when: tests.
    Inputs: response. Output: events. Side effects: none. Role: validator."""
    assert resp.headers["content-type"].startswith("text/event-stream"), resp.text
    out = []
    for frame in resp.text.split("\n\n"):
        if not frame or frame + "\n\n" == KEEPALIVE:
            continue
        lines = frame.split("\n")
        ev: Any = parse_event(lines[2][len("data: "):])
        assert lines[0] == f"event: {ev.type}" and lines[1] == f"id: {ev.seq}"
        out.append(ev)
    return out


def code_of(resp: httpx.Response) -> str:
    """What: the envelope code. Use when: error tests. Inputs: response. Output: code. Side effects: none.
    Role: helper."""
    return ErrorEnvelope.model_validate(resp.json()).error.code


def kb_then_answer() -> list[AIMessage]:
    """What: the primary's script: one search_kb call, then the answer. Use when: tests. Inputs: none.
    Output: replies. Side effects: none. Role: fixture."""
    return [AIMessage(content="", tool_calls=[{"name": "search_kb", "args": {"query": "refund window"},
                                                "id": "call_kb1", "type": "tool_call"}]),
            AIMessage(content=ANSWER)]


# ---- POST /turns through the composed agent ----

async def test_text_turn_streams_contract_events_from_the_create_agent_graph() -> None:
    w = World(*kb_then_answer())
    async with w.client() as c:
        r = await c.post(f"/api/sessions/{SID}/turns", headers={"Origin": ORIGIN},
                         json={"client_turn_id": str(uuid.uuid4()), "text": "How long do I have to ask for a refund?"})
    assert r.status_code == 200, r.text
    evs = events_of(r)
    types = [e.type for e in evs]
    assert types[0] == "turn.started" and types[-1] == "turn.completed" and types.count("turn.completed") == 1
    assert [e.seq for e in evs] == list(range(1, len(evs) + 1))
    assert "token" in types and "step.started" in types
    assert "".join(e.text for e in evs if e.type == "token") == ANSWER
    done = evs[-1]
    assert done.final.text == ANSWER and done.end_reason == "answered" and done.replayed is False
    assert done.usage.model_calls == 2 and done.usage.tool_calls == 1
    assert r.headers["x-trace-id"] == done.turn_id
    (req,) = w.knowledge_calls
    assert str(req.url) == "http://knowledge.test:8000/v1/search"
    assert req.headers["authorization"] == "Bearer knowledge-agent-tok"
    first_call = w.registry.primary.seen[0]
    human = [m for m in first_call if isinstance(m, HumanMessage)][-1]
    assert "<turn_context>" in str(human.content) and "verified: false" in str(human.content)
    assert "How long do I have to ask for a refund?" in str(human.content)
    (row,) = w.turns.turns.values()
    assert row.status == "completed" and row.input_kind == "text" and row.mode == "chat"
    assert w.turns.sessions[SID].user_turns == 1 and w.lock.held == {}


async def test_resent_client_turn_id_replays_without_a_model_call() -> None:
    w = World(AIMessage(content="Hello, how can I help with Quillstack?"))
    body = {"client_turn_id": str(uuid.uuid4()), "text": "hi"}
    async with w.client() as c:
        first = await c.post(f"/api/sessions/{SID}/turns", json=body)
        again = await c.post(f"/api/sessions/{SID}/turns", json=body)
    assert events_of(first)[-1].replayed is False
    last = events_of(again)[-1]
    assert last.type == "turn.completed" and last.replayed is True
    assert last.final.text == "Hello, how can I help with Quillstack?"
    assert len(w.registry.primary.seen) == 1


async def test_other_visitors_session_is_404_without_a_hint() -> None:
    w = World()
    async with w.client() as c:
        r = await c.post(f"/api/sessions/{OTHER_SID}/turns", json={"client_turn_id": str(uuid.uuid4()), "text": "hi"})
        missing = await c.post(f"/api/sessions/{uuid.uuid4()}/turns",
                               json={"client_turn_id": str(uuid.uuid4()), "text": "hi"})
    assert (r.status_code, code_of(r)) == (404, "NOT_FOUND")
    assert (missing.status_code, code_of(missing)) == (404, "NOT_FOUND")
    assert w.turns.turns == {}


@pytest.mark.parametrize("body", [
    {"client_turn_id": "not-a-uuid", "text": "hi"},
    {"client_turn_id": str(uuid.uuid4())},
    {"client_turn_id": str(uuid.uuid4()), "text": "hi", "ticket_offer": {"accept": True}},
    {"client_turn_id": str(uuid.uuid4()), "text": "x" * 2001},
    {"client_turn_id": str(uuid.uuid4()), "text": "hi", "extra": 1},
])
async def test_invalid_body_is_422_validation_error_and_writes_nothing(body: dict[str, Any]) -> None:
    w = World()
    async with w.client() as c:
        r = await c.post(f"/api/sessions/{SID}/turns", json=body)
    assert (r.status_code, code_of(r)) == (422, "VALIDATION_ERROR")
    assert "x" * 50 not in r.text  # input never echoed
    assert w.turns.turns == {} and w.lock.held == {}


async def test_non_json_body_is_422() -> None:
    w = World()
    async with w.client() as c:
        r = await c.post(f"/api/sessions/{SID}/turns", content=b"{nope", headers={"content-type": "application/json"})
    assert (r.status_code, code_of(r)) == (422, "VALIDATION_ERROR")


async def test_foreign_origin_is_403_and_no_cookie_is_401() -> None:
    w = World()
    body = {"client_turn_id": str(uuid.uuid4()), "text": "hi"}
    async with w.client() as c:
        foreign = await c.post(f"/api/sessions/{SID}/turns", json=body, headers={"Origin": "https://evil.example"})
    async with w.client(cookie=False) as c:
        anon = await c.post(f"/api/sessions/{SID}/turns", json=body)
    assert (foreign.status_code, code_of(foreign)) == (403, "ORIGIN_MISMATCH")
    assert (anon.status_code, code_of(anon)) == (401, "ACCESS_DENIED")
    assert w.turns.turns == {}


async def test_route_without_an_ingress_answers_503_busy() -> None:
    w = World(wire=False)
    async with w.client() as c:
        r = await c.post(f"/api/sessions/{SID}/turns", json={"client_turn_id": str(uuid.uuid4()), "text": "hi"})
    assert (r.status_code, code_of(r)) == (503, "BUSY") and r.headers["retry-after"] == "3"


async def test_structured_input_without_its_handler_gets_a_fixed_line_and_no_model_call() -> None:
    w = World()
    async with w.client() as c:
        r = await c.post(f"/api/sessions/{SID}/turns", json={"client_turn_id": str(uuid.uuid4()),
                                                             "ticket_offer": {"accept": True}})
    done = events_of(r)[-1]
    assert done.final.text == composition.UNSUPPORTED_INPUT_LINE and done.end_reason == "error"
    assert w.registry.primary.seen == []


# ---- voice/ask through the same Ingress ----

async def test_voice_ask_returns_a_say_from_the_same_agent() -> None:
    w = World(AIMessage(content="You can ask for a refund within 30 days."))
    w.vstore.voice["vs-1"] = VoiceSessionRow("vs-1", SID, w.voice.now(), w.voice.now())
    body = {"client_turn_id": str(uuid.uuid4()), "voice_session_id": "vs-1", "realtime_call_id": "call_1",
            "question": "How long is the refund window?", "user_item_id": "item_u1",
            "user_transcript": "how long is the refund window", "transcript_final": True,
            "last_assistant_interrupted": False}
    async with w.client() as c:
        r = await c.post(f"/api/sessions/{SID}/voice/ask", json=body)
    assert r.status_code == 200, r.text
    done = events_of(r)[-1]
    assert done.type == "turn.completed" and done.mode == "voice"
    assert done.final.say == "You can ask for a refund within 30 days."
    human = [m for m in w.registry.primary.seen[0] if isinstance(m, HumanMessage)][-1]
    assert "mode: voice" in str(human.content) and "How long is the refund window?" in str(human.content)
    assert "how long is the refund window" in str(human.content)


# ---- the turn body ----

def test_voice_user_text_adds_a_final_transcript_that_differs() -> None:
    body = SimpleNamespace(question="Refund window?", user_transcript="what is the refund window", transcript_final=True)
    assert composition.voice_user_text(body) == "Refund window?\n(the customer said: what is the refund window)"
    body.transcript_final = False
    assert composition.voice_user_text(body) == "Refund window?"
    assert composition.voice_user_text(SimpleNamespace(question="refund window?", user_transcript=" refund window? ",
                                                       transcript_final=True)) == "refund window?"


async def test_merged_transcript_goes_before_the_turn_message() -> None:
    w = World(AIMessage(content="Sure."))
    stream = TurnStream(composition_meta())
    row = TurnRow(turn_id="019a7c2e-5b20-7000-8000-0000000000d1", session_id=SID, client_turn_id="c", mode="chat",
                  input_kind="text", status="running", deadline_at=_far(), started_at=_far())
    inp = IngressInput(session_id=SID, visitor_id=VISITOR, client_turn_id="c", mode="chat", input_kind="text",
                       text="and the window?", merged_transcript='<voice_transcript source="client">x</voice_transcript>')
    out = await composition.AgentTurnBody(w.runtime.agent)(stream, inp, row)
    assert isinstance(out, TurnOutcome) and out.final.text == "Sure."
    humans = [m for m in w.registry.primary.seen[0] if isinstance(m, HumanMessage)]
    assert str(humans[-2].content).startswith("<voice_transcript") and "and the window?" in str(humans[-1].content)


# ---- the production wiring ----

def test_build_agent_runtime_uses_both_registry_roles_all_tools_and_the_token_split() -> None:
    reg = FakeRegistry()
    rt = composition.build_agent_runtime(settings(), http=httpx.AsyncClient(), checkpointer=InMemorySaver(),
                                         registry=reg)
    assert sorted(reg.roles) == ["agent", "agent_fallback"]
    assert tuple(t.name for t in rt.tools) == TOOL_NAMES
    assert (rt.core._base, rt.core._token) == ("http://core.test:8000", "core-agent-tok")
    assert (rt.knowledge._base, rt.knowledge._token) == ("http://knowledge.test:8000", "knowledge-agent-tok")
    assert rt.agent.checkpointer is not None


def test_settings_read_the_agent_tokens_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CORE_TOKEN_AGENT", "a1")
    monkeypatch.setenv("KNOWLEDGE_TOKEN_AGENT", "k1")
    s = Settings.from_env()
    assert (s.core_token_agent, s.knowledge_token_agent) == ("a1", "k1")


def test_wire_app_attaches_one_ingress_to_turns_and_voice(monkeypatch: pytest.MonkeyPatch) -> None:
    built: dict[str, Any] = {}

    def fake_runtime(cfg: Settings, *, http: Any, checkpointer: Any, registry: Any = None) -> Any:
        """What: stand-in runtime. Use when: this test. Inputs: see build_agent_runtime. Output: namespace.
        Side effects: records. Role: fake."""
        built.update(http=http, checkpointer=checkpointer)
        return SimpleNamespace(agent=object())

    monkeypatch.setattr(composition, "build_agent_runtime", fake_runtime)
    voice = VoiceDeps(store=MemoryVoiceStore(), minter=FakeMinter(), voice_spec=None, summary=None)  # type: ignore[arg-type]
    state = SimpleNamespace(checkpointer=object(), pg_pool=object(), redis=object(), http=object(), voice=voice)
    app = SimpleNamespace(state=state)
    ingress = composition.wire_app(app, settings())  # type: ignore[arg-type]
    assert isinstance(ingress, Ingress) and state.ingress is ingress and voice.ingress is ingress
    assert isinstance(ingress.store, PgTurnStore) and isinstance(ingress.lock, RedisTurnLock)
    assert isinstance(ingress.call_keys, RedisCallKeys) and isinstance(ingress.merge_transcript, TranscriptMerger)
    assert ingress.served_by == "agent_a" and built["checkpointer"] is state.checkpointer


def test_wire_app_without_a_checkpointer_leaves_the_routes_unwired(caplog: pytest.LogCaptureFixture) -> None:
    state = SimpleNamespace(checkpointer=None, pg_pool=object(), redis=object(), http=object(), voice=None)
    assert composition.wire_app(SimpleNamespace(state=state), settings()) is None  # type: ignore[arg-type]
    assert state.ingress is None and "turns are off" in caplog.text


def test_wire_app_logs_and_stays_unwired_when_models_yaml_is_missing(caplog: pytest.LogCaptureFixture) -> None:
    state = SimpleNamespace(checkpointer=InMemorySaver(), pg_pool=object(), redis=object(),
                            http=httpx.AsyncClient(), voice=None)
    cfg = settings(models_path="/nonexistent/models.yaml")
    assert composition.wire_app(SimpleNamespace(state=state), cfg) is None  # type: ignore[arg-type]
    assert state.ingress is None and "turns are off" in caplog.text


def composition_meta() -> Any:
    """What: TurnMeta for the direct body test. Use when: one test. Inputs: none. Output: TurnMeta.
    Side effects: none. Role: helper."""
    from agent.sse import TurnMeta
    return TurnMeta(SID, "019a7c2e-5b20-7000-8000-0000000000d1", "chat", "agent_a", "text")


def _far() -> Any:
    """What: a deadline far in the future. Use when: the direct body test. Inputs: none. Output: datetime.
    Side effects: none. Role: helper."""
    from datetime import UTC, datetime, timedelta
    return datetime.now(UTC) + timedelta(seconds=45)



async def test_body_reuses_a_turn_control_that_ingress_already_bound() -> None:
    from agent.turn_control import TurnControl, bind_turn_control
    w = World(AIMessage(content="Sure."))
    stream = TurnStream(composition_meta())
    row = TurnRow(turn_id="019a7c2e-5b20-7000-8000-0000000000d1", session_id=SID, client_turn_id="c", mode="chat",
                  input_kind="text", status="running", deadline_at=_far(), started_at=_far())
    bound = TurnControl(session_id=SID, turn_id=row.turn_id, mode="chat", demo_fault="tool_error", events=stream)
    bind_turn_control(bound)
    inp = IngressInput(session_id=SID, visitor_id=VISITOR, client_turn_id="c", mode="chat", input_kind="text", text="hi")
    seen: list[Any] = []
    real = composition.stream_agent_turn

    async def spy(*a: Any, **kw: Any) -> Any:
        """What: record the control passed. Use when: this test. Inputs: adapter args. Output: outcome.
        Side effects: records. Role: spy."""
        seen.append(kw.get("control"))
        return await real(*a, **kw)

    composition.stream_agent_turn = spy  # type: ignore[assignment]
    try:
        await composition.AgentTurnBody(w.runtime.agent)(stream, inp, row)
    finally:
        composition.stream_agent_turn = real  # type: ignore[assignment]
    assert seen == [bound]


def test_wire_app_passes_the_trajectory_writer_when_the_app_has_one(monkeypatch: pytest.MonkeyPatch) -> None:
    got: dict[str, Any] = {}
    monkeypatch.setattr(composition, "build_agent_runtime",
                        lambda cfg, **kw: SimpleNamespace(agent=object()))
    monkeypatch.setattr(composition, "Ingress", lambda *a, **kw: got.update(kw) or SimpleNamespace())
    sentinel = object()
    state = SimpleNamespace(checkpointer=object(), pg_pool=object(), redis=object(), http=object(), voice=None,
                            trajectory=sentinel)
    composition.wire_app(SimpleNamespace(state=state), settings())  # type: ignore[arg-type]
    assert got["trajectory"] is sentinel
