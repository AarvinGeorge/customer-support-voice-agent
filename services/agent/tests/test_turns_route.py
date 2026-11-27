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
from contracts.stubs import examples as ex
from contracts.stubs.core_stub import create_app as create_core_stub
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
from agent.structured.confirm import CoreCommitClient
from agent.escalation import NOTHING_TO_OFFER_LINE
from agent.structured.handler import StructuredInput
from agent.tools.catalog import TOOL_NAMES
from agent.turn_body import TurnRunner, voice_user_text
from agent.voice import RedisCallKeys, TranscriptMerger, VoiceDeps, VoiceSessionRow
from ingress_fakes import MemoryTurnLock, MemoryTurnStore
from voice_fakes import FakeMinter, MemoryCallKeys, MemoryVoiceStore

KEY = "k" * 32
VISITOR = "visitor-1"
SID = "019a7c2e-5b10-7000-8000-00000000b001"
OTHER_SID = "019a7c2e-5b10-7000-8000-00000000b002"
ORIGIN = "https://test"
COMMIT_FROM_ENV = "commit-from-env-" + "x" * 27  # 43 characters, the keys.py gen shape
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

    def cost_for_model(self, model_name: str, usage: Any) -> float:
        """What: a fixed price per model call. Use when: stream_agent_turn(cost_of=...). Inputs: name, usage.
        Output: 0.001. Side effects: none. Role: fake (real pricing is test_registry and test_usage)."""
        return 0.001


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


class Flags:
    """What: in-memory SessionFlags. Use when: World. Inputs: none. Output: records. Side effects: none. Role: fake."""

    def __init__(self) -> None:
        """What: empty log. Use when: tests. Inputs: none. Output: none. Side effects: none. Role: fake."""
        self.calls: list[tuple[str, bool, str | None]] = []

    async def set_awaiting_verification(self, session_id: str, awaiting: bool, verified_user_id: str | None = None) -> None:
        """What: record. Use when: verification. Inputs: id, flag, user. Output: none. Side effects: records.
        Role: fake."""
        self.calls.append((session_id, awaiting, verified_user_id))


class Hosts(httpx.AsyncBaseTransport):
    """What: route core.test to the contracts core stub (ASGI) and knowledge.test to a fake search.
    Use when: World's shared HTTP client. Inputs: core stub app, knowledge handler. Output: responses.
    Side effects: none outside memory. Role: test transport."""

    def __init__(self, core_app: Any, knowledge: Any) -> None:
        """What: keep both. Use when: World. Inputs: stub, handler. Output: none. Side effects: none. Role: fake."""
        self.core, self.knowledge = httpx.ASGITransport(app=core_app), httpx.MockTransport(knowledge)

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        """What: dispatch by host. Use when: every request. Inputs: request. Output: response. Side effects: none.
        Role: router."""
        target = self.core if request.url.host == "core.test" else self.knowledge
        return await target.handle_async_request(request)


class World:
    """What: an app whose /turns and voice/ask run the composed Ingress (TurnRunner, StructuredInput, Confirmer)
    over the real create_agent graph, with the contracts core stub behind the agent and commit clients.
    Use when: each test. Inputs: primary replies; wire (False: no Ingress, the route answers 503); verified (the stub
    session starts verified); settings overrides. Output: attributes. Side effects: none. Role: test harness."""

    def __init__(self, *replies: AIMessage, wire: bool = True, verified: bool = False, **cfg: Any) -> None:
        """What: build. Use when: tests. Inputs: see class; settings overrides. Output: none. Side effects: none.
        Role: harness."""
        self.cfg = settings(**cfg)
        self.turns = Sessions(SessionRow(session_id=SID, visitor_id=VISITOR),
                              SessionRow(session_id=OTHER_SID, visitor_id="visitor-2"))
        self.lock, self.keys, self.vstore = MemoryTurnLock(), MemoryCallKeys(), MemoryVoiceStore()
        self.knowledge_calls: list[httpx.Request] = []

        def knowledge(request: httpx.Request) -> httpx.Response:
            """What: fake knowledge /v1/search. Use when: search_kb. Inputs: request. Output: response.
            Side effects: records. Role: fake."""
            self.knowledge_calls.append(request)
            return httpx.Response(200, json=search_body())

        self.stub = create_core_stub()
        if verified:
            self.stub.state.verified.add(SID)
        self.http = httpx.AsyncClient(transport=Hosts(self.stub, knowledge))
        self.registry = FakeRegistry(*replies)
        self.runtime = composition.build_agent_runtime(self.cfg, http=self.http, checkpointer=InMemorySaver(),
                                                       registry=self.registry)
        self.flags = Flags()
        self.ingress = composition.build_ingress(
            self.runtime, store=self.turns, lock=self.lock, served_by="agent_a", call_keys=self.keys,
            merge_transcript=TranscriptMerger(self.vstore), flags=self.flags,
            commit=CoreCommitClient(self.cfg.core_url, "commit-tok", http=self.http))

        async def summary(sid: str, context: object) -> SessionSummary:
            """What: fake thread summary. Use when: voice/token. Inputs: sid, RedactContext (ignored). Output: summary. Side effects: none.
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

    def core_calls(self, suffix: str) -> list[Any]:
        """What: stub calls whose path ends with suffix. Use when: asserting. Inputs: suffix. Output: calls.
        Side effects: none. Role: helper."""
        return [c for c in self.stub.state.calls if c.path.endswith(suffix)]

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
    assert done.usage.cost_usd_est == pytest.approx(0.002)  # cost_of reaches the adapter (registry prices)
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


async def test_ticket_offer_reaches_the_structured_handler_with_no_model_call() -> None:
    w = World()
    async with w.client() as c:
        r = await c.post(f"/api/sessions/{SID}/turns", json={"client_turn_id": str(uuid.uuid4()),
                                                             "ticket_offer": {"accept": True}})
    done = events_of(r)[-1]
    assert done.final.text == NOTHING_TO_OFFER_LINE  # no apology in the thread: nothing to offer (s6.7)
    assert w.registry.primary.seen == []


async def test_verification_card_through_the_route_verifies_and_never_streams_the_pin() -> None:
    w = World(AIMessage(content="Thanks Priya, I can see your account."))
    async with w.client() as c:
        r = await c.post(f"/api/sessions/{SID}/turns", json={"client_turn_id": str(uuid.uuid4()),
                                                             "verification": {"account_id": "qs 10423",
                                                                              "pin": ex.STUB_PIN}})
    evs = events_of(r)
    (res,) = [e for e in evs if e.type == "verification.result"]
    assert res.status == "verified" and res.person.display_name == "Priya Raman"
    assert ex.STUB_PIN not in r.text and w.core_calls("/identity/verify")[0].token == "core-agent-tok"
    assert w.flags.calls == [(SID, False, ex.CLAIMS.user_id)]


PREPARE_CALL = {"name": "prepare_refund", "args": {"ending": "0413", "on": "2026-11-21", "reason": "duplicate",
                                                   "note": "charged twice"}, "id": "p1", "type": "tool_call"}


async def s1_readback(w: World, c: httpx.AsyncClient) -> list[Any]:
    """What: verify by card, then ask for the duplicate refund (the model calls prepare_refund). Use when: S1 tests.
    Inputs: world, client. Output: the read-back turn's events. Side effects: as turns. Role: helper."""
    v = await c.post(f"/api/sessions/{SID}/turns", json={"client_turn_id": str(uuid.uuid4()),
                                                         "verification": {"account_id": "QS-10423", "pin": ex.STUB_PIN}})
    assert [e for e in events_of(v) if e.type == "verification.result"][0].status == "verified"
    r = await c.post(f"/api/sessions/{SID}/turns", json={"client_turn_id": str(uuid.uuid4()),
                                                         "text": "Please refund the duplicate $96 charge from 21 November"})
    return events_of(r)


async def test_s1_through_the_route_readback_card_then_confirm_press_commits_on_the_commit_token() -> None:
    w = World(AIMessage(content="Thanks, verified."), AIMessage(content="", tool_calls=[PREPARE_CALL]))
    async with w.client() as c:
        rb = await s1_readback(w, c)
        (conf,) = [e for e in rb if e.type == "confirmation.required"]
        assert conf.pending.pending_action_id == ex.PENDING_ID and conf.pending.amount_cents == 9600
        assert rb[-1].end_reason == "readback"
        r = await c.post(f"/api/sessions/{SID}/turns", json={"client_turn_id": str(uuid.uuid4()),
                                                             "confirm": {"pending_action_id": ex.PENDING_ID,
                                                                         "decision": "confirm"}})
    evs = events_of(r)
    (res,) = [e for e in evs if e.type == "action.result"]
    assert res.outcome == "committed" and evs[-1].end_reason == "committed"
    (commit,) = w.core_calls("/commit")
    assert commit.token == "commit-tok" and commit.headers["x-turn-id"] == evs[0].turn_id


async def test_s1_typed_yes_after_the_readback_commits_so_last_turn_is_wired() -> None:
    w = World(AIMessage(content="Thanks, verified."), AIMessage(content="", tool_calls=[PREPARE_CALL]))
    async with w.client() as c:
        await s1_readback(w, c)
        r = await c.post(f"/api/sessions/{SID}/turns", json={"client_turn_id": str(uuid.uuid4()), "text": "yes"})
    evs = events_of(r)
    assert evs[-1].end_reason == "committed" and len(w.core_calls("/commit")) == 1


def test_structured_input_requires_last_turn() -> None:
    with pytest.raises(TypeError):
        StructuredInput(object(), object(), flags=None)  # type: ignore[arg-type, call-arg]


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
    assert done.final.say == "You can ask for a refund within thirty days."  # spoken form (CS-E-05.S2.T1)
    human = [m for m in w.registry.primary.seen[0] if isinstance(m, HumanMessage)][-1]
    assert "mode: voice" in str(human.content) and "How long is the refund window?" in str(human.content)
    assert "how long is the refund window" in str(human.content)


# ---- the turn body ----

def test_voice_user_text_adds_a_final_transcript_that_differs() -> None:
    body = SimpleNamespace(question="Refund window?", user_transcript="what is the refund window", transcript_final=True)
    assert voice_user_text(body) == "Refund window?\n(the customer said: what is the refund window)"
    body.transcript_final = False
    assert voice_user_text(body) == "Refund window?"
    assert voice_user_text(SimpleNamespace(question="refund window?", user_transcript=" refund window? ",
                                                       transcript_final=True)) == "refund window?"


async def test_merged_transcript_goes_before_the_turn_message() -> None:
    w = World(AIMessage(content="Sure."))
    stream = TurnStream(composition_meta())
    row = TurnRow(turn_id="019a7c2e-5b20-7000-8000-0000000000d1", session_id=SID, client_turn_id="c", mode="chat",
                  input_kind="text", status="running", deadline_at=_far(), started_at=_far())
    inp = IngressInput(session_id=SID, visitor_id=VISITOR, client_turn_id="c", mode="chat", input_kind="text",
                       text="and the window?", merged_transcript='<voice_transcript source="client">x</voice_transcript>')
    out = await w.ingress.run_agent(stream, inp, row)
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
    assert rt.agent.checkpointer is not None and rt.registry is reg


def test_settings_read_the_agent_tokens_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CORE_TOKEN_AGENT", "a1")
    monkeypatch.setenv("KNOWLEDGE_TOKEN_AGENT", "k1")
    s = Settings.from_env()
    assert (s.core_token_agent, s.knowledge_token_agent) == ("a1", "k1")


def fake_rt() -> Any:
    """What: a runtime stand-in with the fields build_ingress reads. Use when: wire_app tests. Inputs: none.
    Output: namespace. Side effects: none. Role: fake."""
    reg = FakeRegistry()
    return SimpleNamespace(agent=object(), core=object(), registry=reg)


def test_wire_app_attaches_one_ingress_to_turns_and_voice(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CORE_TOKEN_COMMIT", COMMIT_FROM_ENV)
    built: dict[str, Any] = {}

    def fake_runtime(cfg: Settings, *, http: Any, checkpointer: Any, registry: Any = None) -> Any:
        """What: stand-in runtime. Use when: this test. Inputs: see build_agent_runtime. Output: namespace.
        Side effects: records. Role: fake."""
        built.update(http=http, checkpointer=checkpointer)
        return fake_rt()

    monkeypatch.setattr(composition, "build_agent_runtime", fake_runtime)
    voice = VoiceDeps(store=MemoryVoiceStore(), minter=FakeMinter(), voice_spec=None, summary=None)  # type: ignore[arg-type]
    state = SimpleNamespace(checkpointer=object(), pg_pool=object(), redis=object(), http=object(), voice=voice)
    app = SimpleNamespace(state=state)
    ingress = composition.wire_app(app, settings())  # type: ignore[arg-type]
    assert isinstance(ingress, Ingress) and state.ingress is ingress and voice.ingress is ingress
    assert isinstance(ingress.store, PgTurnStore) and isinstance(ingress.lock, RedisTurnLock)
    assert isinstance(ingress.call_keys, RedisCallKeys) and isinstance(ingress.merge_transcript, TranscriptMerger)
    assert ingress.served_by == "agent_a" and built["checkpointer"] is state.checkpointer
    body = ingress.run_agent
    assert isinstance(body, TurnRunner) and isinstance(body.structured, StructuredInput)
    assert body.structured.last_turn == ingress.store.last_turn  # typed yes can commit (rereview PR #47 N1)
    assert body.structured.confirmer.client._token == COMMIT_FROM_ENV
    assert body.cost_of is not None


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
    import agent.turn_body as tb
    real = tb.stream_agent_turn

    async def spy(*a: Any, **kw: Any) -> Any:
        """What: record the control passed. Use when: this test. Inputs: adapter args. Output: outcome.
        Side effects: records. Role: spy."""
        seen.append(kw.get("control"))
        return await real(*a, **kw)

    tb.stream_agent_turn = spy  # type: ignore[assignment]
    try:
        await w.ingress.run_agent(stream, inp, row)
    finally:
        tb.stream_agent_turn = real  # type: ignore[assignment]
    assert seen == [bound]


def test_wire_app_passes_the_trajectory_writer_when_the_app_has_one(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CORE_TOKEN_COMMIT", COMMIT_FROM_ENV)
    got: dict[str, Any] = {}
    monkeypatch.setattr(composition, "build_agent_runtime", lambda cfg, **kw: fake_rt())
    monkeypatch.setattr(composition, "Ingress", lambda *a, **kw: got.update(kw) or SimpleNamespace())
    sentinel = object()
    state = SimpleNamespace(checkpointer=object(), pg_pool=object(), redis=object(), http=object(), voice=None,
                            trajectory=sentinel)
    composition.wire_app(SimpleNamespace(state=state), settings())  # type: ignore[arg-type]
    assert got["trajectory"] is sentinel


# ---- wiring part 1 (CS-E-08.S5): demo fault and startup wiring check ----

class SpyIngress:
    """What: records the IngressInput a route hands over and answers 404. Use when: demo fault tests.
    Inputs: none. Output: handle. Side effects: records. Role: spy."""

    def __init__(self) -> None:
        """What: empty log. Use when: tests. Inputs: none. Output: none. Side effects: none. Role: spy."""
        self.inputs: list[IngressInput] = []
        self.call_keys, self.merge_transcript = object(), object()  # voice/ask checks the relay hooks exist

    async def handle(self, inp: IngressInput) -> Any:
        """What: record. Use when: the route. Inputs: input. Output: IngressError. Side effects: records. Role: spy."""
        from agent.ingress import IngressError
        self.inputs.append(inp)
        return IngressError(404, "not_found", "NOT_FOUND", "Not found.")


@pytest.mark.parametrize("enabled,header,expected", [
    (True, "llm_429", "llm_429"), (True, "tool_error", "tool_error"), (True, "bogus", None),
    (False, "llm_429", None), (True, None, None)])
async def test_turn_route_passes_the_gated_demo_fault_on_ingress_input(enabled: bool, header: str | None,
                                                                       expected: str | None) -> None:
    w = World(demo_faults_enabled=enabled)
    spy = SpyIngress()
    w.app.state.ingress = spy
    headers = {"X-Demo-Fault": header} if header else {}
    async with w.client() as c:
        await c.post(f"/api/sessions/{SID}/turns", json={"client_turn_id": str(uuid.uuid4()), "text": "hi"},
                     headers=headers)
    (inp,) = spy.inputs
    assert inp.demo_fault == expected


async def test_voice_ask_passes_the_gated_demo_fault_on_ingress_input() -> None:
    w = World(demo_faults_enabled=True)
    spy = SpyIngress()
    w.voice.ingress = spy  # type: ignore[assignment]
    w.vstore.voice["vs-1"] = VoiceSessionRow("vs-1", SID, w.voice.now(), w.voice.now())
    body = {"client_turn_id": str(uuid.uuid4()), "voice_session_id": "vs-1", "realtime_call_id": "call_9",
            "question": "q", "user_item_id": None, "user_transcript": None, "transcript_final": False,
            "last_assistant_interrupted": False}
    async with w.client() as c:
        r = await c.post(f"/api/sessions/{SID}/voice/ask", json=body, headers={"X-Demo-Fault": "llm_429"})
    assert r.status_code != 503, r.text
    (inp,) = spy.inputs
    assert inp.demo_fault == "llm_429"


def wired_state(**over: Any) -> Any:
    """What: an app whose state looks like the production lifespan after wire_app. Use when: wiring checks.
    Inputs: overrides. Output: namespace app. Side effects: none. Role: fixture."""
    w = World()
    w.ingress.trajectory = object()
    w.voice.ingress = w.ingress
    state = SimpleNamespace(ingress=w.ingress, voice=w.voice, trajectory=w.ingress.trajectory,
                            settings=settings(core_token_agent=STRONG_A, knowledge_token_agent=STRONG_K))
    for k, v in over.items():
        setattr(state, k, v)
    return SimpleNamespace(state=state)


STRONG_A, STRONG_K, STRONG_C = "a" * 43, "b" * 43, "c" * 43  # the shape keys.py gen writes (43 characters)


def test_wiring_gaps_is_empty_for_the_production_shape_apart_from_the_named_pending_pieces() -> None:
    gaps, pending = composition.wiring_gaps(wired_state())
    assert gaps == []
    assert all(p.startswith(("Gate 2 verifier", "PIN scanner")) for p in pending), pending


@pytest.mark.parametrize("over,gap", [
    ({"ingress": None}, "ingress"),
    ({"trajectory": None}, "trajectory"),
])
def test_wiring_gaps_names_each_missing_hook(over: dict[str, Any], gap: str) -> None:
    app = wired_state(**over)
    if gap == "trajectory":
        app.state.ingress.trajectory = None
    gaps, _ = composition.wiring_gaps(app)
    assert any(g.startswith(gap) for g in gaps), gaps


def test_wiring_gaps_flags_an_ingress_without_the_voice_relay_hooks() -> None:
    app = wired_state()
    app.state.ingress.call_keys = None
    gaps, _ = composition.wiring_gaps(app)
    assert any(g.startswith("voice relay") for g in gaps), gaps


def test_wiring_gaps_flags_voice_without_the_shared_ingress() -> None:
    app = wired_state()
    app.state.voice.ingress = None
    gaps, _ = composition.wiring_gaps(app)
    assert any(g.startswith("voice.ingress") for g in gaps), gaps


def test_assert_wired_raises_naming_the_gap() -> None:
    with pytest.raises(RuntimeError, match="trajectory"):
        app = wired_state()
        app.state.ingress.trajectory = None
        composition.assert_wired(app)


def test_gate_2_is_named_pending_exactly_while_the_grounding_stub_is_in_the_stack() -> None:
    from agent.middleware.stubs import STUB_NAMES
    _, pending = composition.wiring_gaps(wired_state())
    named = any(p.startswith("Gate 2 verifier") for p in pending)
    assert named == ("GroundingMiddleware" in STUB_NAMES), pending


def test_the_pin_scanner_is_named_pending_when_its_module_is_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    import importlib
    real = importlib.import_module

    def no_pin_guard(name: str, *a: Any) -> Any:
        """What: import_module without agent.pin_guard. Use when: this test. Inputs: module name. Output: module.
        Side effects: none. Role: fake."""
        if name == "agent.pin_guard":
            raise ModuleNotFoundError(name)
        return real(name, *a)

    monkeypatch.setattr(composition.importlib, "import_module", no_pin_guard)
    _, pending = composition.wiring_gaps(wired_state())
    assert any(p.startswith("PIN scanner") and "absent" in p for p in pending), pending


def test_the_pin_scanner_is_named_pending_when_it_is_a_no_op(monkeypatch: pytest.MonkeyPatch) -> None:
    import types

    import agent.ingress as ingress_module
    fake = types.ModuleType("agent.pin_guard")

    def scan_input(inp: Any, ctx: Any) -> Any:
        """What: a scanner that changes nothing. Use when: this test. Inputs: input, context. Output: same input.
        Side effects: none. Role: fake no-op."""
        return SimpleNamespace(inp=inp, refuse=False, redacted=0)

    fake.scan_input = scan_input  # type: ignore[attr-defined]
    monkeypatch.setitem(__import__("sys").modules, "agent.pin_guard", fake)
    monkeypatch.setattr(ingress_module, "scan_input", scan_input, raising=False)
    _, pending = composition.wiring_gaps(wired_state())
    assert any(p.startswith("PIN scanner") and "no-op" in p for p in pending), pending


def test_the_pin_scanner_is_not_pending_when_ingress_runs_a_real_scan(monkeypatch: pytest.MonkeyPatch) -> None:
    import types

    import agent.ingress as ingress_module
    fake = types.ModuleType("agent.pin_guard")

    def scan_input(inp: Any, ctx: Any) -> Any:
        """What: a scanner that redacts the probe PIN. Use when: this test. Inputs: input, context.
        Output: Scanned-like. Side effects: none. Role: fake real scanner."""
        from dataclasses import replace
        return SimpleNamespace(inp=replace(inp, text=(inp.text or "").replace("4821", "[PIN]")), refuse=True,
                               redacted=1)

    fake.scan_input = scan_input  # type: ignore[attr-defined]
    monkeypatch.setitem(__import__("sys").modules, "agent.pin_guard", fake)
    monkeypatch.setattr(ingress_module, "scan_input", scan_input, raising=False)
    _, pending = composition.wiring_gaps(wired_state())
    assert not any(p.startswith("PIN scanner") for p in pending), pending


@pytest.mark.parametrize("field,name", [("core_token_agent", "CORE_TOKEN_AGENT"),
                                        ("knowledge_token_agent", "KNOWLEDGE_TOKEN_AGENT"),
                                        ("session_signing_key", "SESSION_SIGNING_KEY")])
@pytest.mark.parametrize("value", ["", "   ", "changeme", "replace-me-with-a-real-token-please", "short-token"])
def test_wiring_gaps_rejects_an_empty_or_placeholder_secret(field: str, name: str, value: str) -> None:
    app = wired_state()
    app.state.settings = settings(**{"core_token_agent": STRONG_A, "knowledge_token_agent": STRONG_K, field: value})
    gaps, _ = composition.wiring_gaps(app)
    assert any(g.startswith(f"secret {name}") for g in gaps), gaps
    assert all(value.strip() == "" or value not in g for g in gaps)  # the value itself is never echoed


@pytest.mark.parametrize("value", [None, "", "  ", "changeme", "short-token"])
def test_commit_client_from_env_refuses_an_absent_empty_or_placeholder_token(monkeypatch: pytest.MonkeyPatch,
                                                                             value: str | None) -> None:
    if value is None:
        monkeypatch.delenv("CORE_TOKEN_COMMIT", raising=False)
    else:
        monkeypatch.setenv("CORE_TOKEN_COMMIT", value)
    with pytest.raises(ValueError, match="CORE_TOKEN_COMMIT") as exc:
        CoreCommitClient.from_env("http://core", http=httpx.AsyncClient())
    assert value is None or value.strip() == "" or value not in str(exc.value)


def test_wire_app_keeps_the_cause_so_the_startup_error_names_it(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CORE_TOKEN_COMMIT", "")
    monkeypatch.setattr(composition, "build_agent_runtime", lambda cfg, **kw: fake_rt())
    state = SimpleNamespace(checkpointer=object(), pg_pool=object(), redis=object(), http=object(), voice=None,
                            settings=settings(core_token_agent=STRONG_A, knowledge_token_agent=STRONG_K))
    app = SimpleNamespace(state=state)
    assert composition.wire_app(app, state.settings) is None  # type: ignore[arg-type]
    with pytest.raises(RuntimeError, match="ingress.*CORE_TOKEN_COMMIT"):
        composition.assert_wired(app)


def test_wiring_gaps_flags_a_turn_runner_without_flags_or_cost_of() -> None:
    app = wired_state()
    app.state.ingress.run_agent.flags = None
    gaps, _ = composition.wiring_gaps(app)
    assert any(g.startswith("session flags") for g in gaps), gaps
    app = wired_state()
    app.state.ingress.run_agent.cost_of = None
    gaps, _ = composition.wiring_gaps(app)
    assert any(g.startswith("cost_of") for g in gaps), gaps


PENDING = ["Gate 2 verifier: test", "PIN scanner: test"]


def test_assert_wired_fails_on_pending_safety_pieces_in_production(monkeypatch: pytest.MonkeyPatch) -> None:
    app = wired_state()
    app.state.settings = settings(site_address="demo.example.com")
    monkeypatch.setattr(composition, "wiring_gaps", lambda a: ([], PENDING))
    with pytest.raises(RuntimeError, match="Gate 2 verifier.*PIN scanner"):
        composition.assert_wired(app)


def test_assert_wired_only_warns_on_pending_safety_pieces_in_local_dev(monkeypatch: pytest.MonkeyPatch,
                                                                      caplog: pytest.LogCaptureFixture) -> None:
    app = wired_state()
    app.state.settings = settings(site_address=":80")
    monkeypatch.setattr(composition, "wiring_gaps", lambda a: ([], PENDING))
    composition.assert_wired(app)
    assert "wiring pending: Gate 2 verifier" in caplog.text and "wiring pending: PIN scanner" in caplog.text


@pytest.mark.parametrize("site,env,prod", [
    ("", "", False), (":80", "", False), (":3084", "", False), ("localhost", "", False),
    ("localhost:8080", "", False), ("127.0.0.1", "", False), ("http://localhost", "", False),
    ("demo.example.com", "", True), ("https://demo.example.com", "", True), ("203.0.113.7.sslip.io", "", True),
    (":80", "production", True), ("", "PRODUCTION", True), ("", "dev", False)])
def test_settings_production_is_a_public_site_address_or_agent_env_production(site: str, env: str,
                                                                              prod: bool) -> None:
    assert settings(site_address=site, agent_env=env).production is prod


def test_settings_read_site_address_and_agent_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SITE_ADDRESS", "demo.example.com")
    monkeypatch.setenv("AGENT_ENV", "production")
    s = Settings.from_env()
    assert (s.site_address, s.agent_env, s.production) == ("demo.example.com", "production", True)


# ---- the lifespan must call assert_wired (review PR #58 I3) ----

class _NoTrajectory:
    """What: a TrajectoryWriter stand-in with no database. Use when: lifespan tests. Inputs: any.
    Output: start and aclose. Side effects: none. Role: fake."""

    def __init__(self, *a: Any, **kw: Any) -> None:
        """What: ignore arguments. Use when: the lifespan. Inputs: any. Output: none. Side effects: none.
        Role: fake."""

    async def start(self) -> None:
        """What: nothing. Use when: the lifespan. Inputs: none. Output: none. Side effects: none. Role: fake."""

    async def aclose(self) -> None:
        """What: nothing. Use when: shutdown. Inputs: none. Output: none. Side effects: none. Role: fake."""


def _none_checkpointer() -> Any:
    """What: a checkpointer factory that yields None (as if the database never came up). Use when: lifespan tests.
    Inputs: none. Output: async context manager. Side effects: none. Role: fake."""
    from collections.abc import AsyncIterator
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def cm() -> AsyncIterator[None]:
        """What: yield None. Use when: the lifespan. Inputs: none. Output: None. Side effects: none. Role: fake."""
        yield None

    return cm()


async def test_app_lifespan_fails_startup_when_the_turn_path_is_not_wired(monkeypatch: pytest.MonkeyPatch) -> None:
    import agent.main as main_module
    monkeypatch.setattr(main_module, "TrajectoryWriter", _NoTrajectory)
    app = create_app(settings(agent_db_url="postgresql://nobody@127.0.0.1:1/none", redis_url="redis://127.0.0.1:1/0",
                              models_path="/nonexistent/models.yaml", core_token_agent=STRONG_A,
                              knowledge_token_agent=STRONG_K),
                     checkpointer_factory=_none_checkpointer)
    with pytest.raises(RuntimeError, match=r"turn path not wired: ingress: .*\(cause: no checkpointer"):
        async with app.router.lifespan_context(app):
            pass


async def test_app_lifespan_fails_startup_on_pending_safety_pieces_in_production(
        monkeypatch: pytest.MonkeyPatch) -> None:
    import agent.main as main_module
    monkeypatch.setattr(main_module, "TrajectoryWriter", _NoTrajectory)
    monkeypatch.setattr(main_module, "wire_app", lambda app, cfg: None)
    monkeypatch.setattr(composition, "wiring_gaps", lambda a: ([], PENDING))
    app = create_app(settings(agent_db_url="postgresql://nobody@127.0.0.1:1/none", redis_url="redis://127.0.0.1:1/0",
                              models_path="/nonexistent/models.yaml", site_address="demo.example.com"),
                     checkpointer_factory=_none_checkpointer)
    with pytest.raises(RuntimeError, match="production.*Gate 2 verifier"):
        async with app.router.lifespan_context(app):
            pass


def test_wiring_gaps_flags_a_body_without_structured_input_or_without_last_turn() -> None:
    app = wired_state()
    app.state.ingress.run_agent.structured.last_turn = None
    gaps, _ = composition.wiring_gaps(app)
    assert any(g.startswith("last_turn") for g in gaps), gaps
    app.state.ingress.run_agent = object()
    gaps, _ = composition.wiring_gaps(app)
    assert any(g.startswith("turn body") for g in gaps), gaps
