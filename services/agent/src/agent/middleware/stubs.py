"""What: pass-through placeholder for the custom middleware of doc 07 s2.5 not built yet (Gate 2).
Use when: assembling the agent before the tasks that fill them land; each class names that task in
FILLED_BY. The hook each stub defines is the hook its real version uses, so the graph shape and
the order test stay valid when it is filled in.
Inputs: none.
Output: GroundingMiddleware, STUB_NAMES.
RouterMiddleware, ToolVisibilityMiddleware, ToolGuardMiddleware and DeadlineAndFaultMiddleware are real
(agent.middleware.router, .visibility, .tool_guard, .deadline_fault).
Side effects: none.
Role: slots in the middleware stack. Async hooks only (D-048: the agent runs async only).
"""
from __future__ import annotations

from typing import Any

from langchain.agents.middleware import AgentMiddleware, hook_config


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


STUB_NAMES = frozenset({"GroundingMiddleware"})
