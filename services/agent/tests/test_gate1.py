"""What: tests for Gate 1 routing on the knowledge verdict (CS-E-02.S4.T1; doc 07 s6.2 `gate`, s6.5, s8.1 rows
"Scope classifier" and "Weak retrieval").
Use when: pytest (fake chat models, the contracts core stub and a fake knowledge service; no provider, no network).
Inputs: none. Output: pass/fail. Side effects: none.
Role: Done when: one test per verdict row (pass, weak and uncalibrated, none with the classifier, error); a
retrieval error never says "not in our docs"."""
from __future__ import annotations

import asyncio
from typing import Any

import httpx
import pytest
from contracts.stubs import examples as ex
from contracts.stubs.core_stub import create_app
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.checkpoint.memory import InMemorySaver

from agent.assembly import build_agent, run_config
from agent.clients.core_agent import CoreAgentClient
from agent.clients.knowledge import KnowledgeClient
from agent.gates import ScopeClassifier, retrieval_decision
from agent.sse import TurnMeta, TurnOutcome, TurnStream, start_turn, stream_agent_turn
from agent.tools import build_tools
from agent.turn_control import TurnControl

SID, TID = ex.SESSION_ID, "019a7c2e-5b20-7000-8000-0000000000c1"
OWNER = {"verified": True, "claims_snapshot": {"role": "owner", "tier": "pro"}}
NOT_IN_DOCS = ("don't cover", "not in our docs", "not covered")


class Fake(GenericFakeChatModel):
    """What: scripted fake model that records its inputs. Use when: tests. Inputs: messages. Output: replies.
    Side effects: appends inputs to `seen`. Role: test double."""

    seen: Any = None

    def bind_tools(self, tools: Any, **kw: Any) -> "Fake":
        """What: no-op. Use when: create_agent binds tools. Inputs: tools. Output: self. Side effects: none.
        Role: fake plumbing."""
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):  # type: ignore[override]
        """What: record, then reply. Use when: the framework calls the model. Inputs: messages. Output: result.
        Side effects: appends. Role: fake plumbing."""
        if self.seen is not None:
            self.seen.append(list(messages))
        return super()._generate(messages, stop=stop, run_manager=run_manager, **kwargs)


class Hang(GenericFakeChatModel):
    """What: a fake model that never answers in time. Use when: classifier timeout tests. Inputs: messages.
    Output: never. Side effects: sleeps. Role: test double."""

    async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs):  # type: ignore[override]
        """What: sleep past any gate timeout. Use when: called. Inputs: messages. Output: none.
        Side effects: sleeps. Role: fake."""
        await asyncio.sleep(5)
        raise AssertionError("unreachable")


def classifier(answer: str | None) -> ScopeClassifier:
    """What: a ScopeClassifier over a fake that answers `answer` (None: hangs). Use when: tests.
    Inputs: answer text. Output: classifier with a 0.1 s timeout. Side effects: none. Role: helper."""
    model = Hang(messages=iter([])) if answer is None else Fake(messages=iter([AIMessage(answer)]))
    return ScopeClassifier(model, timeout_s=0.1, min_s=0.0)


def search_body(verdict: str, reason: str = "above_threshold") -> dict[str, Any]:
    """What: a contract-shaped SearchResponse with one relevant chunk. Use when: the fake knowledge service.
    Inputs: verdict, reason. Output: dict. Side effects: none. Role: fixture."""
    chunk = {"chunk_id": "refund-policy#2", "article_slug": "refund-policy", "article_title": "Refund policy",
             "section_path": ["Refund policy", "Refunds on the Pro plan"],
             "text": "## Refunds on the Pro plan\nRefunds on Pro are allowed within 30 days.", "tiers": ["all"],
             "error_codes": [], "flags": [], "last_updated": "2026-11-20", "doc_version": 1, "content_hash": "h",
             "source_url": "https://notion.so/refund-policy#pro", "relevant": verdict != "none",
             "scores": {"cosine": 0.71, "vector_rank": 1, "keyword_rank": 1, "rrf_score": 0.03, "fused_rank": 1},
             "citation": {"ref": "refund-policy#2", "label": "Refund policy", "spoken": "our refund policy"}}
    return {"request_id": "r", "trace_id": TID, "corpus_version": "v1", "embedding_model": "m",
            "query": {"raw": "q", "normalized": "q", "codes": [], "unknown_codes": []}, "allowed_tiers": ["all", "pro"],
            "chunks": [] if verdict == "error" else [chunk],
            "confidence": {"verdict": verdict, "reason": reason, "top_cosine": None if verdict == "error" else 0.71,
                           "tau_low": 0.3, "tau_high": 0.45, "support_count": 1, "code_match": False,
                           "legs": {"vector": "ok", "keyword": "ok"}, "calibration_id": "cal-1",
                           "calibration_status": "provisional", "calibration_stale": False},
            "timing_ms": {"embed": 1, "sql": 1, "total": 2}}


class Harness:
    """What: one agent over the built tools, a scripted model and a fake knowledge service. Use when: each test.
    Inputs: knowledge response (dict, HTTP status, or "timeout"), scope classifier, prior weak_streak.
    Output: run() gives (outcome, events, final state values). Side effects: memory only. Role: test wiring."""

    def __init__(self, knowledge: Any, *, scope: ScopeClassifier | None = None, answer: str = "Pro refunds: 30 days.",
                 search_twice: bool = False) -> None:
        """What: build the agent. Use when: tests. Inputs: see class. Output: none. Side effects: none.
        Role: constructor."""
        self.core_app = create_app()
        self.core_app.state.verified.add(SID)

        def kn(request: httpx.Request) -> httpx.Response:
            """What: fake /v1/search. Use when: search_kb. Inputs: request. Output: response or raises.
            Side effects: none. Role: fake."""
            if knowledge == "timeout":
                raise httpx.ReadTimeout("slow", request=request)
            if isinstance(knowledge, int):
                return httpx.Response(knowledge, json={"error": {"class": "retrieval_unavailable",
                                      "code": "RETRIEVAL_UNAVAILABLE", "message": "down", "retryable": True}})
            return httpx.Response(200, json=knowledge)

        core = CoreAgentClient("http://core:8000", "agent-tok", http=httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.core_app)))
        tools = build_tools(core, KnowledgeClient("http://knowledge:8000", "kn-tok",
                                                  http=httpx.AsyncClient(transport=httpx.MockTransport(kn))))
        call = {"name": "search_kb", "args": {"query": "refund window"}, "type": "tool_call"}
        replies = [AIMessage("", tool_calls=[{**call, "id": "c1"}])]
        if search_twice:
            replies.append(AIMessage("", tool_calls=[{**call, "id": "c2"}]))
        replies.append(AIMessage(answer))
        self.model = Fake(messages=iter(replies), seen=[], disable_streaming=True)
        self.saver = InMemorySaver()
        self.agent = build_agent(self.model, fallback_model=Fake(messages=iter([])), checkpointer=self.saver,
                                 tools=tools, scope_classifier=scope)

    async def run(self, state: dict[str, Any] | None = None, text: str = "how long is the refund window?"
                  ) -> tuple[TurnOutcome, list[Any], dict[str, Any]]:
        """What: one turn. Use when: tests. Inputs: extra input state, user text. Output: (outcome, events,
        state values). Side effects: memory only. Role: helper."""
        stream = TurnStream(TurnMeta(SID, TID, "chat", "agent_a", "text"))
        control = TurnControl(session_id=SID, turn_id=TID, mode="chat")

        async def body(s: TurnStream) -> TurnOutcome:
            """What: agent body. Use when: start_turn. Inputs: stream. Output: outcome. Side effects: none.
            Role: helper."""
            return await stream_agent_turn(s, self.agent, {"messages": [HumanMessage(text)], **OWNER, **(state or {})},
                                           run_config(SID), control=control)

        async def write_final(_: TurnOutcome) -> None:
            """What: no-op. Use when: start_turn. Inputs: outcome. Output: none. Side effects: none. Role: fake."""

        task = start_turn(stream, body, write_final=write_final)
        _ = [f async for f in stream.frames()]
        outcome = await task
        st = await self.agent.aget_state(run_config(SID))
        return outcome, stream.events, st.values


def gates(events: list[Any], gate: str = "retrieval") -> list[Any]:
    """What: the gate events of one kind. Use when: asserting. Inputs: events, gate. Output: list.
    Side effects: none. Role: helper."""
    return [e for e in events if e.type == "gate" and e.gate == gate]


# --- the pure decision table (doc 07 s6.5) -------------------------------------------------------------------
@pytest.mark.parametrize(("verdict", "streak", "expected"), [
    ("pass", 0, ("answer", None)), ("pass", 1, ("answer", None)),
    ("weak", 0, ("clarify", None)), ("weak", 1, ("escalate", "not_in_docs")),
    ("uncalibrated", 0, ("clarify", None)), ("uncalibrated", 1, ("escalate", "not_in_docs")),
    ("none", 0, ("scope_check", None)),
    ("error", 0, ("escalate", "retrieval_unavailable")),
    ("something_new", 0, ("escalate", "retrieval_unavailable")),  # unknown verdict: fail safe, never not_in_docs
])
def test_retrieval_decision_table(verdict: str, streak: int, expected: tuple[str, str | None]):
    assert retrieval_decision(verdict, streak) == expected


# --- row 1: pass ---------------------------------------------------------------------------------------------
async def test_pass_gives_chunks_to_the_model_resets_weak_streak_and_emits_a_full_gate_event():
    h = Harness(search_body("pass"))
    outcome, events, values = await h.run(state={"weak_streak": 1})
    assert outcome.end_reason == "answered" and outcome.final.text == "Pro refunds: 30 days."
    tool_seen = str(h.model.seen[-1][-1].content)
    assert 'id="refund-policy#2"' in tool_seen and "clarifying" not in tool_seen
    [g] = gates(events)
    assert (g.verdict, g.decision, g.top_cosine, g.tau_low, g.tau_high) == ("pass", "answer", 0.71, 0.3, 0.45)
    assert (g.calibration_status, g.code_match, g.chunk_ids) == ("provisional", False, ["refund-policy#2"])
    assert g.step_id.startswith(TID + ":")
    assert values["weak_streak"] == 0
    assert outcome.final.confidence is not None and outcome.final.confidence.retrieval_verdict == "pass"


# --- row 2: weak and uncalibrated ---------------------------------------------------------------------------
@pytest.mark.parametrize("verdict", ["weak", "uncalibrated"])
async def test_first_weak_turn_adds_the_clarify_instruction_and_counts_one(verdict: str):
    h = Harness(search_body(verdict, "weak_match"), answer="Do you mean the Pro or the Business plan?")
    outcome, events, values = await h.run()
    assert "Ask exactly one clarifying question" in str(h.model.seen[-1][-1].content)
    [g] = gates(events)
    assert (g.verdict, g.decision) == (verdict, "clarify")
    assert outcome.end_reason == "clarify" and not outcome.final.escalated
    assert values["weak_streak"] == 1


@pytest.mark.parametrize("verdict", ["weak", "uncalibrated"])
async def test_second_consecutive_weak_turn_escalates_not_in_docs(verdict: str):
    h = Harness(search_body(verdict, "weak_match"))
    outcome, events, values = await h.run(state={"weak_streak": 1})
    [g] = gates(events)
    assert (g.verdict, g.decision) == (verdict, "escalate")
    assert outcome.end_reason == "escalated" and outcome.final.escalated
    assert outcome.final.text.startswith("Our help articles don't cover that.")
    assert len(h.model.seen) == 1  # the router ended the turn; no second model call
    assert values["weak_streak"] == 0


async def test_two_weak_searches_in_one_turn_count_once():
    h = Harness(search_body("weak", "weak_match"), answer="Which plan are you on?", search_twice=True)
    outcome, events, values = await h.run()
    assert [g.decision for g in gates(events)] == ["clarify", "clarify"]
    assert outcome.end_reason == "clarify" and values["weak_streak"] == 1


async def test_a_turn_that_does_not_end_weak_resets_the_streak():
    h = Harness(search_body("pass"))
    h.model.messages = iter([AIMessage("Hello! How can I help?")])  # no search this turn
    _, _, values = await h.run(state={"weak_streak": 1}, text="hi")
    assert values["weak_streak"] == 0


# --- row 3: none through the scope classifier ----------------------------------------------------------------
async def test_none_off_topic_declines_with_the_fixed_line_and_no_ticket():
    h = Harness(search_body("none", "below_threshold"), scope=classifier("OFF"))
    outcome, events, _ = await h.run(text="write me a poem about the sea")
    [g] = gates(events)
    assert (g.verdict, g.decision, g.chunk_ids) == ("none", "decline", ["refund-policy#2"])
    assert outcome.end_reason == "declined_off_topic" and not outcome.final.escalated
    assert outcome.final.ticket is None and outcome.final.text.startswith("I can only help with Quillstack")


async def test_none_on_topic_escalates_not_in_docs():
    h = Harness(search_body("none", "unknown_error_code"), scope=classifier("ON"))
    outcome, events, _ = await h.run(text="what does error E-SSO-999 mean?")
    [g] = gates(events)
    assert (g.verdict, g.decision) == ("none", "escalate")
    assert outcome.end_reason == "escalated" and outcome.final.text.startswith("Our help articles don't cover that.")


@pytest.mark.parametrize("scope", [None, "hang"], ids=["not_wired", "timeout"])
async def test_none_with_a_failed_classifier_counts_as_on_topic(scope: str | None):
    h = Harness(search_body("none", "below_threshold"), scope=classifier(None) if scope else None)
    outcome, events, _ = await h.run()
    [g] = gates(events)
    assert g.decision == "escalate" and outcome.end_reason == "escalated"
    assert outcome.final.text.startswith("Our help articles don't cover that.")


async def test_classifier_calls_are_tagged_internal_gate_and_nostream():
    seen: list[Any] = []

    class Spy(Fake):
        """What: records the run config tags. Use when: this test. Inputs: messages. Output: "OFF".
        Side effects: appends. Role: fake."""

        async def ainvoke(self, input: Any, config: Any = None, **kw: Any) -> Any:  # type: ignore[override]
            """What: record tags then answer. Use when: classifier call. Inputs: input, config. Output: message.
            Side effects: appends. Role: fake."""
            seen.append((config or {}).get("tags"))
            return AIMessage("OFF")

    h = Harness(search_body("none", "below_threshold"), scope=ScopeClassifier(Spy(messages=iter([]))))
    await h.run()
    assert seen == [["internal:gate", "nostream"]]


# --- row 4: error, 503, timeout ----------------------------------------------------------------------------
@pytest.mark.parametrize("knowledge", [
    pytest.param(search_body("error", "retrieval_unavailable"), id="verdict_error"),
    pytest.param(search_body("error", "embedding_unavailable"), id="embedding_unavailable"),
    pytest.param(503, id="http_503"),
    pytest.param("timeout", id="timeout"),
])
async def test_retrieval_error_escalates_retrieval_unavailable_and_never_says_not_in_docs(knowledge: Any):
    h = Harness(knowledge, scope=classifier("ON"))
    outcome, events, _ = await h.run()
    [g] = gates(events)
    assert (g.verdict, g.decision) == ("error", "escalate")
    assert outcome.end_reason == "escalated" and outcome.final.escalated
    assert outcome.final.text.startswith("I couldn't search our help articles just now, so I won't guess.")
    for line in (outcome.final.text, outcome.final.say):
        assert not any(p in line.lower() for p in NOT_IN_DOCS), line
    assert len(h.model.seen) == 1  # no answer from memory


# --- drawer contract (coordinator note, PR #41): every retrieval gate event sets the score fields --------------
@pytest.mark.parametrize(("knowledge", "scope"), [
    pytest.param(search_body("pass"), None, id="pass"),
    pytest.param(search_body("weak", "weak_match"), None, id="weak"),
    pytest.param(search_body("none", "below_threshold"), "OFF", id="none"),
    pytest.param(search_body("error", "retrieval_unavailable"), None, id="verdict_error"),
    pytest.param(503, None, id="http_503"),
    pytest.param("timeout", None, id="timeout"),
])
async def test_every_retrieval_gate_event_sets_code_match_and_the_score_fields(knowledge: Any, scope: str | None):
    h = Harness(knowledge, scope=classifier(scope) if scope else None)
    _, events, _ = await h.run()
    [g] = gates(events)
    assert isinstance(g.code_match, bool)
    assert {"verdict", "top_cosine", "tau_low", "tau_high", "calibration_status", "code_match"} <= g.model_fields_set
    assert isinstance(g.calibration_status, str)
