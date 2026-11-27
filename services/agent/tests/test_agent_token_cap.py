"""What: tests for an agent model reply cut at max_tokens (review PR #68 I-1): an empty reply (the cap spent on
thinking) and a cut-text reply are a failed attempt, never `answered` and never sent to Gate 2 as a complete draft;
the doc 07 s8.1 path applies (one retry when the deadline allows, then the fallback, then the s6.7 apology) with a
drawer reason on the model step.
Use when: pytest (fake models through the real create_agent stack; no provider call).
Inputs: none. Output: pass/fail. Side effects: none.
Role: the agent role cannot turn thinking off on claude-sonnet-5-5, so the cap and this check are the guard.
"""
from __future__ import annotations

from typing import Any

import pytest
from langchain_core.messages import AIMessage
from test_deadline_fault import Scripted, ctl, fallback_events, run
from test_gate2 import Harness, verdict_json, verifier

from agent.middleware.deadline_fault import ModelOutputTruncated, fallback_reason, model_retry_on
from agent.models.usage import reply_truncated


def cut(text: str = "", name: str = "claude-sonnet-5-5", key: str = "stop_reason", value: str = "max_tokens"
        ) -> AIMessage:
    """What: a reply stopped at the cap, as the provider reports it. Use when: tests. Inputs: visible text, model
    name, metadata key and value. Output: AIMessage. Side effects: none. Role: fixture."""
    return AIMessage(text, response_metadata={key: value, "model_name": name},
                     usage_metadata={"input_tokens": 3000, "output_tokens": 2048, "total_tokens": 5048,
                                     "output_token_details": {"reasoning": 2048 if not text else 1900}})


def ok(text: str = "Answer.", name: str = "claude-sonnet-5-5") -> AIMessage:
    """What: a complete reply. Use when: tests. Inputs: text, name. Output: AIMessage. Side effects: none.
    Role: fixture."""
    return AIMessage(text, response_metadata={"stop_reason": "end_turn", "model_name": name})


def scripted(name: str, *replies: AIMessage) -> Scripted:
    """What: a Scripted fake that returns the replies in order. Use when: tests. Inputs: name, replies.
    Output: Scripted. Side effects: none. Role: helper."""
    return Scripted(name_=name, errors=[], disable_streaming=True, messages=iter(list(replies)))


def model_steps(stream: Any) -> list[Any]:
    """What: step.completed events of model steps. Use when: asserting the drawer reason. Inputs: stream.
    Output: events. Side effects: none. Role: helper."""
    started = {e.step_id for e in stream.events if e.type == "step.started" and e.kind == "model"}
    return [e for e in stream.events if e.type == "step.completed" and e.step_id in started]


@pytest.mark.parametrize(("meta", "value"), [("stop_reason", "max_tokens"), ("finish_reason", "max_tokens"),
                                             ("finish_reason", "length"), ("normalized_finish_reason", "length")])
def test_reply_truncated_knows_both_providers(meta: str, value: str):
    assert reply_truncated(cut(key=meta, value=value))
    assert not reply_truncated(ok())


@pytest.mark.parametrize("first", [cut(""), cut("Refunds on the Pro plan are allowed wi")], ids=["empty", "cut-text"])
async def test_a_cut_primary_reply_is_retried_and_the_retry_answers(first: AIMessage):
    primary, fallback = scripted("claude-sonnet-5-5", first, ok()), scripted("gpt-5.4-mini", ok(name="gpt-5.4-mini"))
    out, stream = await run(primary, fallback, ctl())
    assert (primary.calls, fallback.calls) == (2, 0)
    assert out.end_reason == "answered" and out.final.text == "Answer."
    [step] = model_steps(stream)
    assert step.detail and "token cap" in step.detail and step.summary.get("truncated") == 1
    assert out.usage.model_calls == 2
    assert out.usage.output_tokens >= 2048  # the discarded attempt's tokens are still counted


async def test_two_cut_primary_replies_go_to_the_fallback():
    primary = scripted("claude-sonnet-5-5", cut(""), cut("Half an ans"))
    fallback = scripted("gpt-5.4-mini", ok("Fallback answer.", name="gpt-5.4-mini"))
    out, stream = await run(primary, fallback, ctl())
    assert (primary.calls, fallback.calls) == (2, 1)
    assert out.final.text == "Fallback answer." and out.fallback_used
    assert [e.reason for e in fallback_events(stream)] == ["5xx"]  # the contract Literal has no truncated value


async def test_when_every_attempt_is_cut_the_turn_is_a_model_failure_never_answered():
    primary = scripted("claude-sonnet-5-5", cut(""), cut(""))
    fallback = scripted("gpt-5.4-mini", cut("Partial", name="gpt-5.4-mini", key="finish_reason", value="length"))
    c = ctl()
    with pytest.raises(ModelOutputTruncated):
        await run(primary, fallback, c)
    # turn_body reads these two flags to pick the doc 07 s6.7 apology (model_unavailable), not an answer.
    assert c.model_node_failed and c.fallback_unfinished


async def test_a_cut_draft_never_reaches_gate2():
    ver = verifier(verdict_json(True))
    search = AIMessage("", tool_calls=[{"name": "search_kb", "args": {"query": "refund window"}, "id": "c1",
                                        "type": "tool_call"}])
    h = Harness(ver, replies=[search, cut("Refunds on the Pro plan are allowed wi"),
                              ok("Refunds on the Pro plan are allowed within 30 days of the charge.")])
    outcome, events, _ = await h.run()
    assert outcome.final.text == "Refunds on the Pro plan are allowed within 30 days of the charge."
    seen = ver.model.seen  # type: ignore[attr-defined]
    assert len(seen) == 1 and "allowed within 30 days of the charge." in str(seen[0][-1].content)


def test_the_truncation_error_is_retried_and_reported_as_a_model_failure():
    err = ModelOutputTruncated("cut")
    assert model_retry_on(err) and fallback_reason(err) == "5xx"
