"""Spikes 4 and 16: retry predicate, retry into fallback, fallback never retried (doc 07 s14 rows 4, 16; s8.2).

What: builds the doc 07 s2.5 items 5 to 7 stack (ModelFallback outer, ModelRetry, a
    DeadlineAndFault stand-in innermost) and counts primary and fallback attempts per error class.
Use when: deciding whether the built-in retry and fallback middleware carry the s8.2 rules.
Inputs: fake failing models raising langchain_core ModelError subclasses (the provider-neutral
    types both langchain-anthropic and langchain-openai raise).
Output: pytest results.
Side effects: none (no provider calls).
Role: day-1 spike CS-E-03.S6.T3.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import pytest
from langchain.agents import create_agent
from langchain.agents.middleware import AgentMiddleware, ModelFallbackMiddleware, ModelRetryMiddleware
from langchain_core.exceptions import (
    ModelAPIError,
    ModelError,
    ModelInvalidRequestError,
    ModelRateLimitError,
    ModelTimeoutError,
)
from langchain_core.messages import AIMessage, HumanMessage

from fakes import TURN_CONTROL, FailingModel, TurnControl, scripted


class DeadlineSpike(AgentMiddleware):
    """Stand-in for DeadlineAndFaultMiddleware: labels each attempt primary or fallback.

    What: sets TurnControl.attempt_role from the model on the request, then calls the handler.
    Use when: innermost model wrapper (doc 07 s2.5 item 7, s8.2 "fallback is never retried").
    Inputs: the fallback model instance to recognise.
    Output: the handler's response.
    Side effects: mutates TurnControl.attempt_role; appends the role to TurnControl.events.
    Role: spike harness.
    """

    def __init__(self, fallback_model: Any) -> None:
        """Remember the fallback model.

        What: constructor.
        Use when: building the stack.
        Inputs: fallback model.
        Output: none.
        Side effects: none.
        Role: spike harness.
        """
        super().__init__()
        self.fallback_model = fallback_model

    async def awrap_model_call(self, request: Any, handler: Any) -> Any:
        """Label the attempt, then call the model.

        What: see class.
        Use when: framework wraps each model attempt.
        Inputs: request, handler.
        Output: ModelResponse.
        Side effects: mutates TurnControl.
        Role: spike harness.
        """
        tc = TURN_CONTROL.get()
        tc.attempt_role = "fallback" if request.model is self.fallback_model else "primary"
        tc.events.append(tc.attempt_role)
        return await handler(request)


def retry_on(exc: Exception) -> bool:
    """Deadline-aware retry predicate shape from doc 07 s8.2 (budget check omitted in the spike).

    What: retry only the primary, only on timeout or 5xx; 429, 400, auth go straight to fallback.
    Use when: `ModelRetryMiddleware(retry_on=...)`.
    Inputs: the exception (the only argument the middleware passes).
    Output: True to retry.
    Side effects: none.
    Role: spike harness; the real one adds the remaining-time check.
    """
    if TURN_CONTROL.get().attempt_role == "fallback":
        return False
    return isinstance(exc, (ModelTimeoutError, ModelAPIError, TimeoutError))


def build(primary: Any, fallback: Any, delay: float = 0.0):
    """Assemble the agent with items 5, 6, 7 in doc 07 s2.5 order.

    What: create_agent with ModelFallback, ModelRetry(max_retries=1, on_failure="error"), DeadlineSpike.
    Use when: every case here.
    Inputs: primary model, fallback model, initial retry delay.
    Output: compiled agent.
    Side effects: none.
    Role: spike harness.
    """
    return create_agent(
        primary,
        tools=[],
        middleware=[
            ModelFallbackMiddleware(fallback),
            ModelRetryMiddleware(max_retries=1, retry_on=retry_on, initial_delay=delay, on_failure="error"),
            DeadlineSpike(fallback),
        ],
    )


async def _run(agent) -> tuple[TurnControl, Any]:
    """Bind TurnControl once and invoke; return it and the result or the exception.

    What: mirrors ingress around the agent loop.
    Use when: every case.
    Inputs: agent.
    Output: (TurnControl, result dict or exception).
    Side effects: sets TURN_CONTROL.
    Role: spike harness.
    """
    tc = TurnControl()
    TURN_CONTROL.set(tc)
    try:
        return tc, await agent.ainvoke({"messages": [HumanMessage("hi")]})
    except Exception as exc:  # the exception is the measurement
        return tc, exc


def _err(cls: type[ModelError]) -> ModelError:
    """Instantiate a ModelError subclass.

    What: shorthand.
    Use when: scripting failures.
    Inputs: class.
    Output: exception instance.
    Side effects: none.
    Role: test helper.
    """
    return cls(f"fake {cls.__name__}")


@pytest.mark.parametrize("error_cls", [ModelAPIError, ModelTimeoutError])
async def test_5xx_or_timeout_one_primary_retry_then_one_fallback_call(error_cls):
    calls: list[str] = []
    primary = FailingModel(messages=iter([]), label="primary", calls=calls, error=_err(error_cls))
    fallback = scripted("fallback", AIMessage(content="from fallback"), calls=calls)
    tc, out = await _run(build(primary, fallback))
    assert calls == ["primary", "primary", "fallback"]
    assert tc.events == ["primary", "primary", "fallback"]
    assert out["messages"][-1].content == "from fallback"


@pytest.mark.parametrize("error_cls", [ModelRateLimitError, ModelInvalidRequestError])
async def test_429_or_400_goes_straight_to_fallback_without_retry(error_cls):
    calls: list[str] = []
    primary = FailingModel(messages=iter([]), label="primary", calls=calls, error=_err(error_cls))
    fallback = scripted("fallback", AIMessage(content="from fallback"), calls=calls)
    tc, out = await _run(build(primary, fallback))
    assert calls == ["primary", "fallback"]
    assert out["messages"][-1].content == "from fallback"


async def test_failing_fallback_is_called_once_and_error_reaches_caller():
    """Spike 16: the inner retry sees attempt_role == "fallback" and does not retry."""
    calls: list[str] = []
    primary = FailingModel(messages=iter([]), label="primary", calls=calls, error=_err(ModelAPIError))
    fallback = FailingModel(messages=iter([]), label="fallback", calls=calls, error=_err(ModelTimeoutError))
    tc, out = await _run(build(primary, fallback))
    assert calls == ["primary", "primary", "fallback"]
    assert isinstance(out, ModelTimeoutError)  # on_failure="error" path: the exception surfaces


async def test_negative_control_without_role_check_the_fallback_is_retried():
    """Proves spike 16 can fail: a predicate that ignores attempt_role retries the fallback."""
    calls: list[str] = []
    primary = FailingModel(messages=iter([]), label="primary", calls=calls, error=_err(ModelAPIError))
    fallback = FailingModel(messages=iter([]), label="fallback", calls=calls, error=_err(ModelTimeoutError))
    agent = create_agent(
        primary,
        tools=[],
        middleware=[
            ModelFallbackMiddleware(fallback),
            ModelRetryMiddleware(max_retries=1, retry_on=(ModelTimeoutError, ModelAPIError), initial_delay=0, on_failure="error"),
            DeadlineSpike(fallback),
        ],
    )
    await _run(agent)
    assert calls == ["primary", "primary", "fallback", "fallback"]


async def test_default_retry_on_would_retry_429():
    """Records why the custom predicate is required: the default retries rate limits."""
    from langchain.agents.middleware._retry import default_retry_on

    assert default_retry_on(_err(ModelRateLimitError)) is True
    assert default_retry_on(_err(ModelInvalidRequestError)) is False


async def test_hanging_fallback_one_call_under_deadline():
    """doc 07 s8.2 unit test: a hanging fallback gets one call; the turn deadline ends it."""
    calls: list[str] = []

    class Hanging(FailingModel):
        """Fallback that hangs (spike).

        What: sleeps far past the deadline.
        Use when: this test.
        Inputs: none.
        Output: never.
        Side effects: appends label to calls.
        Role: spike harness.
        """

        async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs):  # type: ignore[override]
            """Hang.

            What: simulates a provider that never answers.
            Use when: framework calls the model.
            Inputs: ignored.
            Output: never.
            Side effects: appends label.
            Role: spike harness.
            """
            self.calls.append(self.label)
            await asyncio.sleep(30)

    primary = FailingModel(messages=iter([]), label="primary", calls=calls, error=_err(ModelRateLimitError))
    fallback = Hanging(messages=iter([]), label="fallback", calls=calls)
    tc = TurnControl()
    TURN_CONTROL.set(tc)
    t0 = time.monotonic()
    with pytest.raises(TimeoutError):
        async with asyncio.timeout(0.5):
            await build(primary, fallback).ainvoke({"messages": [HumanMessage("hi")]})
    assert calls == ["primary", "fallback"]
    assert time.monotonic() - t0 < 2


async def test_backoff_is_real_with_contract_delay():
    """With initial_delay=0.5 (doc 07 s2.5 item 6) the retry waits about 0.375 to 0.625 s (jitter 25 percent)."""
    calls: list[str] = []
    primary = FailingModel(messages=iter([]), label="primary", calls=calls, error=_err(ModelAPIError))
    fallback = scripted("fallback", AIMessage(content="ok"), calls=calls)
    t0 = time.monotonic()
    await _run(build(primary, fallback, delay=0.5))
    elapsed = time.monotonic() - t0
    print(f"spike4 backoff elapsed {elapsed:.3f}s")
    assert 0.3 < elapsed < 1.5
