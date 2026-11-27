"""What: tests for Gate 2 grounding (CS-E-02.S4.T2; doc 07 s6.4, s8.1 "Gate 2 verifier", s14 rows 3 and 6).
Use when: pytest (fake chat models, the contracts core stub and a fake knowledge service; no provider, no network).
Inputs: none. Output: pass/fail. Side effects: none.
Role: Done when: a verifier timeout counts as fail; the checkpoint holds no failed draft; two fails give one
ticket."""
from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from contracts.stubs.core_stub import create_app
from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage
from langchain_core.outputs import ChatGenerationChunk
from langgraph.checkpoint.memory import InMemorySaver
from test_gate1 import OWNER, SID, TID, Fake, Hang, search_body

from agent.assembly import build_agent, build_middleware, run_config
from agent.clients.core_agent import CoreAgentClient
from agent.clients.knowledge import KnowledgeClient
from agent.gates import CLARIFY_LINE, GroundingVerifier, needs_grounding
from agent.middleware.grounding import GroundingMiddleware
from agent.sse import TurnMeta, TurnOutcome, TurnStream, start_turn, stream_agent_turn
from agent.tools import build_tools
from agent.turn_control import TurnControl

DRAFT = "Refunds on the Pro plan are allowed within 30 days of the charge."
TICKET = {"ticket_id": "TCK-26118213", "short_id": "8213", "display": "ticket ending 8213", "stage": "open",
          "priority": "P3", "queue": "billing", "target_label": "within 1 business day", "verified": True}


def verdict_json(supported: bool = True, commitment: bool = False) -> str:
    """What: a verifier reply. Use when: fakes. Inputs: claim supported, commitment. Output: JSON text.
    Side effects: none. Role: fixture."""
    return json.dumps({"claims": [{"text": "Pro refunds are allowed within 30 days", "supported": supported,
                                   "source_id": "refund-policy#2" if supported else None}],
                       "commitment": commitment})


def verifier(*replies: str | None, timeout_s: float = 0.2) -> GroundingVerifier:
    """What: a GroundingVerifier over a fake (None: a model that hangs). Use when: tests.
    Inputs: reply texts. Output: verifier with a short timeout. Side effects: none. Role: helper."""
    if replies and replies[0] is None:
        return GroundingVerifier(Hang(messages=iter([])), timeout_s=timeout_s, min_s=0.0)
    return GroundingVerifier(Fake(messages=iter([AIMessage(r) for r in replies if r is not None]), seen=[],
                                  disable_streaming=True), timeout_s=timeout_s, min_s=0.0)


class Tickets:
    """What: a recording open_ticket hook. Use when: escalation tests. Inputs: control. Output: a TicketRef dict.
    Side effects: appends the escalation reason. Role: stands in for the escalation module (CS-E-03.S4.T1)."""

    def __init__(self) -> None:
        """What: empty log. Use when: tests. Inputs: none. Output: none. Side effects: none. Role: constructor."""
        self.reasons: list[str | None] = []

    async def __call__(self, control: TurnControl) -> dict[str, Any]:
        """What: record and return a ticket. Use when: an escalate route is written. Inputs: control.
        Output: ticket dict. Side effects: appends. Role: fake hook."""
        self.reasons.append(control.route.reason if control.route else None)
        return dict(TICKET)


class Harness:
    """What: one agent (same thread across turns) with search_kb against a fake knowledge service, a scripted
    model and a Gate 2 verifier. Use when: each test. Inputs: verifier, model replies (default: search then
    DRAFT), ticket hook. Output: run(). Side effects: memory only. Role: test wiring."""

    def __init__(self, ver: GroundingVerifier | None, replies: list[AIMessage] | None = None,
                 tickets: Tickets | None = None) -> None:
        """What: build the agent. Use when: tests. Inputs: see class. Output: none. Side effects: none.
        Role: constructor."""
        core_app = create_app()
        core_app.state.verified.add(SID)
        core = CoreAgentClient("http://core:8000", "agent-tok", http=httpx.AsyncClient(
            transport=httpx.ASGITransport(app=core_app)))
        kn = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=search_body("pass"))))
        tools = build_tools(core, KnowledgeClient("http://knowledge:8000", "kn-tok", http=kn))
        search = AIMessage("", tool_calls=[{"name": "search_kb", "args": {"query": "refund window"}, "id": "c1",
                                            "type": "tool_call"}])
        self.replies = replies if replies is not None else [search, AIMessage(DRAFT)]
        self.model = Fake(messages=iter(self.replies), seen=[], disable_streaming=True)
        self.tickets = tickets or Tickets()
        self.agent = build_agent(self.model, fallback_model=Fake(messages=iter([])), checkpointer=InMemorySaver(),
                                 tools=tools, grounding_verifier=ver, open_ticket=self.tickets)

    def script(self, *replies: AIMessage) -> None:
        """What: replace the model's remaining replies. Use when: a second turn. Inputs: replies. Output: none.
        Side effects: mutates the fake. Role: helper."""
        self.model.messages = iter(replies)

    async def run(self, state: dict[str, Any] | None = None, turn_id: str = TID, mode: str = "chat"
                  ) -> tuple[TurnOutcome, list[Any], dict[str, Any]]:
        """What: one turn. Use when: tests. Inputs: extra input state, turn id, mode. Output: (outcome, events,
        state values). Side effects: memory only. Role: helper."""
        stream = TurnStream(TurnMeta(SID, turn_id, mode, "agent_a", "text"))
        control = TurnControl(session_id=SID, turn_id=turn_id, mode=mode)

        async def body(s: TurnStream) -> TurnOutcome:
            """What: agent body. Use when: start_turn. Inputs: stream. Output: outcome. Side effects: none.
            Role: helper."""
            return await stream_agent_turn(s, self.agent, {"messages": [HumanMessage("refund window on Pro?")],
                                                           **OWNER, **(state or {})}, run_config(SID), control=control)

        async def write_final(_: TurnOutcome) -> None:
            """What: no-op. Use when: start_turn. Inputs: outcome. Output: none. Side effects: none. Role: fake."""

        task = start_turn(stream, body, write_final=write_final)
        _ = [f async for f in stream.frames()]
        outcome = await task
        st = await self.agent.aget_state(run_config(SID))
        return outcome, stream.events, st.values


def grounding_gate(events: list[Any]) -> Any:
    """What: the single grounding gate event. Use when: asserting. Inputs: events. Output: event.
    Side effects: none. Role: helper."""
    [g] = [e for e in events if e.type == "gate" and e.gate == "grounding"]
    return g


def tokens(events: list[Any]) -> list[Any]:
    """What: token events (TurnStream.events keeps no tokens, so this reads the stream's emitted types).
    Use when: asserting nothing was flushed. Inputs: events. Output: list. Side effects: none. Role: helper."""
    return [e for e in events if e.type == "token"]


def texts(values: dict[str, Any]) -> list[str]:
    """What: the text of every message in the checkpoint. Use when: asserting a draft is gone. Inputs: state
    values. Output: list of texts. Side effects: none. Role: helper."""
    return [str(m.content) for m in values["messages"]]


# --- needs_grounding (doc 07 s6.4 bullet 2) ------------------------------------------------------------------
@pytest.mark.parametrize(("draft", "had_tool", "expected"), [
    ("Hello! How can I help you today?", False, False),
    ("Hello! How can I help you today?", True, True),
    ("You can get a refund.", False, True), ("It costs $96.00 a month.", False, True),
    ("That takes 5 days.", False, True), ("A 20% discount applies.", False, True),
    ("Our policy says so.", False, True), ("You are eligible.", False, True), ("Do it within a week.", False, True),
    ("Upgrade to Business.", False, True), ("The Pro plan has SSO.", False, True),
])
def test_needs_grounding_rule(draft: str, had_tool: bool, expected: bool):
    assert needs_grounding(draft, had_tool) is expected


# --- skipped -------------------------------------------------------------------------------------------------
async def test_a_draft_that_needs_no_grounding_is_skipped_and_flushed_without_a_verifier_call():
    ver = verifier()
    h = Harness(ver, replies=[AIMessage("Hello! How can I help you today?")])
    outcome, events, _ = await h.run()
    g = grounding_gate(events)
    assert (g.verdict, g.decision) == ("skipped", "skip")
    assert not [e for e in events if e.type == "status" and e.kind == "checking_policy"]
    assert outcome.final.text == "Hello! How can I help you today?" and outcome.end_reason == "answered"
    assert outcome.final.confidence is not None and outcome.final.confidence.grounding == "skipped"
    assert ver.model.seen == []


# --- pass ----------------------------------------------------------------------------------------------------
async def test_pass_emits_checking_policy_then_the_gate_flushes_the_draft_and_adds_citations():
    h = Harness(verifier(verdict_json(True)))
    outcome, events, values = await h.run(state={"grounding_failures": 1})
    types = [e.type for e in events]
    status_i = next(i for i, e in enumerate(events) if e.type == "status" and e.kind == "checking_policy")
    assert events[status_i].message == "Checking the answer against policy..."
    gate_i = types.index("gate", status_i)
    g = events[gate_i]
    assert (g.gate, g.verdict, g.decision, g.claims_total, g.claims_supported, g.commitment) == (
        "grounding", "pass", "answer", 1, 1, False)
    assert g.claims[0].source_id == "refund-policy#2"
    assert outcome.end_reason == "answered" and outcome.final.text == DRAFT
    [c] = outcome.final.citations
    assert (c.chunk_id, c.url, c.label, c.section_title, c.score) == (
        "refund-policy#2", "https://notion.so/refund-policy#pro", "Refund policy", "Refunds on the Pro plan", 0.71)
    assert c.excerpt == "Refunds on Pro are allowed within 30 days."  # body only, header line dropped
    assert outcome.final.confidence is not None and outcome.final.confidence.grounding == "pass"
    assert values["grounding_failures"] == 0


async def test_verifier_calls_are_tagged_internal_gate_and_nostream_and_never_stream_tokens():
    seen: list[Any] = []

    class StreamingSpy(Fake):
        """What: a verifier that streams its JSON and records tags. Use when: this test. Inputs: messages.
        Output: chunks. Side effects: appends. Role: fake."""

        async def _astream(self, messages, stop=None, run_manager=None, **kwargs):  # type: ignore[override]
            """What: stream the verdict in two chunks. Use when: called. Inputs: messages. Output: chunks.
            Side effects: none. Role: fake."""
            body = verdict_json(True)
            for part in (body[:10], body[10:]):
                chunk = ChatGenerationChunk(message=AIMessageChunk(content=part))
                if run_manager:
                    await run_manager.on_llm_new_token(part, chunk=chunk)
                yield chunk

        async def ainvoke(self, input: Any, config: Any = None, **kw: Any) -> Any:  # type: ignore[override]
            """What: record tags, then run normally. Use when: verifier call. Inputs: input, config.
            Output: message. Side effects: appends. Role: fake."""
            seen.append((config or {}).get("tags"))
            return await super().ainvoke(input, config, **kw)

    h = Harness(GroundingVerifier(StreamingSpy(messages=iter([])), timeout_s=1, min_s=0.0))
    outcome, events, _ = await h.run()
    assert seen == [["internal:gate", "nostream"]]
    assert outcome.final.text == DRAFT and grounding_gate(events).verdict == "pass"
    assert not any('"claims"' in str(getattr(e, "text", "")) for e in events)


# --- fail ----------------------------------------------------------------------------------------------------
async def test_first_fail_removes_the_draft_from_the_checkpoint_and_says_the_fixed_clarifying_line():
    h = Harness(verifier(verdict_json(False)))
    outcome, events, values = await h.run()
    g = grounding_gate(events)
    assert (g.verdict, g.decision, g.claims_total, g.claims_supported) == ("fail", "clarify", 1, 0)
    assert outcome.end_reason == "clarify" and outcome.final.text == CLARIFY_LINE and not outcome.final.escalated
    assert CLARIFY_LINE == ("I want to be sure I give you the right answer. Could you tell me a little more about "
                            "what you need?")
    assert DRAFT not in texts(values) and texts(values)[-1] == CLARIFY_LINE
    assert values["grounding_failures"] == 1
    assert h.tickets.reasons == []
    assert outcome.final.confidence is not None and outcome.final.confidence.grounding == "fail"
    assert outcome.final.citations == []


@pytest.mark.parametrize("reply", [None, "not json at all", '{"claims": "nope"}'], ids=["timeout", "garbage", "bad"])
async def test_a_verifier_timeout_or_unreadable_reply_counts_as_fail(reply: str | None):
    h = Harness(verifier(reply))
    outcome, events, values = await h.run()
    g = grounding_gate(events)
    assert (g.verdict, g.decision) == ("error", "clarify")
    assert outcome.end_reason == "clarify" and outcome.final.text == CLARIFY_LINE
    assert DRAFT not in texts(values) and values["grounding_failures"] == 1


async def test_two_consecutive_fails_give_one_ticket_and_the_checkpoint_holds_neither_draft():
    h = Harness(verifier(verdict_json(False), verdict_json(False)))
    first, _, _ = await h.run()
    assert first.end_reason == "clarify" and h.tickets.reasons == []
    h.script(AIMessage("", tool_calls=[{"name": "search_kb", "args": {"query": "pro refund"}, "id": "c2",
                                        "type": "tool_call"}]), AIMessage(DRAFT + " Guaranteed."))
    second, events, values = await h.run(turn_id="019a7c2e-5b20-7000-8000-0000000000c2")
    g = grounding_gate(events)
    assert (g.verdict, g.decision) == ("fail", "escalate")
    assert second.end_reason == "escalated" and second.final.escalated
    assert second.final.text == ("I couldn't confirm that answer against our policies, so I won't guess. "
                                 "I've opened a ticket for our team, ticket ending 8213.")
    assert h.tickets.reasons == ["grounding_failed"]  # exactly one ticket across the two fails
    assert not any(t.startswith(DRAFT) for t in texts(values))
    assert values["grounding_failures"] == 0


async def test_a_pass_between_fails_resets_the_counter():
    h = Harness(verifier(verdict_json(True)))
    _, _, values = await h.run(state={"grounding_failures": 1})
    assert values["grounding_failures"] == 0 and h.tickets.reasons == []


async def test_commitment_escalates_needs_commitment_at_once():
    h = Harness(verifier(verdict_json(True, commitment=True)))
    outcome, events, values = await h.run()
    g = grounding_gate(events)
    assert (g.decision, g.commitment) == ("escalate", True)
    assert outcome.end_reason == "escalated"
    assert outcome.final.text.startswith("That needs a decision from our team, so I won't promise it here.")
    assert h.tickets.reasons == ["needs_commitment"] and DRAFT not in texts(values)


async def test_voice_turn_gets_the_same_fixed_line():
    h = Harness(verifier(verdict_json(False)))
    outcome, _, _ = await h.run(mode="voice")
    assert outcome.final.text == CLARIFY_LINE and outcome.final.say == CLARIFY_LINE


# --- wiring --------------------------------------------------------------------------------------------------
def test_grounding_middleware_is_real_and_wired_with_the_verifier():
    ver = verifier()
    mw = {m.name: m for m in build_middleware(fallback_model=Fake(messages=iter([])), grounding_verifier=ver)}
    g = mw["GroundingMiddleware"]
    assert isinstance(g, GroundingMiddleware) and g.verifier is ver
    assert getattr(type(g).aafter_model, "__can_jump_to__", None) == ["end"]


def test_registry_wiring_attaches_both_gate_models(monkeypatch: pytest.MonkeyPatch):
    from pathlib import Path

    from agent import assembly
    from agent.models.config import load_models_config
    from agent.models.registry import ModelRegistry

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-not-real")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-not-real")
    captured: dict[str, Any] = {}
    monkeypatch.setattr(assembly, "build_agent", lambda *a, **kw: captured.update(kw))
    reg = ModelRegistry(load_models_config(Path(__file__).resolve().parents[3] / "data" / "models.yaml"))
    assembly.build_agent_from_registry(reg, checkpointer=InMemorySaver())
    assert captured["grounding_verifier"].timeout_s == 3 and captured["scope_classifier"].timeout_s == 2
    assert "haiku" in str(getattr(captured["grounding_verifier"].model, "model", ""))
