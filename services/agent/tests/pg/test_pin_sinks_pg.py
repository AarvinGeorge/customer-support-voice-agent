"""What: the doc 07 s11.1 sink test (CS-E-03.S7.T9). "QS-10423 4821" and its spoken form go through the chat and voice
paths of the real app (Ingress, create_agent graph, AsyncPostgresSaver, PgTurnStore, PgVoiceStore) on the Compose
Postgres; then the checkpoint tables, agent.turns, agent.turn_events, agent.transcript_items, agent.sessions, the
outbound core and knowledge requests (ticket requests among them), the scope classifier and grounding verifier inputs,
the application logs and the LangSmith payloads are searched for the raw PIN. A failing-gate phase (Gate 2 fails with a
commitment, Gate 1 none with the classifier ON and OFF) runs the ticket and gate input paths (PR #57 follow-up, m4).
Use when: scripts/pg_tests.sh (CI job "pg tests") with AGENT_PG_TEST_URL pointing at agent_app.
Inputs: env AGENT_PG_TEST_URL. Output: pass or fail; a missing variable FAILS.
Side effects: one session (plus turns, items, checkpoint rows), deleted afterwards; LangSmith requests are captured
in memory (no network); no provider is called (an echo fake model).
Role: proves the raw secret is in the fixture and each sink holds the message text (positive control: the kept
account id "QS-10423"), then that no sink holds the PIN. Core audit rows: services/core/tests/pg/
test_audit_idempotency_pg.py::test_free_text_pin_in_audit_args_is_absent_from_the_stored_row (same CI job).
agent.turn_events is written by the real TrajectoryWriter (the TurnStream sink, as in production)."""
from __future__ import annotations

import json
import logging
import os
import re
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, AsyncIterator

import httpx
import pytest
import requests
from contracts.events import KEEPALIVE, parse_event
from contracts.stubs import examples as ex
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langsmith import run_trees

from agent import composition
from agent.access import sign_cookie
from agent.assembly import postgres_checkpointer_factory
from agent.ingress import PgTurnStore
from agent.logs import LOG_FORMAT, RedactingFormatter
from agent.main import create_app
from agent.pin_guard import TYPED_PIN_LINE
from agent.realtime import SessionSummary
from agent.sessions import PgSessionStore
from agent.settings import Settings
from agent.tracing import build_langsmith_client
from agent.trajectory import TrajectoryWriter
from agent.structured.confirm import CoreCommitClient
from agent.turn_body import PgSessionFlags
from agent.voice import PgVoiceStore, TranscriptMerger, VoiceDeps, VoiceSessionRow, load_relay_facts

pytestmark = pytest.mark.pg

RAW = "QS-10423 4821"
SPOKEN = "Q S one zero four two three, four eight two one"
KEPT = "QS-10423"
VISITOR = "pg-sink-visitor"
KEY = "k" * 32
ORIGIN = "https://test"
# The raw PIN as a standalone group: not inside a uuid group, a hex digest or a timestamp (a log asctime such as
# "10:55:34,821" must not count as "4,821", seen once as a flake).
# Hex-led or hex-trailed digits are uuid groups or timestamps; every spoken, grouped and Unicode form is listed.
SECRET = re.compile(r"(?<![0-9a-f-])4821(?![0-9a-f])|104234821|10423-4821|four[ ,.-]+eight[ ,.-]+two[ ,.-]+(?:one|oh)"
                    r"|forty[ -]?eight|twenty[ -]?one|48 21|4[ -]8[ -]2[ -]1|\uff14\uff18\uff12\uff11"
                    r"|\u0664\u0668\u0662\u0661|(?<!\d)4,821|(?<!\d)4 821|4\.8\.2\.1|(?<!\d)48\s+21|4/8/2/1|(?<![0-9a-f-])4481(?![0-9a-f])"
                    r"|double four|four thousand|(?<![0-9a-f-])0482(?![0-9a-f])|nought four", re.IGNORECASE)


def secret_hits(text: str) -> list[str]:
    """What: the raw PIN forms in a text, after dropping format characters and combining marks (so "48<ZWSP>21"
    and keycap digits are seen). Use when: the leak search and its fixture check. Inputs: text. Output: matches.
    Side effects: none. Role: helper."""
    import unicodedata
    clean = "".join(c for c in text if unicodedata.category(c) not in ("Cf", "Mn", "Me"))
    return SECRET.findall(clean)
# The review PR #57 bypass list (same strings as packages/contracts/tests/test_pin_bypasses.py BYPASSES).
BYPASSES = [
    "QS-10423,4821", "QS-10423.4821", "my PIN is four eight two one. One more thing",
    "oh, four eight two one is my pin", "I have 2, four eight two one is my PIN", "forty-eight twenty-one is my pin",
    "my pin is 48 21", "my PIN is \uff14\uff18\uff12\uff11", "my PIN is \u0664\u0668\u0662\u0661", "pin4821",
    "QS - 10423 4821", "my passcode is 4821", "the code is 4821",
    "QS\u201310423 4821", "QS_10423 4821", "QS#10423 4821", "pincode 4821", "my P.I.N. is 4821",
    "my PIN, which I set up last year when I first opened the account, is 4821", "my pin is $4821",
    "PIN: 4821", "PIN:4821", "_4821_ is my pin",
    "QS-10423/4821", "QS-10423:4821", "QS-10423-4821", "QS-104234821", "QS-10423   4821", "QS-10423\t4821",
    "4821,QS-10423", "4821.QS-10423", "4821QS-10423", "4821 QS-10423", "4821 / QS-10423", "qs10423,4821",
    "QS-10423 forty-eight twenty-one", "QS-10423 forty eight, twenty one", "QS-10423 48 21", "QS-10423 4-8-2-1",
    "my pin is 4-8-2-1", "my pin is forty eight twenty one", "Customer QS-10423: pin is four, eight, two, one.",
    "my PIN is four eight two one one", "my pin is four eight two oh", "code: 4 8 2 1",
    "Q S one zero four two three four eight two one", "my PIN is 4821 and my account is QS-10423",
    "pin number four-eight-two-one", "my passcode: forty eight twenty one",
]
# Fix round 2 (rereview-pr57-r1): the 14 realistic forms (two are split over two messages) and the cheap crafted
# forms, each with the session state it was reproduced in (True: awaiting verification).
ROUND2 = [
    ("my pin is 4,821", False), ("my pin is 4 821", False), ("my pin is 4.8.2.1", False), ("my pin is 48\n21", False),
    ("my pin is double four eight one", False), ("four thousand eight hundred twenty-one", True),
    ("four thousand eight hundred and twenty-one", True), ("four, eight. two, one.", True),
    ("my pin is four. eight. two. one.", False), ("yeah it's four eight two one", False),
    ("my password is 4821", False), ("my secret number is 4821", False),
    ("my pin is 48\u200b21", False), ("my pin is 4\u200b8\u200b2\u200b1", False),
    ("my pin is 4\u03018\u03012\u03011\u0301", False), ("my pin is 4\ufe0f\u20e38\ufe0f\u20e32\ufe0f\u20e31\ufe0f\u20e3", False),
    ("my p\u200bin is 4821", False), ("my \u0440in is 4821", False), ("my pin is 4/8/2/1", False),
    ("my pin is nought four eight two", False),
]
SPLIT = [("my PIN is", "4821 by the way, which plan am I on?"), ("I am QS-10423", "and the number is 4821 if you need it")]
SPOKEN_BYPASSES = ["my PIN is four eight two one. One more thing", "oh, four eight two one is my pin",
                   "I have 2, four eight two one is my PIN"]


class Echo(GenericFakeChatModel):
    """What: a fake chat model that repeats what it was told: first a search_kb call whose query is the last human
    message, then an answer quoting it. Use when: this test. Inputs: none. Output: AI messages.
    Side effects: records every call in `seen`. Role: the worst-case model (it copies customer text into tool args,
    the answer and the thread)."""

    seen: list[Any] = []

    def bind_tools(self, tools: Any, **kw: Any) -> "Echo":
        """What: no-op. Use when: create_agent binds tools. Inputs: tools. Output: self. Side effects: none.
        Role: fake plumbing."""
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kw):  # type: ignore[override]
        """What: tool call on the first step of a turn, then the echo answer. Use when: model call.
        Inputs: messages. Output: ChatResult. Side effects: records. Role: fake."""
        self.seen.append(list(messages))
        human = [m for m in messages if isinstance(m, HumanMessage)][-1]
        said = str(human.content)[-300:]
        first = not messages or not getattr(messages[-1], "tool_call_id", None)
        if first and "check my account" in said:  # I3: the model asks for verification
            msg = AIMessage(content="", tool_calls=[{"name": "request_verification",
                                                     "args": {"reason": "account_lookup"},
                                                     "id": f"call_{uuid.uuid4().hex[:8]}", "type": "tool_call"}])
        elif first:
            msg = AIMessage(content="", tool_calls=[{"name": "search_kb", "args": {"query": said},
                                                     "id": f"call_{uuid.uuid4().hex[:8]}", "type": "tool_call"}])
        else:
            msg = AIMessage(content=f"You said: {said}")
        return ChatResult(generations=[ChatGeneration(message=msg)])

    async def _agenerate(self, messages, stop=None, run_manager=None, **kw):  # type: ignore[override]
        """What: async twin. Use when: model call. Inputs: messages. Output: ChatResult. Side effects: records.
        Role: fake."""
        return self._generate(messages, stop=stop, **kw)


class Lock:
    """What: in-memory turn lock (Redis is not part of this test). Use when: Ingress here. Inputs: none.
    Output: acquire, release. Side effects: its dict. Role: fake of RedisTurnLock."""

    def __init__(self) -> None:
        """What: empty. Use when: setup. Inputs: none. Output: none. Side effects: none. Role: constructor."""
        self.held: dict[str, str] = {}

    async def acquire(self, session_id: str, token: str, ttl_ms: int) -> bool:
        """What: take if free. Use when: step 7. Inputs: ids, ttl. Output: bool. Side effects: dict. Role: fake."""
        return self.held.setdefault(session_id, token) == token

    async def release(self, session_id: str, token: str) -> bool:
        """What: compare and delete. Use when: step 16. Inputs: ids. Output: bool. Side effects: dict. Role: fake."""
        return self.held.pop(session_id, None) is not None


class Keys:
    """What: in-memory voice call keys. Use when: Ingress here. Inputs: none. Output: claim. Side effects: set.
    Role: fake of RedisCallKeys."""

    def __init__(self) -> None:
        """What: empty. Use when: setup. Inputs: none. Output: none. Side effects: none. Role: constructor."""
        self.keys: set[str] = set()

    async def claim(self, session_id: str, call_id: str) -> bool:
        """What: claim once. Use when: step 7. Inputs: ids. Output: True when new. Side effects: set. Role: fake."""
        key = f"{session_id}:{call_id}"
        new = key not in self.keys
        self.keys.add(key)
        return new


class NoMinter:
    """What: a minter that is never used (no voice/token here). Use when: VoiceDeps. Inputs: none.
    Output: mint raises. Side effects: none. Role: fake."""

    async def mint(self, session: dict[str, Any]) -> Any:
        """What: refuse. Use when: never. Inputs: session. Output: none. Side effects: raises. Role: fake."""
        raise AssertionError("no mint in the sink test")


class Fixed(GenericFakeChatModel):
    """What: a fake gate model that records its inputs and replies per the shared mode: the scope classifier "ON"
    (or "OFF" when mode["scope"] is "OFF"); the grounding verifier zero claims with no_claims_reason "conversation" (passes; D-069: every empty
    list needs a reason), or with
    mode["verifier"] == "fail" one unsupported claim quoting its input plus a commitment (escalate with a ticket).
    Use when: the two gates of this test. Inputs: role, shared mode dict. Output: AI message.
    Side effects: records every call in `seen`. Role: gate inputs become a searched sink, and the failing paths run."""

    role: str = ""
    mode: dict[str, str] = {}
    seen: list[Any] = []

    def _generate(self, messages, stop=None, run_manager=None, **kw):  # type: ignore[override]
        """What: the reply for the current mode. Use when: model call. Inputs: messages. Output: ChatResult.
        Side effects: records. Role: fake."""
        self.seen.append(list(messages))
        if self.role == "scope_classifier":
            reply = self.mode.get("scope", "ON")
        elif self.mode.get("verifier") == "fail":
            quoted = " ".join(str(m.content) for m in messages)[-300:]
            reply = json.dumps({"claims": [{"text": quoted, "supported": False, "source_id": "none"}],
                                "commitment": True})
        else:
            reply = '{"claims": [], "commitment": false, "no_claims_reason": "conversation"}'
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content=reply))])

    async def _agenerate(self, messages, stop=None, run_manager=None, **kw):  # type: ignore[override]
        """What: async twin. Use when: model call. Inputs: messages. Output: ChatResult. Side effects: none.
        Role: fake."""
        return self._generate(messages)


class Registry:
    """What: build(role) returns the echo model. Use when: build_agent_runtime. Inputs: none. Output: build.
    Side effects: none. Role: fake ModelRegistry."""

    def __init__(self, mode: dict[str, str]) -> None:
        """What: one echo model and the two recording gate fakes. Use when: setup. Inputs: shared mode dict.
        Output: none. Side effects: none. Role: fake."""
        self.model = Echo(messages=iter([]), disable_streaming=True, seen=[])
        self.gates = {r: Fixed(messages=iter([]), role=r, mode=mode, seen=[])
                      for r in ("scope_classifier", "grounding_verifier")}

    def build(self, role: str) -> Any:
        """What: the echo for the agent roles, the recording fakes for the two gates. Use when: assembly.
        Inputs: role. Output: model. Side effects: none. Role: fake."""
        return self.gates.get(role, self.model)

    def cost_for_model(self, model: str, usage: Any) -> float:
        """What: zero cost. Use when: TurnRunner usage. Inputs: model, usage. Output: 0.0. Side effects: none.
        Role: fake."""
        return 0.0


class Capture(requests.Session):
    """What: a requests session that records LangSmith request bodies and answers 200. Use when: the hiding client.
    Inputs: none. Output: session. Side effects: appends to `bodies`. Role: fake LangSmith endpoint."""

    def __init__(self) -> None:
        """What: empty record. Use when: setup. Inputs: none. Output: none. Side effects: none. Role: constructor."""
        super().__init__()
        self.bodies: list[bytes] = []

    def request(self, method: Any, url: Any, *a: Any, **kw: Any) -> requests.Response:  # type: ignore[override]
        """What: record the body. Use when: the client posts. Inputs: request. Output: 200. Side effects: records.
        Role: fake."""
        body = kw.get("data")
        self.bodies.append(body if isinstance(body, bytes) else str(body).encode())
        resp = requests.Response()
        resp.status_code, resp._content = 200, b"{}"
        return resp


def text_of(value: Any) -> str:
    """What: one value as searchable text (bytes decoded byte for byte, so msgpack blobs are searched too).
    Use when: row search. Inputs: value. Output: text. Side effects: none. Role: helper."""
    if isinstance(value, (bytes, bytearray, memoryview)):
        return bytes(value).decode("latin-1")
    if isinstance(value, (dict, list)):
        return json.dumps(value, default=str)
    return str(value)


def search_body(verdict: str = "pass") -> dict[str, Any]:
    """What: a contract SearchResponse with no relevant chunk (verdict pass: the echo answers anyway; verdict none:
    Gate 1 runs the scope classifier). Use when: the fake knowledge service. Inputs: verdict. Output: dict.
    Side effects: none. Role: fixture."""
    return {"request_id": "r", "trace_id": "t", "corpus_version": "v1", "embedding_model": "m",
            "query": {"raw": "q", "normalized": "q", "codes": [], "unknown_codes": []}, "allowed_tiers": ["all"],
            "chunks": [],
            "confidence": {"verdict": verdict, "reason": "above_threshold" if verdict == "pass" else "no_match", "top_cosine": 0.7, "tau_low": 0.3,
                           "tau_high": 0.45, "support_count": 1, "code_match": False,
                           "legs": {"vector": "ok", "keyword": "ok"}, "calibration_id": None,
                           "calibration_status": "provisional", "calibration_stale": False},
            "timing_ms": {"embed": 1, "sql": 1, "total": 2}}


def events_of(resp: httpx.Response) -> list[Any]:
    """What: parse an SSE body with the contracts models. Use when: every turn. Inputs: response. Output: events.
    Side effects: none. Role: validator."""
    assert resp.headers["content-type"].startswith("text/event-stream"), resp.text
    out = []
    for frame in resp.text.split("\n\n"):
        if frame and frame + "\n\n" != KEEPALIVE:
            out.append(parse_event(frame.split("\n")[2][len("data: "):]))
    return out


@pytest.fixture
async def pool() -> AsyncIterator[Any]:
    """What: an agent_app pool. Use when: this test. Inputs: env. Output: pool. Side effects: opens and closes it.
    Role: fixture (a missing URL fails: a skipped DB test tests nothing)."""
    from psycopg_pool import AsyncConnectionPool

    url = os.environ.get("AGENT_PG_TEST_URL", "")
    if not url:
        pytest.fail("AGENT_PG_TEST_URL is not set; run scripts/pg_tests.sh (Compose Postgres, migrate_agent)")
    p = AsyncConnectionPool(url, min_size=1, max_size=4, open=False)
    await p.open()
    try:
        yield p
    finally:
        await p.close()


async def rows_text(pool: Any, sid: str) -> dict[str, str]:
    """What: every row of every agent sink table for this session, as text per table.
    Use when: the search. Inputs: pool, session id. Output: {table: text}. Side effects: SELECTs.
    Role: the sinks the agent owns in Postgres (doc 07 s12)."""
    queries = {
        "checkpoints": "SELECT * FROM agent.checkpoints WHERE thread_id = %s",
        "checkpoint_blobs": "SELECT * FROM agent.checkpoint_blobs WHERE thread_id = %s",
        "checkpoint_writes": "SELECT * FROM agent.checkpoint_writes WHERE thread_id = %s",
        "turns": "SELECT * FROM agent.turns WHERE session_id = %s::uuid",
        "turn_events": "SELECT * FROM agent.turn_events WHERE session_id = %s::uuid",
        "transcript_items": "SELECT * FROM agent.transcript_items WHERE session_id = %s::uuid",
        "sessions": "SELECT * FROM agent.sessions WHERE session_id = %s::uuid",
    }
    out = {}
    async with pool.connection() as conn:
        for table, sql in queries.items():
            cur = await conn.execute(sql, (sid,))
            out[table] = "\n".join(" | ".join(text_of(v) for v in r) for r in await cur.fetchall())
    return out


async def new_session(pool: Any, sids: list[str]) -> tuple[str, str]:
    """What: insert a session and a live voice session. Use when: each phase. Inputs: pool, the id list (appended).
    Output: (session id, voice session id). Side effects: INSERTs. Role: helper (a session holds 40 turns at most)."""
    sid = str(uuid.uuid4())
    async with pool.connection() as conn:
        await conn.execute("INSERT INTO agent.sessions (session_id, visitor_id) VALUES (%s, %s)", (sid, VISITOR))
    now = datetime.now(UTC)
    await PgVoiceStore(pool).create_voice_session(VoiceSessionRow(f"vs-{sid}", sid, now, now + timedelta(seconds=600)))
    sids.append(sid)
    return sid, f"vs-{sid}"


async def awaiting(pool: Any, sid: str) -> bool:
    """What: agent.sessions.awaiting_verification. Use when: asserting. Inputs: pool, id. Output: bool.
    Side effects: SELECT. Role: helper."""
    async with pool.connection() as conn:
        cur = await conn.execute("SELECT awaiting_verification FROM agent.sessions WHERE session_id = %s", (sid,))
        row = await cur.fetchone()
    return bool(row and row[0])


async def test_raw_pin_reaches_no_sink(pool: Any, caplog: pytest.LogCaptureFixture) -> None:
    sids: list[str] = []
    outbound: list[bytes] = []
    tickets: list[bytes] = []
    gate_cfg: dict[str, str] = {}

    def services(request: httpx.Request) -> httpx.Response:
        """What: fake core and knowledge. Use when: tool calls, tickets and memory refresh. Inputs: request.
        Output: response (a ticket for POST /v1/tickets, else a search). Side effects: records the body.
        Role: the outbound HTTP sink."""
        outbound.append(str(request.url).encode() + b" " + request.content)
        if request.url.path == "/v1/tickets":
            tickets.append(request.content)
            ref = ex.TICKET_REF.model_dump(mode="json")
            return httpx.Response(200, json={"ticket": ref, "merged": False, "created": True})
        return httpx.Response(200, json=search_body(gate_cfg.get("search", "pass")))

    capture = Capture()
    tracer_client = build_langsmith_client(api_key="lsv2_test_not_real", api_url="http://langsmith.test",
                                           session=capture, auto_batch_tracing=False)
    previous_client = run_trees._CLIENT  # noqa: SLF001 - restored in finally
    run_trees.configure(client=tracer_client, enabled=True)
    caplog.set_level(logging.DEBUG)
    cfg = Settings(served_by="agent_pg", agent_db_url="postgresql://x", redis_url="redis://x",
                   core_url="http://core.test:8000", knowledge_url="http://knowledge.test:8000",
                   demo_faults_enabled=False, site_origin=ORIGIN, session_signing_key=KEY, demo_access_code="c",
                   core_token_agent="core-agent-tok", knowledge_token_agent="knowledge-agent-tok")
    registry = Registry(gate_cfg)
    bodies: list[Any] = []
    refused: list[str] = []
    try:
        trajectory = TrajectoryWriter(pool=pool)
        async with postgres_checkpointer_factory(os.environ["AGENT_PG_TEST_URL"])() as saver:
            async with httpx.AsyncClient(transport=httpx.MockTransport(services)) as http:
                runtime = composition.build_agent_runtime(cfg, http=http, checkpointer=saver, registry=registry)
                ingress = composition.build_ingress(  # the production wiring: TurnRunner, StructuredInput (PR #58)
                    runtime, store=PgTurnStore(pool), lock=Lock(), served_by="agent_pg", call_keys=Keys(),
                    merge_transcript=TranscriptMerger(PgVoiceStore(pool)), flags=PgSessionFlags(pool),
                    commit=CoreCommitClient(cfg.core_url, "commit-tok", http=http), trajectory=trajectory)

                async def summary(_: str, ctx: Any = None) -> SessionSummary:
                    """What: empty summary. Use when: never (no token mint here). Inputs: sid, ctx.
                    Output: summary. Side effects: none. Role: fake."""
                    return SessionSummary()

                async def relay_facts(sid: str) -> Any:
                    """What: the production RelayFacts loader over Postgres and this saver. Use when: POST
                    /transcript. Inputs: sid. Output: RelayFacts. Side effects: reads. Role: wiring (D-071)."""
                    return await load_relay_facts(PgTurnStore(pool), saver, sid)

                voice = VoiceDeps(store=PgVoiceStore(pool), minter=NoMinter(), voice_spec=None, summary=summary,
                                  ingress=ingress, relay_facts=relay_facts)

                async def ok() -> str:
                    """What: healthy probe. Use when: readyz. Inputs: none. Output: ok. Side effects: none.
                    Role: fake."""
                    return "ok"

                app = create_app(cfg, probes={k: ok for k in ("redis", "postgres", "core", "knowledge")},
                                 session_store=PgSessionStore(pool), voice=voice, ingress=ingress)
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=ORIGIN) as c:
                    c.cookies.set("qs_access", sign_cookie(KEY, visitor_id=VISITOR))

                    async def post(sid: str, path: str, body: dict[str, Any]) -> httpx.Response:
                        """What: one request, its body kept for the fixture check. Use when: below.
                        Inputs: session, path, body. Output: response. Side effects: a request. Role: helper."""
                        bodies.append(body)
                        return await c.post(f"/api/sessions/{sid}{path}", json=body)

                    async def chat(sid: str, text: str) -> list[Any]:
                        """What: one chat turn. Use when: below. Inputs: session, text. Output: events.
                        Side effects: a turn. Role: helper."""
                        r = await post(sid, "/turns", {"client_turn_id": str(uuid.uuid4()), "text": text})
                        assert r.status_code == 200, r.text
                        return events_of(r)

                    def voice_body(vs_id: str, question: str, said: str, item: str) -> dict[str, Any]:
                        """What: a voice/ask body. Use when: below. Inputs: ids, texts. Output: body.
                        Side effects: none. Role: helper."""
                        return {"client_turn_id": str(uuid.uuid4()), "voice_session_id": vs_id,
                                "realtime_call_id": f"call_{uuid.uuid4().hex[:8]}", "question": question,
                                "user_item_id": item, "user_transcript": said, "transcript_final": True,
                                "last_assistant_interrupted": False}

                    # 1. Chat, first turn: refused with the fixed line (the Done-when of T9).
                    sid, vs_id = await new_session(pool, sids)
                    e1 = await chat(sid, RAW)
                    assert e1[-1].final.text == TYPED_PIN_LINE and e1[-1].end_reason == "verification_requested"
                    assert [e.reason for e in e1 if e.type == "verification.required"] == ["typed_pin_rejected"]
                    assert registry.model.seen == []  # no model call
                    # 2. Voice: the PIN in the question and spoken in the final transcript; goes to the model.
                    e2 = events_of(await post(sid, "/voice/ask", voice_body(vs_id, f"Customer says {RAW}", SPOKEN,
                                                                             "item_pg1")))
                    assert e2[-1].type == "turn.completed" and e2[-1].final.text.startswith("You said:")
                    # 3. Voice chatter posted outside ask_support_agent, then merged by the next chat turn.
                    t = await post(sid, "/transcript", {"voice_session_id": vs_id, "items": [
                        {"realtime_item_id": "item_pg2", "role": "user", "text": f"again {SPOKEN}",
                         "interrupted": False, "client_ts": "2026-11-28T13:00:00Z"},
                        {"realtime_item_id": "item_pg3", "role": "user", "text": f"my PIN is 4821 and I am {KEPT}",
                         "interrupted": False, "client_ts": "2026-11-28T13:00:01Z"}]})
                    assert t.status_code == 204, t.text
                    assert (await chat(sid, "thanks, any update?"))[-1].type == "turn.completed"

                    # 4. I3: the model asks for verification through the wired TurnRunner; the row flag is set, so a
                    # PIN with no context word is redacted and a bare PIN is refused.
                    sid, _ = await new_session(pool, sids)
                    ev = await chat(sid, "please check my account balance")
                    assert ev[-1].end_reason == "verification_requested", ev[-1]
                    assert [e.reason for e in ev if e.type == "verification.required"] == ["account_lookup"]
                    assert await awaiting(pool, sid), "request_verification did not set agent.sessions.awaiting"
                    ev = await chat(sid, "it is 4 8 2 1 I think")  # redacted, not refused (no credential word)
                    assert ev[-1].type == "turn.completed" and ev[-1].final.text != TYPED_PIN_LINE
                    ev = await chat(sid, "4821")
                    assert ev[-1].final.text == TYPED_PIN_LINE

                    # 5. Every review bypass as the first turn of a fresh session (unverified, not awaiting): each
                    # one is refused.
                    for text in BYPASSES:
                        sid, vs_id = await new_session(pool, sids)
                        ev = await chat(sid, text)
                        if ev[-1].final.text == TYPED_PIN_LINE:
                            refused.append(text)
                    # 6. Every bypass as posted voice chatter, then merged; the spoken ones also as voice/ask.
                    # The session holds an issued ticket (rereview-pr76-r1 N3), so the relay exemption is live.
                    sid, vs_id = await new_session(pool, sids)
                    ref = ex.TICKET_REF.model_dump(mode="json")
                    async with pool.connection() as conn:
                        await conn.execute(
                            "INSERT INTO agent.turns (turn_id, session_id, client_turn_id, mode, input_kind, status, "
                            "deadline_at, started_at, final) VALUES (%s, %s, %s, 'voice', 'voice', 'completed', "
                            "now() + interval '45 seconds', now() - interval '1 minute', %s)",
                            (str(uuid.uuid4()), sid, str(uuid.uuid4()),
                             json.dumps({"text": f"I've opened {ref['display']} for our team.", "say": "s",
                                         "ticket": ref})))
                    facts = await relay_facts(sid)
                    assert facts.ids == {ref["short_id"]}, facts  # positive control: a non-empty issued id set
                    # D-071 / review-pr76 M3: every bypass also as an assistant item (the realtime model echoing it,
                    # or a browser that labels customer speech "assistant"); provenance is decided server-side.
                    items = [{"realtime_item_id": f"item_{role[0]}{n}", "role": role, "text": text,
                              "interrupted": False, "client_ts": "2026-11-28T13:00:00Z"}
                             for role in ("user", "assistant") for n, text in enumerate(BYPASSES)][:50]
                    items2 = [{"realtime_item_id": f"item_{role[0]}{n}", "role": role, "text": text,
                               "interrupted": False, "client_ts": "2026-11-28T13:00:00Z"}
                              for role in ("user", "assistant") for n, text in enumerate(BYPASSES)][50:]
                    if items2:
                        assert (await post(sid, "/transcript", {"voice_session_id": vs_id,
                                                                "items": items2})).status_code == 204
                    assert (await post(sid, "/transcript", {"voice_session_id": vs_id, "items": items})).status_code == 204
                    assert (await chat(sid, "thanks, any update?"))[-1].type == "turn.completed"
                    for n, text in enumerate(SPOKEN_BYPASSES):
                        r = await post(sid, "/voice/ask", voice_body(vs_id, "customer gave a number", text, f"item_s{n}"))
                        assert events_of(r)[-1].type == "turn.completed"
                    # 7. Fix round 2: each realistic and cheap crafted form in chat and in a voice transcript, in a
                    # fresh session in the state it was reproduced in; the split forms as two chat turns.
                    for n, (text, wait) in enumerate(ROUND2):
                        for mode in ("chat", "voice"):
                            sid, vs_id = await new_session(pool, sids)
                            if wait:
                                async with pool.connection() as conn:
                                    await conn.execute("UPDATE agent.sessions SET awaiting_verification = true "
                                                       "WHERE session_id = %s", (sid,))
                            if mode == "chat":
                                assert (await chat(sid, text))[-1].type == "turn.completed"
                            else:
                                r = await post(sid, "/voice/ask", voice_body(vs_id, "customer gave a number", text,
                                                                             f"item_r{n}"))
                                assert events_of(r)[-1].type == "turn.completed"
                    for first, second in SPLIT:
                        sid, _ = await new_session(pool, sids)
                        await chat(sid, first)
                        assert await awaiting(pool, sid), f"no awaiting flag after {first!r}"
                        assert (await chat(sid, second))[-1].type == "turn.completed"
                    # 8. Failing gates (re-review r2 m4): Gate 2 fails with a commitment (claims event, ticket with
                    # the customer's last words); Gate 1 verdict none with the classifier ON (escalate, ticket) and
                    # OFF (decline). Each with a voice PIN, a chat PIN while awaiting and a split chat PIN.
                    for gate_mode in ({"verifier": "fail"}, {"search": "none", "scope": "ON"},
                                      {"search": "none", "scope": "OFF"}):
                        gate_cfg.clear()
                        gate_cfg.update(gate_mode)
                        sid, vs_id = await new_session(pool, sids)
                        r = await post(sid, "/voice/ask", voice_body(vs_id, f"Customer says {RAW}",
                                                                     f"{SPOKEN} {KEPT}", f"item_g{len(sids)}"))
                        assert events_of(r)[-1].type == "turn.completed"
                        sid, _ = await new_session(pool, sids)
                        async with pool.connection() as conn:
                            await conn.execute("UPDATE agent.sessions SET awaiting_verification = true "
                                               "WHERE session_id = %s", (sid,))
                        assert (await chat(sid, f"it is 4 8 2 1 I think, account {KEPT}"))[-1].type == "turn.completed"
                        sid, _ = await new_session(pool, sids)
                        await chat(sid, "my PIN is")
                        assert (await chat(sid, f"4821 by the way, {KEPT} was charged twice"))[-1].type == \
                            "turn.completed"
                    gate_cfg.clear()
        tracer_client.flush()
        await trajectory.flush()
        assert trajectory.pending() == 0

        assert sorted(set(BYPASSES) - set(refused)) == [], "bypasses not refused"
        # Proof that a zero is possible: every secret form is in the fixture, and the search finds it in raw form.
        fixture = json.dumps(bodies, ensure_ascii=False)
        assert SECRET.search(fixture) and SECRET.search(RAW) and SECRET.search(SPOKEN)
        missed = [b for b in BYPASSES + [t for t, _ in ROUND2] + [b for _, b in SPLIT] if not secret_hits(b)]
        assert missed == [], f"the leak search cannot see these raw forms: {missed}"
        from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
        _, raw_blob = JsonPlusSerializer().dumps_typed([HumanMessage(RAW)])
        assert SECRET.search(text_of(raw_blob))  # the byte search works on the checkpoint encoding

        per_session = [await rows_text(pool, sid) for sid in sids]
        sinks = {table: "\n".join(rows[table] for rows in per_session) for table in per_session[0]}
        sinks["outbound core and knowledge requests"] = text_of(b"\n".join(outbound))
        sinks["model inputs"] = repr(registry.model.seen)
        sinks["scope classifier inputs"] = repr(registry.gates["scope_classifier"].seen)
        sinks["grounding verifier inputs"] = repr(registry.gates["grounding_verifier"].seen)
        sinks["ticket requests"] = text_of(b"\n".join(tickets))
        sinks["langsmith payloads"] = b"\n".join(capture.bodies).decode("utf-8", "replace")
        formatter = RedactingFormatter(LOG_FORMAT)
        sinks["application logs (formatted)"] = "\n".join(formatter.format(r) for r in caplog.records)
        sinks["application logs (raw messages)"] = "\n".join(r.getMessage() for r in caplog.records)

        # Positive control: every sink that holds message text holds the kept account id, so the search below
        # looks at real content (the logs hold no message text by design, doc 07 s11 "never in logs").
        for name in ("checkpoint_blobs", "checkpoint_writes", "turns", "turn_events", "transcript_items",
                     "outbound core and knowledge requests", "model inputs", "langsmith payloads",
                     "scope classifier inputs", "grounding verifier inputs", "ticket requests"):
            assert KEPT in sinks[name], f"{name} holds no message text, so its search would prove nothing"
        assert caplog.records, "no log records were captured"

        # checkpoint blobs are msgpack: decode them as UTF-8 too, so Unicode digits are searched in both forms.
        sinks["checkpoint_blobs (utf-8)"] = sinks["checkpoint_blobs"].encode("latin-1").decode("utf-8", "replace")
        sinks["checkpoint_writes (utf-8)"] = sinks["checkpoint_writes"].encode("latin-1").decode("utf-8", "replace")
        leaks = {name: [line[:300] for line in text.splitlines() if secret_hits(line)][:3]
                 for name, text in sinks.items() if secret_hits(text)}
        assert leaks == {}, f"raw PIN found in: {leaks}"
    finally:
        run_trees.configure(client=previous_client, enabled=None)
        async with pool.connection() as conn:
            for sid in sids:
                for table in ("checkpoint_writes", "checkpoint_blobs", "checkpoints"):
                    await conn.execute(f"DELETE FROM agent.{table} WHERE thread_id = %s", (sid,))
                await conn.execute("DELETE FROM agent.sessions WHERE session_id = %s", (sid,))
