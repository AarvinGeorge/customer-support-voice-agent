"""What: tests for DeadlineAndFaultMiddleware, the deadline-aware retry predicate, the shared clamp helper, the turn
deadline binding and the request-scoped demo fault (doc 07 s2.5 items 5 to 7, s8.1, s8.2, s10; CS-E-07.S1.T2).
Use when: pytest (fake models through the real create_agent stack; no provider call).
Inputs: none. Output: pass/fail. Side effects: none.
Role: Done when: a primary 429 goes to the fallback with no retry; flag false makes the header a no-op; flag true
shows model.fallback reason="fault_llm_429"."""
from __future__ import annotations

import asyncio
import logging
import time
import uuid
from typing import Any

import pytest
from langchain_core.exceptions import ModelAPIError, ModelRateLimitError, ModelTimeoutError
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.checkpoint.memory import InMemorySaver

from agent.assembly import build_agent, turn_run_config
from agent.deadline import TurnBudgetExhausted, clamp_timeout, retry_fits
from agent.ingress import Ingress, IngressInput, IngressStream, SessionRow, TurnRow
from agent.middleware.deadline_fault import (
    DeadlineAndFaultMiddleware,
    demo_fault_from_header,
    fallback_reason,
    model_retry_on,
    rate_limit_error_for,
)
from agent.sse import TurnMeta, TurnOutcome, TurnStream, stream_agent_turn
from agent.turn_control import TurnControl, bind_turn_control, current_control, TURN_CONTROL
from ingress_fakes import MemoryTurnLock, MemoryTurnStore

SID, VISITOR = "019a7c2e-5b10-7000-8000-00000000a001", "visitor-1"
TID = "019a7c2e-5b20-7000-8000-0000000000c1"


class Scripted(GenericFakeChatModel):
    """What: fake model that raises the next scripted error (or hangs) before answering, and counts calls.
    Use when: these tests. Inputs: name, errors (list of exceptions or "hang"), messages. Output: reply.
    Side effects: records calls. Role: test double for a provider client."""

    name_: str = "fake"
    errors: list[Any] = []
    calls: int = 0

    def bind_tools(self, tools: Any, **kw: Any) -> "Scripted":
        """What: no-op. Use when: create_agent binds tools. Inputs: tools. Output: self. Side effects: none.
        Role: fake."""
        return self

    async def _agenerate(self, messages, stop=None, run_manager=None, **kw):  # type: ignore[override]
        """What: raise or hang per script, else reply. Use when: model call. Inputs: messages.
        Output: ChatResult. Side effects: counts. Role: fake."""
        self.calls += 1
        if self.errors:
            e = self.errors.pop(0)
            if e == "hang":
                await asyncio.sleep(30)
            assert isinstance(e, BaseException)
            raise e
        return self._generate(messages, stop=stop, run_manager=None, **kw)


def model(name: str, *errors: Any, reply: str = "Answer.") -> Scripted:
    """What: a scripted fake. Use when: tests. Inputs: model name, errors, reply. Output: Scripted.
    Side effects: none. Role: helper."""
    return Scripted(name_=name, errors=list(errors), disable_streaming=True,
                    messages=iter([AIMessage(reply, response_metadata={"model_name": name})]))


async def run(primary: Scripted, fallback: Scripted, control: TurnControl, **mw: Any) -> tuple[TurnOutcome, TurnStream]:
    """What: one turn through the real stack with the control bound. Use when: tests.
    Inputs: models, control, middleware overrides. Output: (outcome, stream). Side effects: model calls (fakes).
    Role: helper."""
    stream = TurnStream(TurnMeta(SID, TID, control.mode, "agent_a", "text"))
    control.events = stream
    agent = build_agent(primary, fallback_model=fallback, checkpointer=InMemorySaver(), deadline_kwargs=mw)
    token = bind_turn_control(control)
    try:
        out = await stream_agent_turn(stream, agent, {"messages": [HumanMessage("hi")]}, turn_run_config(SID, TID),
                                      control=control)
    finally:
        TURN_CONTROL.reset(token)
    return out, stream


def fallback_events(stream: TurnStream) -> list[Any]:
    """What: model.fallback events of a stream. Use when: tests. Inputs: stream. Output: events.
    Side effects: none. Role: helper."""
    return [e for e in stream.events if getattr(e, "type", None) == "model.fallback"]


def ctl(**kw: Any) -> TurnControl:
    """What: a TurnControl for the fixed turn. Use when: tests. Inputs: overrides. Output: TurnControl.
    Side effects: none. Role: helper."""
    return TurnControl(session_id=SID, turn_id=TID, **kw)


# --- Done when 1: a primary 429 goes to the fallback with no retry -------------------------------------------
async def test_primary_429_goes_straight_to_the_fallback_with_no_retry():
    primary, fallback = model("claude-sonnet-5-5", ModelRateLimitError("429")), model("gpt-5.4-mini")
    c = ctl()
    out, stream = await run(primary, fallback, c)
    assert (primary.calls, fallback.calls) == (1, 1)
    ev = fallback_events(stream)
    assert [(e.from_model, e.to_model, e.reason) for e in ev] == [("claude-sonnet-5-5", "gpt-5.4-mini", "rate_limit")]
    assert out.final.text == "Answer." and out.usage.fallback_used and out.fallback_used
    assert out.usage.model_calls == 2  # provider attempts, as LangSmith counts llm runs


async def test_primary_5xx_is_retried_once_then_the_fallback_answers():
    primary, fallback = model("p", ModelAPIError("500"), ModelAPIError("500")), model("f")
    out, stream = await run(primary, fallback, ctl())
    assert (primary.calls, fallback.calls) == (2, 1)
    assert [e.reason for e in fallback_events(stream)] == ["5xx"]
    assert out.usage.model_calls == 3


async def test_a_failing_fallback_is_called_once_and_the_error_reaches_the_caller():
    primary, fallback = model("p", ModelAPIError("500"), ModelAPIError("500")), model("f", ModelTimeoutError("t"))
    with pytest.raises(ModelTimeoutError):
        await run(primary, fallback, ctl())
    assert (primary.calls, fallback.calls) == (2, 1)


async def test_an_attempt_longer_than_its_clamp_times_out_and_counts_as_a_timeout():
    primary, fallback = model("p", "hang", "hang"), model("f")
    out, stream = await run(primary, fallback, ctl(), attempt_timeout_s=0.05, min_attempt_s=0.01)
    assert (primary.calls, fallback.calls) == (2, 1)
    assert [e.reason for e in fallback_events(stream)] == ["timeout"]


# --- Done when 2 and 3: the demo fault -----------------------------------------------------------------------
def test_flag_false_makes_the_header_a_no_op_and_logs_it(caplog):
    caplog.set_level(logging.INFO, logger="agent.fault")
    assert demo_fault_from_header("llm_429", enabled=False) is None
    assert demo_fault_from_header("tool_error", enabled=False) is None
    assert any("fault_header_ignored" in r.getMessage() for r in caplog.records)
    assert demo_fault_from_header(None, enabled=False) is None


def test_flag_true_accepts_only_the_two_contract_values():
    assert demo_fault_from_header("llm_429", enabled=True) == "llm_429"
    assert demo_fault_from_header(" tool_error ", enabled=True) == "tool_error"
    assert demo_fault_from_header("drop_tables", enabled=True) is None


async def test_no_fault_on_the_control_calls_the_primary():
    primary, fallback = model("p"), model("f")
    out, stream = await run(primary, fallback, ctl(demo_fault=demo_fault_from_header("llm_429", enabled=False)))
    assert (primary.calls, fallback.calls) == (1, 0) and not fallback_events(stream)
    assert out.usage.fallback_used is False


async def test_flag_true_llm_429_fails_the_primary_only_and_shows_fault_llm_429():
    primary, fallback = model("claude-sonnet-5-5"), model("gpt-5.4-mini")
    c = ctl(demo_fault=demo_fault_from_header("llm_429", enabled=True))
    out, stream = await run(primary, fallback, c)
    assert primary.calls == 0, "the injected 429 is raised before the provider is called"
    assert fallback.calls == 1 and out.final.text == "Answer."
    assert [e.reason for e in fallback_events(stream)] == ["fault_llm_429"]
    assert [(e.from_model, e.to_model) for e in fallback_events(stream)] == [("claude-sonnet-5-5", "gpt-5.4-mini")]
    assert c.fault_fired and out.usage.model_calls == 1 and out.usage.fallback_used


def test_the_injected_429_is_the_providers_own_rate_limit_class():
    import anthropic
    import openai
    from langchain_anthropic import ChatAnthropic
    from langchain_openai import ChatOpenAI
    a = rate_limit_error_for(ChatAnthropic(model="claude-sonnet-5-5", api_key="x"))  # type: ignore[call-arg]
    o = rate_limit_error_for(ChatOpenAI(model="gpt-5.4-mini", api_key="x"))  # type: ignore[arg-type]
    assert isinstance(a, anthropic.RateLimitError) and isinstance(a, ModelRateLimitError)
    assert isinstance(o, openai.RateLimitError) and isinstance(o, ModelRateLimitError)
    assert getattr(a, "fault_injected") is True
    assert fallback_reason(a) == "fault_llm_429" and fallback_reason(ModelRateLimitError("x")) == "rate_limit"


def test_a_fault_turn_carries_the_langsmith_tag_and_metadata():
    cfg = turn_run_config(SID, TID, demo_fault="llm_429")
    assert "demo_fault" in cfg["tags"] and cfg["metadata"]["fault_injected"] is True
    plain = turn_run_config(SID, TID)
    assert "tags" not in plain and "metadata" not in plain


# --- retry predicate and clamp -------------------------------------------------------------------------------
def test_retry_predicate_rules():
    c = ctl()
    token = bind_turn_control(c)
    try:
        assert model_retry_on(ModelAPIError("500")) and model_retry_on(ModelTimeoutError("t"))
        assert not model_retry_on(ModelRateLimitError("429"))
        assert not model_retry_on(ValueError("400-ish"))
        assert not model_retry_on(TurnBudgetExhausted("skip"))
        c.attempt_role = "fallback"
        assert not model_retry_on(ModelAPIError("500")), "the fallback is never retried"
        c.attempt_role = "primary"
        c.deadline = time.monotonic() + 3 + 0.5 + 10 + 6 + 0.2  # fits retry 10 s plus 6 s left for a fallback
        assert model_retry_on(ModelAPIError("500"))
        c.deadline = time.monotonic() + 5.6  # voice row of doc 07 s8.3: retry skipped
        assert not model_retry_on(ModelAPIError("500"))
    finally:
        TURN_CONTROL.reset(token)


def test_shared_clamp_helper_matches_doc07_s8_2():
    assert clamp_timeout(10, 30.1, minimum_s=2) == 10
    assert clamp_timeout(10, 10.1, minimum_s=2) == pytest.approx(7.1)
    assert clamp_timeout(10, 4.9, minimum_s=2) is None
    assert retry_fits(3, 6.6, 0.5) and not retry_fits(3, 6.4, 0.5)


async def test_a_clamp_below_two_seconds_skips_both_models_and_raises_the_deadline_signal():
    primary, fallback = model("p"), model("f")
    with pytest.raises(TurnBudgetExhausted):
        await run(primary, fallback, ctl(deadline=time.monotonic() + 4.5))
    assert (primary.calls, fallback.calls) == (0, 0)


async def test_voice_primary_hang_case_clamps_the_fallback_to_what_is_left():
    seen: list[float | None] = []
    mw = DeadlineAndFaultMiddleware(fallback_models=())
    c = ctl(deadline=time.monotonic() + 5.6)
    token = bind_turn_control(c)
    try:
        seen.append(mw.attempt_timeout(c))
    finally:
        TURN_CONTROL.reset(token)
    assert seen[0] == pytest.approx(2.6, abs=0.05)


# --- turn deadline binding in ingress ------------------------------------------------------------------------
@pytest.mark.parametrize("mode,budget", [("chat", 45), ("voice", 20)])
async def test_ingress_binds_one_turn_control_with_the_mode_deadline_and_the_fault(mode, budget):
    seen: dict[str, Any] = {}

    async def body(stream: TurnStream, inp: IngressInput, row: TurnRow) -> TurnOutcome:
        c = current_control()
        assert c is not None
        seen.update(remaining=c.remaining_s(), fault=c.demo_fault, turn=c.turn_id, mode=c.mode)
        return TurnOutcome.fixed("ok", end_reason="answered")

    store = MemoryTurnStore(SessionRow(session_id=SID, visitor_id=VISITOR))
    ing = Ingress(store, MemoryTurnLock(), served_by="agent_a", run_agent=body)
    res = await ing.handle(IngressInput(session_id=SID, visitor_id=VISITOR, client_turn_id=str(uuid.uuid4()), mode=mode,
                                        input_kind="voice" if mode == "voice" else "text", text="hi",
                                        demo_fault="llm_429"))
    assert isinstance(res, IngressStream) and res.task is not None
    await res.task
    assert budget - 1 < seen["remaining"] <= budget
    assert seen["fault"] == "llm_429" and seen["turn"] == res.stream.meta.turn_id and seen["mode"] == mode


async def test_the_skip_signal_takes_the_ingress_deadline_path():
    async def body(stream: TurnStream, inp: IngressInput, row: TurnRow) -> TurnOutcome:
        raise TurnBudgetExhausted("model attempt below 2 s")

    store = MemoryTurnStore(SessionRow(session_id=SID, visitor_id=VISITOR))
    ing = Ingress(store, MemoryTurnLock(), served_by="agent_a", run_agent=body)
    res = await ing.handle(IngressInput(session_id=SID, visitor_id=VISITOR, client_turn_id=str(uuid.uuid4()),
                                        mode="chat", input_kind="text", text="hi"))
    assert isinstance(res, IngressStream) and res.task is not None
    out = await res.task
    assert (out.end_reason, out.status) == ("deadline", "failed")
    assert out.error is not None and out.error.class_ == "turn_deadline_exceeded"
