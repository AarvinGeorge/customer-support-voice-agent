"""What: pass-through placeholders for the custom middlewares of doc 07 s2.5, plus the retry predicate.
Use when: assembling the agent before the tasks that fill them land; each class names that task in
FILLED_BY. The hook each stub defines is the hook its real version uses, so the graph shape and
the order test stay valid when it is filled in.
Inputs: none.
Output: DeadlineAndFaultMiddleware, GroundingMiddleware, retry_on_placeholder, STUB_NAMES.
RouterMiddleware, ToolVisibilityMiddleware and ToolGuardMiddleware are real (agent.middleware.router,
.visibility, .tool_guard).
Side effects: none.
Role: slots in the middleware stack. Async hooks only (D-048: the agent runs async only).
"""
from __future__ import annotations

from typing import Any, Awaitable, Callable

from langchain.agents.middleware import AgentMiddleware, hook_config


class DeadlineAndFaultMiddleware(AgentMiddleware):
    """What: PLACEHOLDER for doc 07 s2.5 item 7 (innermost model wrapper). Filled by CS-E-07.S1.T2:
    attempt clamp (s8.2), 6 s to first token, attempt_role on TurnControl, llm_429 fault on the primary.
    Use when: inside ModelRetryMiddleware.
    Inputs: model request, handler.
    Output: the handler's response unchanged until filled.
    Side effects: none.
    Role: deadline and fault slot.
    """

    FILLED_BY = "CS-E-07.S1.T2"

    async def awrap_model_call(self, request: Any, handler: Callable[[Any], Awaitable[Any]]) -> Any:
        """What: pass-through. Use when: each model attempt. Inputs: request, handler.
        Output: handler(request). Side effects: none. Role: placeholder."""
        return await handler(request)


class GroundingMiddleware(AgentMiddleware):
    """What: PLACEHOLDER for doc 07 s2.5 item 9 (after_model, can_jump_to end). Filled by CS-E-02.S4.T2:
    Gate 2 grounding (s6.4), RemoveMessage on a failed draft, fixed clarifying line.
    Use when: after each model call (after_model hooks run in reverse list order, so this runs first).
    Inputs: state, runtime.
    Output: None (pass-through) until filled.
    Side effects: none.
    Role: Gate 2 slot.
    """

    FILLED_BY = "CS-E-02.S4.T2"

    @hook_config(can_jump_to=["end"])
    async def aafter_model(self, state: Any, runtime: Any) -> dict[str, Any] | None:
        """What: pass-through. Use when: after each model call. Inputs: state, runtime.
        Output: None. Side effects: none. Role: placeholder."""
        return None


def retry_on_placeholder(exc: BaseException) -> bool:
    """What: PLACEHOLDER retry predicate for ModelRetryMiddleware. Filled by CS-E-07.S1.T2 with the
    deadline-aware predicate (s8.2: timeout or 5xx on the primary only, read from TurnControl).
    Use when: ModelRetryMiddleware(retry_on=...).
    Inputs: the exception of a failed attempt.
    Output: always False, so no attempt is retried yet (never the fallback: spike 16).
    Side effects: none.
    Role: retry predicate slot.
    """
    return False


retry_on_placeholder.FILLED_BY = "CS-E-07.S1.T2"  # type: ignore[attr-defined]

STUB_NAMES = frozenset({"DeadlineAndFaultMiddleware", "GroundingMiddleware"})
