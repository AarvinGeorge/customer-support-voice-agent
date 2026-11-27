"""What: DeadlineAndFaultMiddleware (doc 07 s2.5 item 7, innermost model wrapper), the deadline-aware retry
predicate for ModelRetryMiddleware (item 6), the model.fallback reason mapping, and the request-scoped demo fault
gate (s10).
Use when: assembly builds the stack (ModelFallbackMiddleware outer, ModelRetryMiddleware, then this); the turn
route reads X-Demo-Fault through demo_fault_from_header and puts the result on IngressInput.demo_fault.
Inputs: the model request and handler; TurnControl bound for the turn (deadline, demo_fault, events).
Output: DeadlineAndFaultMiddleware, model_retry_on, fallback_reason, rate_limit_error_for, demo_fault_from_header,
fault_run_metadata, FAULT_VALUES.
Side effects: mutates TurnControl (attempt_role, model_attempts, primary_error, fallback_used, fault_fired); emits
model.fallback; logs fault_header_ignored.
Role: every model attempt is clamped to min(10 s, remaining - 3 s) and skipped below 2 s; the fallback is labelled
so the predicate never retries it (spike 16); a 429, spend cap, auth or 400 is not retried, so it goes straight to
the fallback (spike 4); llm_429 raises the provider's own rate-limit class on the primary only, before any request.
First-token timeout: not enforced (spike 5 dropped by doc 09 s3; the contract fallback is the total 10 s only).
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Callable, Sequence

import httpx
from contracts import limits
from langchain.agents.middleware import AgentMiddleware
from langchain_core.exceptions import (
    ModelAPIError,
    ModelAuthenticationError,
    ModelConnectionError,
    ModelPermissionDeniedError,
    ModelRateLimitError,
    ModelTimeoutError,
)

from agent.deadline import TurnBudgetExhausted, clamp_timeout, retry_fits
from agent.turn_control import TurnControl, current_control

log = logging.getLogger("agent.fault")
FAULT_VALUES = frozenset({"tool_error", "llm_429"})
FAULT_MESSAGE = "Injected demo fault: rate limit (llm_429)"


class ModelAttemptTimeout(ModelTimeoutError):
    """What: a model attempt ran past its clamped timeout. Use when: raised by DeadlineAndFaultMiddleware.
    Inputs: message. Output: exception. Side effects: none. Role: a timeout to the retry predicate and reasons."""


def demo_fault_from_header(value: str | None, *, enabled: bool) -> str | None:
    """What: the request's demo fault after the DEMO_FAULTS_ENABLED gate (doc 07 s10 Gate and Scope).
    Use when: the /turns and /voice/ask routes, once per request, before IngressInput is built.
    Inputs: the X-Demo-Fault header value (or None); the agent's flag.
    Output: "tool_error", "llm_429" or None. Side effects: logs fault_header_ignored when a header arrives with
    the flag off. Role: the only gate; nothing is stored (request-scoped via TurnControl)."""
    if value is None or not value.strip():
        return None
    v = value.strip()
    if not enabled:
        log.info("fault_header_ignored value=%s", v if v in FAULT_VALUES else "other")
        return None
    return v if v in FAULT_VALUES else None


def fault_run_metadata(demo_fault: str | None) -> dict[str, Any]:
    """What: LangSmith tags and metadata for a faulted turn (doc 07 s10 last bullet).
    Use when: turn_run_config. Inputs: the gated fault or None. Output: {} or {"tags": ["demo_fault"],
    "metadata": {"fault_injected": True, "demo_fault": value}}. Side effects: none. Role: fault tag."""
    if demo_fault not in FAULT_VALUES:
        return {}
    return {"tags": ["demo_fault"], "metadata": {"fault_injected": True, "demo_fault": demo_fault}}


def _fake_response(status: int) -> Any:
    """What: a synthetic provider response for an injected error. Use when: rate_limit_error_for.
    Inputs: status. Output: httpx.Response (typed Any: the SDKs annotate their own httpx alias).
    Side effects: none. Role: helper (no request is sent)."""
    return httpx.Response(status, request=httpx.Request("POST", "https://fault.invalid/v1/messages"))


def rate_limit_error_for(model: Any) -> Exception:
    """What: the provider's own rate-limit exception for a model client, marked fault_injected.
    Use when: the llm_429 fault on a primary attempt. Inputs: the chat model of the request.
    Output: AnthropicRateLimitError, OpenAIRateLimitError (both also ModelRateLimitError) or a plain
    ModelRateLimitError for other clients. Side effects: none. Role: doc 07 s10 llm_429."""
    err: Exception
    cls = type(model).__name__
    if cls == "ChatAnthropic":
        from langchain_anthropic.chat_models import AnthropicRateLimitError
        err = AnthropicRateLimitError(FAULT_MESSAGE, response=_fake_response(429), body=None)
    elif cls in ("ChatOpenAI", "AzureChatOpenAI"):
        from langchain_openai.chat_models.base import OpenAIRateLimitError
        err = OpenAIRateLimitError(FAULT_MESSAGE, response=_fake_response(429), body=None)
    else:
        err = ModelRateLimitError(FAULT_MESSAGE)
    setattr(err, "fault_injected", True)
    return err


def _is_spend_cap(exc: BaseException) -> bool:
    """What: a provider budget refusal (OpenAI insufficient_quota, Anthropic credit balance). Use when: reasons.
    Inputs: exception. Output: bool. Side effects: none. Role: helper."""
    text = f"{getattr(exc, 'code', '')} {getattr(exc, 'body', '')} {exc}".lower()
    return "insufficient_quota" in text or "credit balance" in text


def fallback_reason(exc: BaseException | None) -> str:
    """What: the model.fallback reason for the primary's failure (doc 07 s6.2 Literal).
    Use when: the fallback attempt starts. Inputs: the primary's last exception (None: unknown).
    Output: fault_llm_429, spend_cap, rate_limit, timeout, auth or 5xx (other errors, for example a 400, are
    reported as 5xx: the Literal has no client-error value). Side effects: none. Role: badge reason."""
    if exc is None:
        return "5xx"
    if getattr(exc, "fault_injected", False):
        return "fault_llm_429"
    if _is_spend_cap(exc):
        return "spend_cap"
    if isinstance(exc, ModelRateLimitError):
        return "rate_limit"
    if isinstance(exc, (ModelTimeoutError, TimeoutError, asyncio.TimeoutError)):
        return "timeout"
    if isinstance(exc, (ModelAuthenticationError, ModelPermissionDeniedError)):
        return "auth"
    return "5xx"


def model_retry_on(exc: Exception) -> bool:
    """What: the deadline-aware retry predicate (doc 07 s8.1 row "Model attempt", s8.2).
    Use when: ModelRetryMiddleware(retry_on=model_retry_on). Inputs: the failed attempt's exception.
    Output: True only for a primary attempt that timed out or got a 5xx or connection error, when after the
    0.5 s backoff the remaining time minus the 3 s reserve still fits a full 10 s retry plus 6 s for the fallback.
    Never for the fallback, a 429, spend cap, auth, a 400 or a skipped call. Side effects: none.
    Role: retry slot of the stack."""
    control = current_control()
    if control is not None and control.attempt_role == "fallback":
        return False
    if isinstance(exc, TurnBudgetExhausted) or getattr(exc, "fault_injected", False) or _is_spend_cap(exc):
        return False
    if not isinstance(exc, (ModelTimeoutError, ModelAPIError, ModelConnectionError, TimeoutError)):
        return False
    remaining = control.remaining_s() if control is not None else float("inf")
    return retry_fits(limits.MODEL_ATTEMPT_TIMEOUT_S, remaining, limits.MODEL_RETRY_BACKOFF_S,
                      extra_s=limits.MODEL_FIRST_TOKEN_TIMEOUT_S)


def model_name_of(model: Any) -> str:
    """What: a display id for a chat model client. Use when: model.fallback fields. Inputs: model.
    Output: its model id (model or model_name attribute) or class name. Side effects: none. Role: helper."""
    for attr in ("model", "model_name", "name_"):
        v = getattr(model, attr, None)
        if isinstance(v, str) and v:
            return v
    return type(model).__name__


class DeadlineAndFaultMiddleware(AgentMiddleware):
    """What: innermost model wrapper: label the attempt primary or fallback, raise the llm_429 fault on the primary,
    clamp the attempt to the turn deadline (skip below the minimum), emit model.fallback when the fallback runs,
    and count provider attempts.
    Use when: doc 07 s2.5 item 7, inside ModelRetryMiddleware. Inputs: fallback model instances (to recognise
    them), attempt timeout (10 s), minimum attempt (2 s), reserve (3 s); overridable for tests.
    Output: the handler's response. Side effects: TurnControl fields; one model.fallback per fallback attempt.
    Role: deadline and fault layer for model calls (spikes 4 and 16)."""

    def __init__(self, fallback_models: Sequence[Any] = (), *,
                 attempt_timeout_s: float = limits.MODEL_ATTEMPT_TIMEOUT_S,
                 min_attempt_s: float = limits.MODEL_MIN_ATTEMPT_S,
                 reserve_s: float = limits.ESCALATION_RESERVE_S) -> None:
        """What: configure. Use when: build_middleware. Inputs: see class. Output: none. Side effects: none.
        Role: constructor."""
        super().__init__()
        self.fallback_models = tuple(fallback_models)
        self.attempt_timeout_s, self.min_attempt_s, self.reserve_s = attempt_timeout_s, min_attempt_s, reserve_s

    def attempt_timeout(self, control: TurnControl | None) -> float | None:
        """What: this attempt's clamped timeout. Use when: before each attempt. Inputs: control (None: no deadline).
        Output: seconds, or None (skip). Side effects: none. Role: doc 07 s8.2 clamp for model attempts."""
        remaining = control.remaining_s() if control is not None else float("inf")
        return clamp_timeout(self.attempt_timeout_s, remaining, reserve_s=self.reserve_s, minimum_s=self.min_attempt_s)

    async def awrap_model_call(self, request: Any, handler: Callable[[Any], Awaitable[Any]]) -> Any:
        """What: one model attempt under the rules in the class docstring. Use when: every model attempt.
        Inputs: request (request.model is the primary or a fallback), handler.
        Output: the model response. Side effects: see class; raises the injected fault, TurnBudgetExhausted
        (skip) or ModelAttemptTimeout. Role: wrap_model_call hook."""
        control = current_control()
        fallback = any(request.model is m for m in self.fallback_models)
        if control is None:  # outside a turn (unit use): plain call with the listed timeout
            return await asyncio.wait_for(handler(request), self.attempt_timeout_s)
        control.attempt_role = "fallback" if fallback else "primary"
        if not fallback:
            control.primary_model = model_name_of(request.model)  # before the fault, so model.fallback names it
        if not fallback and control.demo_fault == "llm_429":
            control.fault_fired = True
            err = rate_limit_error_for(request.model)
            control.primary_error = err
            raise err
        timeout = self.attempt_timeout(control)
        if timeout is None:
            raise TurnBudgetExhausted(f"{control.attempt_role} model attempt skipped: under "
                                      f"{self.min_attempt_s} s left after the reserve")
        if fallback:
            control.fallback_used = True
            if control.events is not None:
                control.events.emit("model.fallback", from_model=control.primary_model or "unknown",
                                    to_model=model_name_of(request.model), reason=fallback_reason(control.primary_error))
        control.model_attempts += 1
        try:
            return await asyncio.wait_for(handler(request), timeout)
        except (TimeoutError, asyncio.TimeoutError) as exc:
            err = ModelAttemptTimeout(f"model attempt exceeded {timeout:.1f} s")
            if not fallback:
                control.primary_error = err
            raise err from exc
        except Exception as exc:
            if not fallback:
                control.primary_error = exc
            raise
