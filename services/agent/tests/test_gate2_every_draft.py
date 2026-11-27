"""What: tests for D-064 (amends doc 07 s6.4): Gate 2 runs the grounding verifier on every model-written draft;
needs_grounding is only a floor (it fires and the verifier finds zero claims: fail closed); lines our code writes
(route, ingress, gate2, limit, deadline, unavailable ids) are not model drafts and are never verified.
Use when: pytest (fake chat models, the contracts core stub, a fake knowledge service; no provider, no network).
Inputs: none. Output: pass/fail. Side effects: none.
Role: root-cause audit 2026-11-27 class B, open item 4: the last regex in the safety path no longer decides
whether the verifier runs."""
from __future__ import annotations

import json

import pytest
from langchain_core.messages import AIMessage, HumanMessage
from test_gate1 import search_body
from test_gate2 import Harness, grounding_gate, texts, verifier

from agent.escalation import UNAVAILABLE_ID_PREFIX
from agent.gates import CLARIFY_LINE, apply_floor, is_code_line, matches_policy, needs_grounding, parse_verifier_reply
from agent.middleware.grounding import GroundingMiddleware
from agent.middleware.router import route_message
from agent.structured.pending import ingress_message_id
from agent.turn_control import TURN_CONTROL, Route, TurnControl, bind_turn_control

# A made-up policy promise that avoids every word, digit and symbol in the policy-claim pattern.
NO_TRIGGER_POLICY = "Good news: Quillstack gives you back everything you spent if you ask soon enough."
NO_CLAIMS = json.dumps({"claims": [], "commitment": False})  # an empty list with no stated reason
CLARIFYING = json.dumps({"claims": [], "commitment": False, "no_claims_reason": "clarifying_question"})
CONVERSATION = json.dumps({"claims": [], "commitment": False, "no_claims_reason": "conversation"})
UNSUPPORTED = json.dumps({"claims": [{"text": "Quillstack gives back everything spent if asked soon",
                                      "supported": False, "source_id": None}], "commitment": False})


def test_the_no_trigger_draft_really_avoids_the_pattern():
    assert matches_policy(NO_TRIGGER_POLICY) is False
    assert needs_grounding(NO_TRIGGER_POLICY, False) is False


async def test_a_policy_draft_that_avoids_every_trigger_word_is_verified_and_blocked_when_unsupported():
    ver = verifier(UNSUPPORTED)
    h = Harness(ver, replies=[AIMessage(NO_TRIGGER_POLICY)])
    outcome, events, values = await h.run()
    assert len(ver.model.seen) == 1  # before D-064 this draft skipped the verifier
    g = grounding_gate(events)
    assert (g.verdict, g.decision, g.claims_total, g.claims_supported) == ("fail", "clarify", 1, 0)
    assert [e for e in events if e.type == "status" and e.kind == "checking_policy"]
    assert outcome.end_reason == "clarify" and outcome.final.text == CLARIFY_LINE
    assert NO_TRIGGER_POLICY not in texts(values)
    assert outcome.final.confidence is not None and outcome.final.confidence.grounding == "fail"


async def test_plain_conversation_is_verified_too_and_zero_claims_without_the_floor_passes():
    ver = verifier(NO_CLAIMS)
    h = Harness(ver, replies=[AIMessage("Hello! How can I help you today?")])
    outcome, events, _ = await h.run()
    assert len(ver.model.seen) == 1
    g = grounding_gate(events)
    assert (g.verdict, g.decision, g.claims_total) == ("pass", "answer", 0)
    assert outcome.final.text == "Hello! How can I help you today?" and outcome.end_reason == "answered"
    assert outcome.final.confidence is not None and outcome.final.confidence.grounding == "pass"


async def test_floor_a_policy_shaped_draft_with_zero_claims_fails_closed():
    draft = "Refunds on the Pro plan are allowed within 30 days of the charge."
    assert needs_grounding(draft, False) is True
    ver = verifier(NO_CLAIMS)
    h = Harness(ver, replies=[AIMessage(draft)])
    outcome, events, values = await h.run()
    g = grounding_gate(events)
    assert (g.verdict, g.decision, g.claims_total) == ("fail", "clarify", 0)
    [done] = [e for e in events if e.type == "step.completed" and e.step_id == g.step_id]
    assert done.status == "ok" and done.summary["reason"] == "no_claims_floor"
    assert done.detail and "no claim" in done.detail.lower()
    assert outcome.end_reason == "clarify" and outcome.final.text == CLARIFY_LINE
    assert draft not in texts(values)


async def test_floor_a_tool_result_this_turn_with_zero_claims_fails_closed():
    ver = verifier(NO_CLAIMS)
    h = Harness(ver)  # search_kb then a draft: the turn holds a tool result
    outcome, events, _ = await h.run()
    g = grounding_gate(events)
    assert (g.verdict, g.decision) == ("fail", "clarify")
    assert outcome.final.text == CLARIFY_LINE


async def test_floor_a_pure_clarifying_question_the_verifier_names_as_such_passes():
    # D-059 kept: naming the options to ask which applies states nothing, and the model says so explicitly.
    q = "Just to confirm, are you on Pro or Business?"
    assert needs_grounding(q, False) is True
    h = Harness(verifier(CLARIFYING), replies=[AIMessage(q)])
    outcome, events, values = await h.run()
    assert (grounding_gate(events).verdict, outcome.end_reason, outcome.final.text) == ("pass", "answered", q)
    assert values.get("grounding_failures", 0) == 0


async def test_floor_a_tool_turn_acknowledgement_the_verifier_names_as_conversation_passes():
    h = Harness(verifier(CONVERSATION), replies=[
        AIMessage("", tool_calls=[{"name": "search_kb", "args": {"query": "refund window"}, "id": "c1", "type": "tool_call"}]),
        AIMessage("Thanks, I have what I need.")])
    outcome, events, _ = await h.run()
    assert (grounding_gate(events).verdict, outcome.end_reason) == ("pass", "answered")


@pytest.mark.parametrize("reply", [
    NO_CLAIMS,
    json.dumps({"claims": [], "commitment": False, "no_claims_reason": "statement"}),
    json.dumps({"claims": [], "commitment": False, "no_claims_reason": None}),
    json.dumps({"claims": [], "commitment": False, "no_claims_reason": ["clarifying_question"]}),
], ids=["missing", "unknown_value", "null", "wrong_type"])
def test_floor_an_empty_list_without_a_known_reason_fails_closed(reply: str):
    result = apply_floor(parse_verifier_reply(reply), True)
    assert (result.verdict, result.reason, result.passed) == ("fail", "no_claims_floor", False)
    assert apply_floor(parse_verifier_reply(reply), False).passed is True  # off the floor D-059 holds


def test_a_reason_given_beside_claims_is_ignored():
    reply = json.dumps({"claims": [{"text": "x", "supported": False, "source_id": None}], "commitment": False,
                        "no_claims_reason": "conversation"})
    result = parse_verifier_reply(reply)
    assert result.no_claims_reason is None and apply_floor(result, True).verdict == "fail"


async def test_floor_never_turns_a_verifier_error_into_anything_but_an_error():
    h = Harness(verifier("not json"))
    _, events, _ = await h.run()
    g = grounding_gate(events)
    [done] = [e for e in events if e.type == "step.completed" and e.step_id == g.step_id]
    assert g.verdict == "error" and done.summary["reason"] == "unparseable"


# --- lines our code writes are not model drafts ---------------------------------------------------------------
def code_line_ids() -> list[str]:
    """What: the id of every line kind our code writes to the thread, built by the code that writes it.
    Use when: the identification tests. Inputs: none. Output: ids. Side effects: none. Role: fixture."""
    control = TurnControl(session_id="s", turn_id="T1", mode="chat")
    control.set_route(Route("decline_off_topic"))
    return [str(route_message(control).id), ingress_message_id("T1"), "gate2-T1", "limit-T1", "deadline-T1",
            f"{UNAVAILABLE_ID_PREFIX}T1"]


@pytest.mark.parametrize("msg_id", code_line_ids())
def test_every_code_written_line_id_is_identified(msg_id: str):
    assert is_code_line(AIMessage("fixed", id=msg_id)) is True


@pytest.mark.parametrize("msg_id", [None, "lc_run--0b1c", "run-7f", "msg_01abc", "chatcmpl-9x", "resp_1"])
def test_model_message_ids_are_not_code_lines(msg_id: str | None):
    assert is_code_line(AIMessage("draft", id=msg_id)) is False


@pytest.mark.parametrize("msg_id", code_line_ids())
async def test_a_fixed_line_reaching_the_hook_is_not_verified(msg_id: str):
    ver = verifier(NO_CLAIMS)
    mw = GroundingMiddleware(ver)
    control = TurnControl(session_id="s", turn_id="T1", mode="chat")
    state = {"messages": [HumanMessage("hi"), AIMessage("Refunds within 30 days.", id=msg_id)]}
    token = bind_turn_control(control)
    try:
        assert await mw.aafter_model(state, None) is None
    finally:
        TURN_CONTROL.reset(token)
    assert ver.model.seen == [] and control.grounding == "not_run"


async def test_a_router_line_in_a_real_turn_makes_no_verifier_call():
    # Gate 1 weak twice: search_kb sets escalate(not_in_docs); RouterMiddleware writes the route line in
    # before_model and jumps to end, so no model draft follows and the verifier is never called.
    ver = verifier()
    search = AIMessage("", tool_calls=[{"name": "search_kb", "args": {"query": "refund window"}, "id": "c1", "type": "tool_call"}])
    h = Harness(ver, replies=[search], body=search_body("weak", "weak_match"))
    outcome, events, values = await h.run(state={"weak_streak": 1})
    assert outcome.end_reason == "escalated"
    assert not [e for e in events if e.type == "gate" and e.gate == "grounding"]
    assert ver.model.seen == []
    assert is_code_line(values["messages"][-1])
    assert len(h.model.seen) == 1  # the router ended the turn before a second model call
