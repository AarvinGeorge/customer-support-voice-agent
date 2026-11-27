"""What: ToolVisibilityMiddleware and the visibility rule (doc 07 s5.2): V0 unverified, V1 verified member,
V2 verified owner or billing_admin, chosen in code before every model call.
Use when: the create_agent stack (item 4, wrap_model_call); ToolGuard reuses visible_tool_names to block a
call to a tool the model was not shown.
Inputs: the agent state (verified, claims_snapshot.role).
Output: visibility_set, visible_tool_names, ToolVisibilityMiddleware.
Side effects: none.
Role: least privilege per turn. The sets come from contracts.tools.VISIBILITY, which is doc 07 s5.2 minus the
designed-only tools (billing email, grace extension, preferences: doc 09 s1.2), so the grace tool is hidden
even if a tool with that name were registered.
"""
from __future__ import annotations

from typing import Any, Awaitable, Callable

from contracts.tools import VISIBILITY
from langchain.agents.middleware import AgentMiddleware

BILLING_ROLES = frozenset({"owner", "billing_admin"})


def _get(state: Any, key: str) -> Any:
    """What: read a key from a dict-like or attribute state. Use when: reading agent state.
    Inputs: state, key. Output: value or None. Side effects: none. Role: helper."""
    if isinstance(state, dict):
        return state.get(key)
    return getattr(state, key, None)


def visibility_set(state: Any) -> str:
    """What: the visibility set name for this state. Use when: before a model call and in ToolGuard.
    Inputs: agent state. Output: "V0", "V1" or "V2". Side effects: none.
    Role: doc 07 s5.2 "When" column (a verified state with an unknown role gets V1, the smaller set)."""
    if not _get(state, "verified"):
        return "V0"
    role = (_get(state, "claims_snapshot") or {}).get("role")
    return "V2" if role in BILLING_ROLES else "V1"


def visible_tool_names(state: Any) -> tuple[str, ...]:
    """What: the tool names the model may see and call in this state, sorted by name.
    Use when: ToolVisibilityMiddleware and ToolGuard's hidden-tool block. Inputs: agent state.
    Output: tuple of names. Side effects: none. Role: one rule for both sides."""
    return tuple(sorted(VISIBILITY[visibility_set(state)]))


def _tool_name(tool: Any) -> str:
    """What: a tool's name (BaseTool or dict spec). Use when: filtering. Inputs: tool. Output: name or "".
    Side effects: none. Role: helper."""
    if isinstance(tool, dict):
        return str(tool.get("name") or (tool.get("function") or {}).get("name") or "")
    return str(getattr(tool, "name", ""))


class ToolVisibilityMiddleware(AgentMiddleware):
    """What: replaces the request's tool list with the visible set, sorted by name, before every model call.
    Use when: item 4 of the stack (after the model-call limit, outside the fallback, so both models get the
    same list). Inputs: model request, handler. Output: the handler's response.
    Side effects: none. Role: doc 07 s5.2."""

    async def awrap_model_call(self, request: Any, handler: Callable[[Any], Awaitable[Any]]) -> Any:
        """What: filter and sort request.tools by the state's visibility set. Use when: each model call.
        Inputs: request, handler. Output: handler(request with the visible tools). Side effects: none.
        Role: the only place the bound tool list is chosen."""
        names = set(visible_tool_names(request.state))
        tools = sorted((t for t in request.tools if _tool_name(t) in names), key=_tool_name)
        return await handler(request.override(tools=tools))
