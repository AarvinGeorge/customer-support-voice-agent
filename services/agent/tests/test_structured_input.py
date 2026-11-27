"""What: ingress step 13 end to end through Ingress and the TurnRunner against the contracts core stub: S1 commits
by a Confirm press and by a typed strict yes, the read-back turn emits confirmation.required with the card fields,
adjacency and the "does not qualify" rows of doc 07 s7.3, the masked verification card's five results, and that the
commit token (never the agent token) carries commit and cancel with X-Turn-Id.
Use when: pytest (fake model, in-memory checkpointer, store and lock; ASGI stub; no network).
Inputs: none. Output: pass/fail. Side effects: none.
Role: Done when of CS-E-03.S7.T8 for the flows; outcome rows are in test_confirm.py."""
from __future__ import annotations

import uuid
from dataclasses import replace
from typing import Any

import httpx
from contracts.agent_api import TurnRequest
from contracts.stubs import examples as ex
from contracts.stubs.core_stub import create_app
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.checkpoint.memory import InMemorySaver

from agent.assembly import build_agent, run_config
from agent.clients.core_agent import CoreAgentClient
from agent.clients.knowledge import KnowledgeClient
from agent.ingress import Ingress, IngressInput, IngressStream, SessionRow, input_kind_of
from agent.structured.confirm import Confirmer, CoreCommitClient, agent_ticket_opener
from agent.structured.handler import StructuredInput
from agent.structured.verification import VERIFIED_THREAD_LINE
from agent.tools import build_tools
from agent.turn_body import TurnRunner
from ingress_fakes import MemoryTurnLock, MemoryTurnStore
from pin_probe import leaks

SID, VISITOR = ex.SESSION_ID, "visitor-1"
AGENT_TOKEN, COMMIT_TOKEN = "agent-tok", "commit-tok"
OWNER = {"verified": True, "claims_snapshot": ex.CLAIMS.model_dump(mode="json"),
         "context_snapshot": ex.ME_CONTEXT.model_dump(mode="json")}
PREPARE_CALL = {"name": "prepare_refund", "args": {"ending": "0413", "on": "2026-11-21", "reason": "duplicate",
                                                   "note": "charged twice"}, "id": "p1", "type": "tool_call"}


class Fake(GenericFakeChatModel):
    """What: scripted fake model that records prompts. Use when: tests. Inputs: messages. Output: replies.
    Side effects: records. Role: test double."""

    seen: list[list[Any]] = []

    def bind_tools(self, tools: Any, **kw: Any) -> "Fake":
        """What: no-op. Use when: binding. Inputs: tools. Output: self. Side effects: none. Role: plumbing."""
        return self

    async def _agenerate(self, messages: Any, *args: Any, **kwargs: Any) -> Any:
        """What: record, then answer. Use when: each model call. Inputs: messages. Output: ChatResult.
        Side effects: records. Role: probe."""
        Fake.seen.append(list(messages))
        return await super()._agenerate(messages, *args, **kwargs)


class Flags:
    """What: in-memory SessionFlags. Use when: tests. Inputs: none. Output: calls. Side effects: records.
    Role: fake of agent.sessions.awaiting_verification."""

    def __init__(self) -> None:
        """What: empty log. Use when: tests. Inputs: none. Output: none. Side effects: none. Role: constructor."""
        self.calls: list[tuple[str, bool, str | None]] = []

    async def set_awaiting_verification(self, session_id: str, awaiting: bool, verified_user_id: str | None = None) -> None:
        """What: record. Use when: verification. Inputs: id, flag, user. Output: none. Side effects: records.
        Role: fake."""
        self.calls.append((session_id, awaiting, verified_user_id))


class World:
    """What: ingress with the full step 13 handler on the core stub. Use when: each test. Inputs: model replies,
    whether the stub session starts verified, initial thread state. Output: helpers. Side effects: none.
    Role: wiring."""

    def __init__(self, replies: list[AIMessage] | None = None, *, verified: bool = True,
                 commit_transport: httpx.MockTransport | None = None) -> None:
        """What: build it. Use when: tests. Inputs: see class, plus an optional scripted transport for the commit
        client only. Output: none. Side effects: none. Role: constructor."""
        Fake.seen = []
        self.stub = create_app()
        if verified:
            self.stub.state.verified.add(SID)
        http = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.stub))
        core = CoreAgentClient("http://core:8000", AGENT_TOKEN, http=http)
        knowledge = KnowledgeClient("http://knowledge:8000", "kn",
                                    http=httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(503))))
        self.agent = build_agent(Fake(messages=iter(replies or []), disable_streaming=True),
                                 fallback_model=Fake(messages=iter([])), checkpointer=InMemorySaver(),
                                 tools=build_tools(core, knowledge))
        commit_http = httpx.AsyncClient(transport=commit_transport) if commit_transport is not None else http
        confirmer = Confirmer(CoreCommitClient("http://core:8000", COMMIT_TOKEN, http=commit_http), agent_ticket_opener(core))
        self.flags = Flags()
        self.store = MemoryTurnStore(SessionRow(session_id=SID, visitor_id=VISITOR))
        self.handler = handler = StructuredInput(core, confirmer, flags=self.flags, last_turn=self.store.last_turn)
        self.ingress = Ingress(self.store, MemoryTurnLock(),
                               served_by="agent_a", run_agent=TurnRunner(self.agent, core, structured=handler,
                                                                          flags=self.flags))

    async def seed(self, values: dict[str, Any]) -> None:
        """What: initial thread state. Use when: arranging. Inputs: values. Output: none. Side effects: checkpoint.
        Role: helper."""
        await self.agent.aupdate_state(run_config(SID), values)

    async def send(self, mode: str = "chat", **body: Any) -> tuple[Any, list[Any]]:
        """What: one turn with a TurnRequest body. Use when: tests. Inputs: mode (chat, or voice for the I3 refusal),
        text, confirm or verification. Output: (outcome, events). Side effects: as a turn. Role: helper."""
        req = TurnRequest.model_validate({"client_turn_id": str(uuid.uuid4()), **body})
        res = await self.ingress.handle(IngressInput(session_id=SID, visitor_id=VISITOR,
                                                     client_turn_id=str(req.client_turn_id), mode=mode,
                                                     input_kind=input_kind_of(req), text=req.text, body=req))
        assert isinstance(res, IngressStream) and res.task is not None
        _ = [f async for f in res.stream.frames()]
        return await res.task, res.stream.events

    def core_calls(self, suffix: str) -> list[Any]:
        """What: stub calls whose path ends with suffix. Use when: asserting. Inputs: suffix. Output: calls.
        Side effects: none. Role: helper."""
        return [c for c in self.stub.state.calls if c.path.endswith(suffix)]

    async def values(self) -> dict[str, Any]:
        """What: thread values. Use when: asserting. Inputs: none. Output: dict. Side effects: none. Role: helper."""
        return (await self.agent.aget_state(run_config(SID))).values


def of(events: list[Any], type_: str) -> list[Any]:
    """What: events of one type. Use when: asserting. Inputs: events, type. Output: list. Side effects: none.
    Role: helper."""
    return [e for e in events if getattr(e, "type", None) == type_]


async def readback_world(extra_replies: list[AIMessage] | None = None) -> tuple[World, list[Any], Any]:
    """What: a world after the S1 prepare turn (Priya asks for the duplicate refund; the model calls prepare_refund;
    the read-back ends the turn). Use when: confirm tests. Inputs: replies after the prepare turn.
    Output: (world, prepare-turn events, prepare-turn outcome). Side effects: as turns. Role: helper."""
    w = World([AIMessage(content="", tool_calls=[PREPARE_CALL]), *(extra_replies or [])])
    await w.seed({"messages": [HumanMessage("hi"), AIMessage("Hello", id="a0")], **OWNER})
    outcome, events = await w.send(text="Please refund the duplicate $96 charge from 21 November")
    return w, events, outcome


async def test_readback_turn_emits_confirmation_required_with_the_card_fields() -> None:
    _, events, outcome = await readback_world()
    assert outcome.end_reason == "readback" and outcome.final.text == ex.READBACK.chat
    (conf,) = of(events, "confirmation.required")
    assert conf.confirm_label == "Confirm refund"
    card = conf.pending
    assert card.pending_action_id == ex.PENDING_ID and card.kind == "commit" and card.state == "prepared"
    assert card.readback_text == ex.READBACK.chat and card.readback_say == ex.READBACK.voice
    assert card.amount_cents == 9600 and card.reason_code == "PRO_WITHIN_LIMITS" and card.title.startswith("Refund $96.00")
    assert outcome.final.pending_action is not None and outcome.final.pending_action.pending_action_id == ex.PENDING_ID


async def test_s1_commits_by_confirm_press_with_the_commit_token_and_x_turn_id() -> None:
    w, _, first = await readback_world()
    outcome, events = await w.send(confirm={"pending_action_id": ex.PENDING_ID, "decision": "confirm"})
    assert outcome.end_reason == "committed" and outcome.final.text == ex.COMMIT_RESULT.summary
    (res,) = of(events, "action.result")
    assert res.outcome == "committed" and res.changed is True and res.result_text == ex.COMMIT_RESULT.summary
    (commit,) = w.core_calls("/commit")
    assert commit.token == COMMIT_TOKEN and commit.headers["idempotency-key"] == ex.PENDING_ID
    assert commit.headers["x-turn-id"] == outcome_turn_id(events) != prepare_turn_id(w)
    v = await w.values()
    assert v["pending"] is None and v["messages"][-1].content == ex.COMMIT_RESULT.summary
    assert Fake.seen and len(Fake.seen) == 1  # only the prepare turn called the model


def outcome_turn_id(events: list[Any]) -> str:
    """What: the turn id of a turn's events. Use when: header asserts. Inputs: events. Output: id. Side effects: none.
    Role: helper."""
    return events[0].turn_id


def prepare_turn_id(w: World) -> str:
    """What: the X-Turn-Id the prepare carried. Use when: header asserts. Inputs: world. Output: id.
    Side effects: none. Role: helper."""
    (prep,) = w.core_calls("/refund/prepare")
    return prep.headers["x-turn-id"]


async def test_s1_commits_by_typed_strict_yes() -> None:
    w, _, _ = await readback_world()
    outcome, events = await w.send(text="Yes, go ahead!")
    assert outcome.end_reason == "committed" and of(events, "action.result")[0].outcome == "committed"
    assert len(w.core_calls("/commit")) == 1 and len(Fake.seen) == 1


async def test_strict_no_cancels_and_says_nothing_changed() -> None:
    w, _, _ = await readback_world()
    outcome, events = await w.send(text="no thanks")
    assert outcome.end_reason == "cancelled" and outcome.final.text == "Okay, I haven't changed anything."
    (res,) = of(events, "action.result")
    assert res.outcome == "cancelled" and res.changed is False
    (cancel,) = w.core_calls("/cancel")
    assert cancel.token == COMMIT_TOKEN and "x-turn-id" in cancel.headers


async def test_cancel_press_cancels() -> None:
    w, _, _ = await readback_world()
    outcome, _ = await w.send(confirm={"pending_action_id": ex.PENDING_ID, "decision": "cancel"})
    assert outcome.end_reason == "cancelled" and len(w.core_calls("/cancel")) == 1


async def test_press_for_another_pending_is_expired_with_no_core_call() -> None:
    w, _, _ = await readback_world()
    outcome, events = await w.send(confirm={"pending_action_id": "PA-00000000", "decision": "confirm"})
    assert outcome.final.text == "That request is no longer active. Ask me again if you still need it."
    (res,) = of(events, "action.result")
    assert res.outcome == "expired" and res.changed is False
    assert w.core_calls("/commit") == [] and w.core_calls("/cancel") == []


async def test_yes_that_is_not_adjacent_repeats_the_readback_then_the_next_yes_commits() -> None:
    w, _, _ = await readback_world([AIMessage("Refunds reach your card in 5 to 10 days.")])
    other, _ = await w.send(text="how long do refunds take?")  # goes to the model, with the pending in context
    human = [m for m in Fake.seen[-1] if isinstance(m, HumanMessage)][-1]
    assert f"pending: {ex.READBACK.chat} awaiting yes or no" in human.content and other.end_reason == "answered"
    again, events = await w.send(text="yes")
    assert again.end_reason == "readback" and again.final.text == ex.READBACK.chat
    assert w.core_calls("/commit") == [] and len(of(events, "confirmation.required")) == 1
    done, _ = await w.send(text="yes")
    assert done.end_reason == "committed" and len(w.core_calls("/commit")) == 1


async def test_yes_with_no_current_pending_goes_to_the_model() -> None:
    w = World([AIMessage("Glad to help.")])
    await w.seed({"messages": [HumanMessage("hi"), AIMessage("Anything else?", id="a0")], **OWNER})
    outcome, _ = await w.send(text="yes")
    assert outcome.end_reason == "answered" and len(Fake.seen) == 1 and w.core_calls("/commit") == []


async def test_strict_yes_inside_another_request_goes_to_the_model_and_never_commits() -> None:
    w, _, _ = await readback_world([AIMessage("I can only refund one charge at a time.")])
    outcome, _ = await w.send(text="yes but make it $50")
    assert outcome.end_reason == "answered" and w.core_calls("/commit") == []


# --- fix round 1: I1 delivery, I2 lapsed verification (D-055), I3 voice ---------------------------------------

async def test_yes_after_a_failed_turn_repeats_the_readback_and_never_commits() -> None:
    w, _, _ = await readback_world()  # no more scripted replies: the next model turn fails
    failed, _ = await w.send(text="actually can you make it $50 instead?")
    assert failed.status == "failed" or failed.end_reason == "error"
    again, events = await w.send(text="yes")
    assert again.end_reason == "readback" and again.final.text == ex.READBACK.chat
    assert w.core_calls("/commit") == [] and len(of(events, "confirmation.required")) == 1
    done, _ = await w.send(text="yes")  # the re-shown read-back was delivered, so this yes is adjacent
    assert done.end_reason == "committed" and len(w.core_calls("/commit")) == 1


async def test_yes_after_an_undelivered_readback_turn_repeats_the_readback() -> None:
    w, _, _ = await readback_world()
    (tid,) = list(w.store.turns)
    w.store.turns[tid] = replace(w.store.turns[tid], status="failed")  # the read-back turn's final row says failed
    again, _ = await w.send(text="yes")
    assert again.end_reason == "readback" and w.core_calls("/commit") == []
    done, _ = await w.send(text="yes")
    assert done.end_reason == "committed" and len(w.core_calls("/commit")) == 1


async def test_without_a_delivery_source_a_typed_yes_never_commits() -> None:
    w, _, _ = await readback_world()
    w.handler.last_turn = None  # fail closed: delivery cannot be proven
    again, _ = await w.send(text="yes")
    assert again.end_reason == "readback" and w.core_calls("/commit") == []


def not_verified_commit(request: httpx.Request) -> httpx.Response:
    """What: core answers a commit with 401 SESSION_NOT_VERIFIED (D-055). Use when: commit transport.
    Inputs: request. Output: 401 envelope. Side effects: none. Role: fake."""
    return httpx.Response(401, json={"error": {"class": "auth", "code": "SESSION_NOT_VERIFIED",
                                               "message": "Please verify first.", "retryable": False,
                                               "changed": False, "fault_injected": False, "details": {}}})


async def test_commit_401_session_not_verified_shows_the_card_keeps_the_pending_and_never_claims_success() -> None:
    w = World([AIMessage(content="", tool_calls=[PREPARE_CALL])],
              commit_transport=httpx.MockTransport(not_verified_commit))
    await w.seed({"messages": [HumanMessage("hi"), AIMessage("Hello", id="a0")], **OWNER})
    await w.send(text="Please refund the duplicate $96 charge from 21 November")
    outcome, events = await w.send(confirm={"pending_action_id": ex.PENDING_ID, "decision": "confirm"})
    (req,) = of(events, "verification.required")
    assert req.reason == "action" and req.fields == ["account_id", "pin"]
    (res,) = of(events, "action.result")
    assert res.outcome == "failed" and res.changed is False
    assert outcome.end_reason == "verification_requested" and "verify" in outcome.final.text.lower()
    assert "went through" not in outcome.final.text and ex.COMMIT_RESULT.summary not in outcome.final.text
    v = await w.values()
    assert v["pending"] and v["pending"]["id"] == ex.PENDING_ID and v["verified"] is False
    assert (SID, True, None) in w.flags.calls


async def test_lapsed_verification_at_the_refresh_on_a_confirm_turn_shows_the_card_not_no_longer_active() -> None:
    w, _, _ = await readback_world()
    w.stub.state.verified.discard(SID)  # claims expired: me/context now answers 401 SESSION_NOT_VERIFIED
    bodies: list[dict[str, Any]] = [{"confirm": {"pending_action_id": ex.PENDING_ID, "decision": "confirm"}},
                                    {"text": "yes"}]
    for body in bodies:
        outcome, events = await w.send(**body)
        assert len(of(events, "verification.required")) == 1 and outcome.end_reason == "verification_requested"
        assert "no longer active" not in outcome.final.text and w.core_calls("/commit") == []
        assert all(r.outcome != "expired" for r in of(events, "action.result"))
        assert (await w.values())["pending"]["id"] == ex.PENDING_ID


async def test_voice_text_yes_never_commits_and_repeats_the_readback_with_the_card() -> None:
    w, _, _ = await readback_world()
    outcome, events = await w.send(mode="voice", text="yes")
    assert outcome.end_reason == "readback" and w.core_calls("/commit") == []
    (conf,) = of(events, "confirmation.required")
    assert conf.pending.pending_action_id == ex.PENDING_ID
    assert ex.READBACK.voice in (outcome.final.say or "") and "press Confirm" in (outcome.final.say or "")
    again, _ = await w.send(mode="voice", text="yes")  # still never commits by voice text
    assert again.end_reason == "readback" and w.core_calls("/commit") == []
    done, _ = await w.send(mode="voice", confirm={"pending_action_id": ex.PENDING_ID, "decision": "confirm"})
    assert done.end_reason == "committed" and len(w.core_calls("/commit")) == 1


# --- verification card ---------------------------------------------------------------------------------------

async def test_card_success_verifies_loads_context_and_continues_with_a_value_free_thread_line() -> None:
    w = World([AIMessage("Thanks Priya, I can see the two charges.")], verified=False)
    outcome, events = await w.send(verification={"account_id": "qs 10423", "pin": ex.STUB_PIN})
    (res,) = of(events, "verification.result")
    assert res.status == "verified" and res.person.display_name == "Priya Raman"
    assert outcome.end_reason == "answered"
    human = [m for m in Fake.seen[-1] if isinstance(m, HumanMessage)][-1]
    assert VERIFIED_THREAD_LINE in human.content and "verification: just_verified" in human.content
    assert '<account_context untrusted="true">' in human.content
    v = await w.values()
    dumped = " ".join(str(m.content) for m in v["messages"])
    assert not leaks(dumped, ex.STUB_PIN) and "10423" not in dumped.replace(ex.ACCOUNT_ID, "")
    assert not leaks(str(outcome.final.model_dump()), ex.STUB_PIN) and v["verified"] is True
    assert w.flags.calls == [(SID, False, ex.CLAIMS.user_id)]
    assert w.core_calls("/identity/verify")[0].token == AGENT_TOKEN


async def test_card_failure_reports_attempts_left_and_asks_again() -> None:
    w = World(verified=False)
    outcome, events = await w.send(verification={"account_id": "QS-10423", "pin": "0000"})
    (res,) = of(events, "verification.result")
    assert res.status == "failed" and res.attempts_left == 2
    (req,) = of(events, "verification.required")
    assert req.reason == "retry" and req.attempts_left == 2
    assert outcome.end_reason == "verification_requested" and "2 tries left" in outcome.final.text
    assert "0000" not in " ".join(str(m.content) for m in (await w.values())["messages"])


async def test_card_third_failure_locks_and_escalates() -> None:
    w = World(verified=False)
    for _ in range(2):
        await w.send(verification={"account_id": "QS-10423", "pin": "0000"})
    outcome, events = await w.send(verification={"account_id": "QS-10423", "pin": "0000"})
    assert of(events, "verification.result")[0].status == "locked"
    assert outcome.end_reason == "escalated" and outcome.final.escalated


async def test_card_conflict_and_unavailable() -> None:
    w = World(verified=False)

    def conflict(request: httpx.Request) -> httpx.Response:
        """What: core answers IDENTITY_CONFLICT. Use when: transport. Inputs: request. Output: 409. Side effects: none.
        Role: fake."""
        return httpx.Response(409, json={"error": {"class": "core_conflict", "code": "IDENTITY_CONFLICT",
                                                   "message": "m", "retryable": False, "changed": False,
                                                   "fault_injected": False, "details": {}}})

    from agent.structured.verification import CONFLICT_LINE, UNAVAILABLE_LINE, verify_card
    from agent.sse import TurnMeta, TurnStream
    from agent.turn_control import TurnControl
    body = TurnRequest.model_validate({"client_turn_id": str(uuid.uuid4()),
                                       "verification": {"account_id": "QS-10423", "pin": "4821"}}).verification
    for transport, status, line in ((httpx.MockTransport(conflict), "conflict", CONFLICT_LINE),
                                    (httpx.MockTransport(lambda r: httpx.Response(503)), "unavailable", UNAVAILABLE_LINE)):
        stream = TurnStream(TurnMeta(SID, "019a7c2e-5b20-7000-8000-0000000000e1", "chat", "agent_a", "verification"))
        control = TurnControl(session_id=SID, turn_id=stream.meta.turn_id, events=stream)
        core = CoreAgentClient("http://core:8000", AGENT_TOKEN, http=httpx.AsyncClient(transport=transport))
        res = await verify_card(stream, control, body, core)
        assert res.outcome is not None and res.outcome.final.text == line
        assert of(stream.events, "verification.result")[0].status == status
    assert w is not None
