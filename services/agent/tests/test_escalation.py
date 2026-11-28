"""What: the L1 s7 escalation trigger table end to end through Ingress and the TurnRunner (doc 07 s3.4 owners, s6.2
`escalated`, s6.4, s6.5, s6.7, s8.1 failure-path ticket row): each trigger gives its reason and exactly one ticket, a
ticket-create failure gives the fallback line, both models unavailable gives the apology and the ticket_offer flow.
Use when: pytest (fake models, contracts core stub over ASGI with failure overrides, in-memory checkpointer).
Inputs: none. Output: pass/fail. Side effects: none.
Role: Done when of CS-E-03.S4.T1 (timing proofs of the reserve are in test_escalation_timing.py)."""
from __future__ import annotations

import json
import logging
from typing import Any

import httpx
import pytest
from contracts.stubs import examples as ex
from langchain.agents.middleware import AgentMiddleware, hook_config
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.tools import StructuredTool

import agent.assembly as assembly
from agent.escalation import MODEL_UNAVAILABLE_LINE, TICKET_OFFER_OPENED
from agent.middleware.router import escalate_from_hook
from agent.turn_control import Route, require_control
from escalation_world import OWNER, SID, Boom, Fake, World, error_response, of

def call(name: str, args: dict[str, Any], cid: str = "c1") -> AIMessage:
    """What: an AI message with one tool call. Use when: scripting the fake model. Inputs: tool, args, id.
    Output: AIMessage. Side effects: none. Role: helper."""
    return AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": cid, "type": "tool_call"}])


def routed_search(reason: str) -> StructuredTool:
    """What: a search_kb stand-in that routes like Gate 1 (doc 07 s6.5) for `reason`. Use when: the not_in_docs and
    retrieval_unavailable rows (Gate 1 routing itself is CS-E-02.S4). Inputs: reason. Output: tool. Side effects:
    sets the route. Role: test double of the Gate 1 decision."""

    async def search_kb(**raw: Any) -> tuple[str, dict[str, Any]]:
        """What: set route escalate(reason), return an empty verdict. Use when: the model searches.
        Inputs: query. Output: (content, artifact). Side effects: route. Role: double."""
        require_control().set_route(Route("escalate", reason=reason))
        return "ok", {"verdict": "none", "instruction": None, "docs": [], "chunk_ids": [], "confidence": {}}

    return StructuredTool(name="search_kb", description="search", coroutine=search_kb,
                          args_schema={"type": "object", "properties": {"query": {"type": "string"}},
                                       "required": ["query"], "additionalProperties": False},
                          response_format="content_and_artifact")


def ticket_bodies(w: World) -> list[tuple[dict[str, Any], str]]:
    """What: (body, Idempotency-Key) of every ticket POST. Use when: asserting one ticket. Inputs: world.
    Output: list. Side effects: none. Role: helper."""
    return [(json.loads(r.content), r.headers.get("idempotency-key", "")) for r in w.transport.tickets]


def assert_one_code_ticket(w: World, outcome: Any, events: list[Any], reason: str) -> None:
    """What: exactly one code ticket with the s3.4 key, one escalated event with the reason, ticket on the final.
    Use when: the code-owned rows. Inputs: world, outcome, events, reason. Output: none. Side effects: none.
    Role: shared assertion."""
    bodies = ticket_bodies(w)
    assert len(bodies) == 1, bodies
    body, key = bodies[0]
    assert key == f"{SID}:{outcome_turn(events)}:esc:{reason}"
    assert body["origin"] == "code" and body["escalation_reason"] == reason and body["transcript_ref"] == SID
    esc = of(events, "escalated")
    assert [(e.reason, e.origin, e.offer_ticket_button) for e in esc] == [(reason, "code", False)]
    assert esc[0].ticket.short_id == "8213"
    assert outcome.final.escalated is True and outcome.final.ticket.short_id == "8213"
    assert "ticket ending 8213" in outcome.final.text
    steps = [e for e in of(events, "step.started") if e.kind == "escalation"]
    assert [s.name for s in steps] == ["create_ticket"]


def outcome_turn(events: list[Any]) -> str:
    """What: the turn id of a turn's events. Use when: building the expected key. Inputs: events. Output: id.
    Side effects: none. Role: helper."""
    return events[0].turn_id


async def test_off_topic_declines_with_no_ticket() -> None:
    async def off_topic(**raw: Any) -> tuple[str, dict[str, Any]]:
        """What: Gate 1 off-topic decision. Use when: this test. Inputs: query. Output: result. Side effects: route.
        Role: double."""
        require_control().set_route(Route("decline_off_topic"))
        return "ok", {"verdict": "none", "instruction": None, "docs": [], "chunk_ids": [], "confidence": {}}

    tool = routed_search("x")
    tool.coroutine = off_topic
    w = World([call("search_kb", {"query": "weather"})], tools={"search_kb": tool})
    outcome, events = await w.send(text="what's the weather?")
    assert outcome.end_reason == "declined_off_topic"
    assert w.transport.tickets == [] and of(events, "escalated") == []


@pytest.mark.parametrize("reason,lead", [
    ("not_in_docs", "Our help articles don't cover that."),
    ("retrieval_unavailable", "I couldn't search our help articles just now, so I won't guess."),
])
async def test_gate1_rows_open_exactly_one_code_ticket(reason: str, lead: str) -> None:
    w = World([call("search_kb", {"query": "hipaa baa"})], tools={"search_kb": routed_search(reason)})
    outcome, events = await w.send(text="do you sign a HIPAA BAA?")
    assert outcome.end_reason == "escalated"
    assert outcome.final.text == f"{lead} I've opened a ticket for our team, ticket ending 8213."
    assert_one_code_ticket(w, outcome, events, reason)
    body = ticket_bodies(w)[0][0]
    assert "do you sign a HIPAA BAA?" in body["summary"] and body["category"] == "other"
    thread = (await w.values())["messages"]
    assert thread[-2].content == outcome.final.text  # the thread line carries the ticket, not the fallback
    assert str(thread[-1].id).startswith("record-")  # then the code-written record of the ticket (D-071)


class _Gate2(AgentMiddleware):
    """What: Gate 2 stand-in: on the first model draft, takes the escalation hook with `reason` (second
    consecutive grounding failure, or a commitment). Use when: the Gate 2 rows (GroundingMiddleware is CS-E-02.S4.T2).
    Inputs: reason. Output: the hook's update. Side effects: route. Role: double of the s6.4 decision."""

    reason = "grounding_failed"

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        """What: accept and ignore the GroundingMiddleware arguments (verifier, open_ticket). Use when: the double
        replaces GroundingMiddleware in build_middleware. Inputs: any. Output: none. Side effects: none.
        Role: constructor of the double."""
        super().__init__()

    @hook_config(can_jump_to=["end"])
    async def aafter_model(self, state: Any, runtime: Any) -> dict[str, Any] | None:
        """What: escalate on a final draft. Use when: after each model call. Inputs: state. Output: update.
        Side effects: route. Role: double."""
        last = state["messages"][-1]
        if isinstance(last, AIMessage) and not last.tool_calls:
            return escalate_from_hook(require_control(), self.reason, remove_id=last.id)
        return None


@pytest.mark.parametrize("reason,lead", [
    ("grounding_failed", "I couldn't confirm that answer against our policies, so I won't guess."),
])
async def test_gate2_hook_removes_the_draft_and_opens_exactly_one_ticket(monkeypatch: pytest.MonkeyPatch,
                                                                         reason: str, lead: str) -> None:
    gate = type("Gate2", (_Gate2,), {"reason": reason})
    monkeypatch.setattr(assembly, "GroundingMiddleware", gate)
    w = World([AIMessage("You get a 50% discount forever.", id="draft-1")])
    outcome, events = await w.send(text="can I get a discount?")
    assert outcome.end_reason == "escalated" and of(events, "token") == []
    assert outcome.final.text.startswith(lead)
    assert_one_code_ticket(w, outcome, events, reason)
    thread = (await w.values())["messages"]
    assert all(m.id != "draft-1" for m in thread) and thread[-2].content == outcome.final.text
    assert str(thread[-1].id).startswith("record-")


async def test_same_tool_failing_twice_opens_one_ticket_and_says_what_did_not_change() -> None:
    w = World([call("list_charges", {})], overrides={("GET", "/v1/me/charges"): lambda r: error_response()})
    await w.seed({"messages": [HumanMessage("hi"), AIMessage("hello", id="a0")], **OWNER})
    outcome, events = await w.send(text="show my charges")
    assert outcome.end_reason == "escalated"
    assert outcome.final.text == ("Our billing system didn't respond, so I couldn't check your charges. I've opened a "
                                  "ticket for our team, ticket ending 8213.")
    assert_one_code_ticket(w, outcome, events, "tool_failure")
    assert ticket_bodies(w)[0][0]["category"] == "billing"


async def test_ticket_create_failure_gives_the_fallback_line_with_the_turn_reference() -> None:
    w = World([call("list_charges", {})], overrides={("GET", "/v1/me/charges"): lambda r: error_response(),
                                                     ("POST", "/v1/tickets"): lambda r: error_response()})
    await w.seed({"messages": [HumanMessage("hi"), AIMessage("hello", id="a0")], **OWNER})
    outcome, events = await w.send(text="show my charges")
    ref = outcome_turn(events).replace("-", "")[-4:]
    assert outcome.final.text == ("Our billing system didn't respond, so I couldn't check your charges. I couldn't "
                                  f"open a ticket just now. If you contact us, please quote reference {ref}.")
    assert len(w.transport.tickets) == 1  # one attempt, no retry on the failure path
    esc = of(events, "escalated")
    assert len(esc) == 1 and esc[0].ticket is None and outcome.final.ticket is None
    done = [e for e in of(events, "step.completed") if e.title == "Couldn't open a ticket"]
    assert len(done) == 1 and done[0].status == "error"


async def test_customer_asks_for_a_human_gives_one_model_ticket_and_no_code_ticket() -> None:
    args = {"category": "human_requested", "summary": "Customer asked for a person about SSO setup.",
            "steps_tried": [], "blocking": False}
    w = World([call("create_ticket", args), AIMessage("I've opened ticket ending 8213 for our team.")])
    await w.seed({"messages": [], **OWNER})  # D-070: the model's create_ticket is offered to verified customers only
    outcome, events = await w.send(text="I want to talk to a human")
    bodies = ticket_bodies(w)
    assert len(bodies) == 1 and bodies[0][0]["origin"] == "model" and bodies[0][1] == f"{SID}:c1"
    esc = of(events, "escalated")
    assert [(e.reason, e.origin) for e in esc] == [("customer_asked_human", "model")]
    assert outcome.end_reason == "answered" and outcome.final.escalated is True
    assert outcome.final.ticket.short_id == "8213"


async def test_verify_lockout_opens_no_ticket_and_no_escalated_event() -> None:
    """D-070: a locked session has no requester, so nobody opens a ticket (core used to, inside verify)."""
    w = World(verified=False)
    for _ in range(2):
        await w.send(verification={"account_id": ex.ACCOUNT_ID, "pin": "0000"})
    outcome, events = await w.send(verification={"account_id": ex.ACCOUNT_ID, "pin": "0000"})
    assert outcome.end_reason == "answered" and outcome.final.ticket is None
    assert of(events, "escalated") == []
    assert w.transport.tickets == []


async def test_model_call_limit_opens_the_limit_reached_ticket_in_code() -> None:
    replies = [call("get_account_summary", {}, f"c{i}") for i in range(6)]
    w = World(replies)
    await w.seed({"messages": [HumanMessage("hi"), AIMessage("hello", id="a0")], **OWNER})
    outcome, events = await w.send(text="keep checking my account")
    assert outcome.end_reason == "limit_reached"
    assert outcome.final.text == ("I couldn't finish that within the steps I'm allowed for one message. I've opened a "
                                  "ticket for our team, ticket ending 8213.")
    assert_one_code_ticket(w, outcome, events, "limit_reached")
    thread = (await w.values())["messages"]
    assert thread[-2].content == outcome.final.text and str(thread[-1].id).startswith("record-")


async def test_both_models_unavailable_apologise_offer_a_ticket_and_create_none() -> None:
    w = World(model=Boom(messages=iter([])), fallback=Boom(messages=iter([])))
    outcome, events = await w.send(text="my SSO login keeps failing")
    assert outcome.end_reason == "escalated" and outcome.final.text == MODEL_UNAVAILABLE_LINE
    assert outcome.final.say == MODEL_UNAVAILABLE_LINE and outcome.final.ticket is None
    esc = of(events, "escalated")
    assert [(e.reason, e.ticket, e.origin, e.offer_ticket_button) for e in esc] == [
        ("model_unavailable", None, "code", True)]
    assert w.transport.tickets == [] and of(events, "error") == []
    thread = (await w.values())["messages"]
    assert thread[-1].content == MODEL_UNAVAILABLE_LINE


async def test_create_ticket_button_after_the_apology_opens_one_ticket_with_no_model_call() -> None:
    w = World(model=Boom(messages=iter([])), fallback=Boom(messages=iter([])))
    await w.send(text="my SSO login keeps failing")
    outcome, events = await w.send(ticket_offer={"accept": True})
    assert outcome.final.text == TICKET_OFFER_OPENED.format(short="8213")
    assert outcome.end_reason == "escalated" and outcome.final.ticket.short_id == "8213"
    [(body, key)] = ticket_bodies(w)
    assert key == f"{SID}:{outcome_turn(events)}:esc:model_unavailable"
    assert (body["origin"], body["category"], body["escalation_reason"]) == ("code", "other", "model_unavailable")
    assert "my SSO login keeps failing" in body["summary"]
    assert [(e.reason, e.origin, e.offer_ticket_button) for e in of(events, "escalated")] == [
        ("model_unavailable", "code", False)]
    assert of(events, "step.started")[-1].kind == "escalation"  # no model step on this turn


async def test_voice_strict_yes_after_the_apology_opens_the_ticket() -> None:
    w = World(model=Boom(messages=iter([])), fallback=Boom(messages=iter([])))
    await w.send(mode="voice", text="my SSO login keeps failing")
    outcome, _ = await w.send(mode="voice", text="yes please")
    assert outcome.final.text == TICKET_OFFER_OPENED.format(short="8213") and len(w.transport.tickets) == 1


async def test_create_ticket_button_without_the_apology_creates_nothing() -> None:
    w = World([AIMessage("Hello!")])
    await w.send(text="hi")
    outcome, _ = await w.send(ticket_offer={"accept": True})
    assert w.transport.tickets == [] and outcome.end_reason == "answered"


async def test_ticket_offer_failure_gives_the_fallback_line() -> None:
    w = World(model=Boom(messages=iter([])), fallback=Boom(messages=iter([])),
              overrides={("POST", "/v1/tickets"): lambda r: error_response()})
    await w.send(text="my SSO login keeps failing")
    outcome, events = await w.send(ticket_offer={"accept": True})
    ref = outcome_turn(events).replace("-", "")[-4:]
    assert outcome.final.text == f"I couldn't open a ticket just now. If you contact us, please quote reference {ref}."
    assert len(w.transport.tickets) == 1 and outcome.final.ticket is None


async def test_commit_failure_ticket_emits_escalated_tool_failure() -> None:
    pending = ex.PENDING_PUBLIC.model_dump(mode="json")
    ctx = {**ex.ME_CONTEXT.model_dump(mode="json"), "pending_action": pending}
    w = World(overrides={("GET", "/v1/me/context"): lambda r: httpx.Response(200, json=ctx),
                         ("POST", "/commit"): lambda r: error_response(changed=False),
                         ("GET", f"/v1/actions/pending/{ex.PENDING_ID}"): lambda r: error_response()})
    await w.seed({"messages": [HumanMessage("refund"), AIMessage("read-back", id="route-t0")], **OWNER})
    outcome, events = await w.send(confirm={"pending_action_id": ex.PENDING_ID, "decision": "confirm"})
    assert outcome.end_reason == "escalated" and len(w.transport.tickets) == 1
    assert [(e.reason, e.origin) for e in of(events, "escalated")] == [("tool_failure", "code")]


class Bug(Fake):
    """What: a model call that hits a programming bug (not a provider error). Use when: the D-058 logging test.
    Inputs: none. Output: raises KeyError. Side effects: none. Role: double."""

    async def _agenerate(self, messages: Any, *args: Any, **kwargs: Any) -> Any:
        """What: raise KeyError. Use when: each call. Inputs: ignored. Output: none. Side effects: raises.
        Role: double."""
        raise KeyError("programming bug")


async def test_unexpected_model_node_exception_is_logged_with_the_turn_id_before_the_apology(
        caplog: pytest.LogCaptureFixture) -> None:
    """D-058: no silent apologies. A bug in the model call is logged (with its traceback and the turn id)."""
    w = World(model=Bug(messages=iter([])), fallback=Bug(messages=iter([])))
    with caplog.at_level(logging.ERROR, logger="agent.turn_body"):
        outcome, events = await w.send(text="my SSO login keeps failing")
    assert outcome.final.text == MODEL_UNAVAILABLE_LINE
    turn = outcome_turn(events)
    recs = [r for r in caplog.records if r.name == "agent.turn_body"]
    assert len(recs) == 1, caplog.records
    assert turn in recs[0].getMessage() and recs[0].exc_info is not None
    assert "programming bug" in caplog.text
