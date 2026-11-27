"""What: tests for D-064 and D-069 (amend doc 07 s6.4 and D-067): Gate 2 runs the grounding verifier on every
model-written draft; EVERY empty claim list needs a valid no_claims_reason or Gate 2 fails closed, with no regex
in the safety path (needs_grounding and matches_policy are deleted); lines our code writes (route, ingress, gate2,
limit, deadline, unavailable ids) are not model drafts and are never verified.
Use when: pytest (fake chat models, the contracts core stub, a fake knowledge service; no provider, no network).
Inputs: none. Output: pass/fail. Side effects: none.
Role: root-cause audit 2026-11-27 class B, open item 4; review PR #72 I1: no regex decides whether the verifier
runs or whether a lazy empty reply is caught."""
from __future__ import annotations

import json

import pytest
from langchain_core.messages import AIMessage, HumanMessage
from test_gate1 import search_body
from test_gate2 import Harness, grounding_gate, texts, verifier

from agent import gates, line_ids
from agent.gates import CLARIFY_LINE, enforce_code_rules, is_code_line, parse_verifier_reply
from agent.middleware.grounding import GroundingMiddleware
from agent.middleware.router import route_message
from agent.structured.pending import ingress_message_id
from agent.turn_control import TURN_CONTROL, Route, TurnControl, bind_turn_control

# A made-up policy promise in plain words (no digit, money, plan or rule word).
NO_TRIGGER_POLICY = "Good news: Quillstack gives you back everything you spent if you ask soon enough."
NO_CLAIMS = json.dumps({"claims": [], "commitment": False})  # an empty list with no stated reason
CLARIFYING = json.dumps({"claims": [], "commitment": False, "no_claims_reason": "clarifying_question"})
CONVERSATION = json.dumps({"claims": [], "commitment": False, "no_claims_reason": "conversation"})
UNSUPPORTED = json.dumps({"claims": [{"text": "Quillstack gives back everything spent if asked soon",
                                      "supported": False, "source_id": None}], "commitment": False})


def test_the_policy_regex_is_gone_from_the_safety_path():
    for name in ("needs_grounding", "matches_policy", "apply_floor", "_POLICY_RE", "_REASON_CODE_RE"):
        assert not hasattr(gates, name), name


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


async def test_a_no_trigger_draft_with_a_bare_empty_list_fails_closed():
    # Review PR #72 I1: the case that slipped through when only the regex decided whether a reason was needed.
    h = Harness(verifier(NO_CLAIMS), replies=[AIMessage(NO_TRIGGER_POLICY)])
    outcome, events, values = await h.run()
    g = grounding_gate(events)
    assert (g.verdict, g.decision, g.claims_total) == ("fail", "clarify", 0)
    [done] = [e for e in events if e.type == "step.completed" and e.step_id == g.step_id]
    assert done.status == "ok" and done.summary["reason"] == "no_claims_unexplained"
    assert done.detail and "no claim" in done.detail.lower()
    assert outcome.final.text == CLARIFY_LINE and NO_TRIGGER_POLICY not in texts(values)


async def test_plain_conversation_with_a_bare_empty_list_fails_closed_too():
    h = Harness(verifier(NO_CLAIMS), replies=[AIMessage("Hello! How can I help you today?")])
    outcome, events, _ = await h.run()
    assert grounding_gate(events).verdict == "fail" and outcome.final.text == CLARIFY_LINE


async def test_plain_conversation_the_verifier_names_as_conversation_passes():
    ver = verifier(CONVERSATION)
    h = Harness(ver, replies=[AIMessage("Hello! How can I help you today?")])
    outcome, events, _ = await h.run()
    assert len(ver.model.seen) == 1
    g = grounding_gate(events)
    assert (g.verdict, g.decision, g.claims_total) == ("pass", "answer", 0)
    assert outcome.final.text == "Hello! How can I help you today?" and outcome.end_reason == "answered"
    assert outcome.final.confidence is not None and outcome.final.confidence.grounding == "pass"


async def test_a_policy_shaped_draft_with_a_bare_empty_list_fails_closed():
    draft = "Refunds on the Pro plan are allowed within 30 days of the charge."
    h = Harness(verifier(NO_CLAIMS), replies=[AIMessage(draft)])
    outcome, events, values = await h.run()
    assert (grounding_gate(events).verdict, outcome.end_reason) == ("fail", "clarify")
    assert draft not in texts(values)


async def test_a_tool_turn_with_a_bare_empty_list_fails_closed():
    h = Harness(verifier(NO_CLAIMS))  # search_kb then a draft: the turn holds a tool result
    outcome, events, _ = await h.run()
    assert (grounding_gate(events).verdict, grounding_gate(events).decision) == ("fail", "clarify")
    assert outcome.final.text == CLARIFY_LINE


async def test_a_pure_clarifying_question_the_verifier_names_as_such_passes():
    # D-059 kept: naming the options to ask which applies states nothing, and the model says so explicitly.
    q = "Just to confirm, are you on Pro or Business?"
    h = Harness(verifier(CLARIFYING), replies=[AIMessage(q)])
    outcome, events, values = await h.run()
    assert (grounding_gate(events).verdict, outcome.end_reason, outcome.final.text) == ("pass", "answered", q)
    assert values.get("grounding_failures", 0) == 0


async def test_a_tool_turn_acknowledgement_the_verifier_names_as_conversation_passes():
    h = Harness(verifier(CONVERSATION), replies=[
        AIMessage("", tool_calls=[{"name": "search_kb", "args": {"query": "refund window"}, "id": "c1", "type": "tool_call"}]),
        AIMessage("Thanks, I have what I need.")])
    outcome, events, _ = await h.run()
    assert (grounding_gate(events).verdict, outcome.end_reason) == ("pass", "answered")


@pytest.mark.parametrize("reply", [
    NO_CLAIMS,
    json.dumps({"claims": [], "commitment": False, "no_claims_reason": "statement"}),
    json.dumps({"claims": [], "commitment": False, "no_claims_reason": None}),
    json.dumps({"claims": [], "commitment": False, "no_claims_reason": ""}),
    json.dumps({"claims": [], "commitment": False, "no_claims_reason": "Conversation"}),
    json.dumps({"claims": [], "commitment": False, "no_claims_reason": " conversation"}),
    json.dumps({"claims": [], "commitment": False, "no_claims_reason": True}),
    json.dumps({"claims": [], "commitment": False, "no_claims_reason": ["clarifying_question"]}),
    json.dumps({"claims": [], "commitment": False, "no_claims_reason": {"kind": "conversation"}}),
    json.dumps({"claims": [], "commitment": False, "no_claims_reason": "this is a clarifying question"}),
], ids=["missing", "unknown_value", "null", "empty", "wrong_case", "padded", "bool", "list", "dict", "free_text"])
def test_an_empty_list_without_a_known_reason_always_fails_closed(reply: str):
    result = enforce_code_rules(parse_verifier_reply(reply), "", {}, tool_names=set())
    assert (result.verdict, result.reason, result.passed) == ("fail", "no_claims_unexplained", False)


@pytest.mark.parametrize("why", sorted(gates.NO_CLAIMS_REASONS))
def test_an_empty_list_with_a_known_reason_passes_and_a_commitment_still_does_not(why: str):
    ok = json.dumps({"claims": [], "commitment": False, "no_claims_reason": why})
    assert enforce_code_rules(parse_verifier_reply(ok), "", {}, tool_names=set()).passed is True
    promise = json.dumps({"claims": [], "commitment": True, "no_claims_reason": why})
    assert enforce_code_rules(parse_verifier_reply(promise), "", {}, tool_names=set()).passed is False


def test_a_reason_given_beside_claims_is_ignored():
    reply = json.dumps({"claims": [{"text": "x", "supported": False, "source_id": None}], "commitment": False,
                        "no_claims_reason": "conversation"})
    result = parse_verifier_reply(reply)
    assert result.no_claims_reason is None
    assert enforce_code_rules(result, "", {}, tool_names=set()).verdict == "fail"


async def test_a_verifier_error_stays_an_error():
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
    built = [str(route_message(control).id), ingress_message_id("T1")]  # from the writers themselves
    return built + [line_ids.line_id(p, "T1") for p in (line_ids.GATE2, line_ids.LIMIT, line_ids.DEADLINE,
                                                       line_ids.UNAVAILABLE)]


def test_every_prefix_constant_is_a_code_line_prefix_and_no_other_id_can_be_built():
    consts = {v for k, v in vars(line_ids).items() if k.isupper() and isinstance(v, str)}
    assert consts == set(line_ids.CODE_LINE_PREFIXES) and len(consts) == 6
    with pytest.raises(ValueError):
        line_ids.line_id("draft-", "T1")


@pytest.mark.parametrize("msg_id", code_line_ids())
def test_every_code_written_line_id_is_identified(msg_id: str):
    assert is_code_line(AIMessage("fixed", id=msg_id)) is True


@pytest.mark.parametrize("msg_id", [None, "lc_run--0b1c", "run-7f", "msg_01abc", "chatcmpl-9x", "resp_1"])
def test_model_message_ids_are_not_code_lines(msg_id: str | None):
    assert is_code_line(AIMessage("draft", id=msg_id)) is False


@pytest.mark.parametrize("msg_id", code_line_ids())
async def test_a_fixed_line_reaching_the_hook_is_not_verified(msg_id: str):
    # Defence in depth only (review PR #72 M1): the graph never hands the hook a code line (router lines end the run
    # in before_model; the rest are written by aupdate_state). The real-graph case is the router test below.
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


def test_a_reply_that_only_says_no_information_was_found_has_its_own_reason():
    # Review PR #72 I2 (real eval, 19:12 UTC): "I couldn't find that in our help articles." on a tool turn came back
    # as a claim sourced to search_kb and failed closed. D-069: the fix is a reason value, never a regex.
    assert "no_information" in gates.NO_CLAIMS_REASONS
    p = gates.VERIFIER_PROMPT
    assert '"no_information"' in p and "could not find" in p.lower()
    ok = json.dumps({"claims": [], "commitment": False, "no_claims_reason": "no_information"})
    assert enforce_code_rules(parse_verifier_reply(ok), "", {}, tool_names=set()).passed is True
