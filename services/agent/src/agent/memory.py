"""What: ingress steps 10 and 11 (doc 07 s2.4): repair of dangling tool calls, and the memory refresh from core
GET /v1/me/context that feeds the untrusted account context of the turn message (s2.6, s5.3).
Use when: at the start of every turn, after the turn row exists and before structured input or the model.
Inputs: the compiled agent and run config (repair); a CoreAgentClient, the turn's TurnControl and the thread's
current values (refresh).
Output: repair_dangling, refresh_memory, MemoryRefresh, dangling_calls.
Side effects: repair writes ToolMessages to the thread via aupdate_state; refresh makes up to two core calls;
both emit step events (kinds repair and memory) when a sink is bound.
Role: plain code before the model. The refresh never writes the thread itself: the caller passes
MemoryRefresh.update into the agent input (or aupdate_state), so one write carries the turn's state.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

from contracts import limits
from langchain_core.messages import AIMessage, ToolMessage

from agent.clients.core_agent import CoreAgentClient
from agent.clients.http import ToolCallError
from agent.middleware.tool_guard import clamp_timeout, retry_fits
from agent.steps import Step
from agent.turn_control import EventSink, TurnControl
from agent.untrusted import not_run_tool_message


def dangling_calls(messages: list[Any]) -> list[dict[str, Any]]:
    """What: the tool calls of the thread's last AI message that have no ToolMessage after it.
    Use when: repair_dangling. Inputs: thread messages. Output: tool call dicts (empty when none).
    Side effects: none. Role: doc 07 s2.4 step 10 condition ("the thread's last AI message")."""
    last_ai = next((i for i in range(len(messages) - 1, -1, -1) if isinstance(messages[i], AIMessage)), None)
    if last_ai is None:
        return []
    answered = {m.tool_call_id for m in messages[last_ai + 1:] if isinstance(m, ToolMessage)}
    return [c for c in (messages[last_ai].tool_calls or []) if c.get("id") and c["id"] not in answered]


async def repair_dangling(agent: Any, config: dict[str, Any], *, events: EventSink | None = None) -> int:
    """What: append one not_run ToolMessage per dangling tool call (content {"status":"not_run",
    "reason":"turn_interrupted"} plus result "unknown" and the never-empty note), before anything else touches
    the thread. Use when: ingress step 10. Inputs: compiled agent, run config, optional event sink.
    Output: number of calls repaired. Side effects: one aupdate_state when any call dangles; repair step events.
    Role: an interrupted turn (killed replica, deadline mid tool call) never leaves the provider an AI message with
    unanswered tool calls, and the model reads the outcome as UNKNOWN, never as empty (spike 9)."""
    snapshot = await agent.aget_state(config)
    calls = dangling_calls(list((snapshot.values or {}).get("messages") or []))
    if not calls:
        return 0
    step = Step.start(events, "repair", "repair", "Tidying up an interrupted step")
    await agent.aupdate_state(config, {"messages": [not_run_tool_message(c) for c in calls]})
    step.done("ok", "Tidied up an interrupted step", detail=f"{len(calls)} unfinished step(s) marked not run")
    return len(calls)


@dataclass
class MemoryRefresh:
    """What: the result of the step 11 refresh. Use when: building the turn's state update and TurnContext.
    Inputs: update (state keys to write: context_snapshot and claims_snapshot, or the cleared verified fields),
    snapshot (the context to show this turn), stale (refresh failed, previous snapshot kept), verified_cleared
    (core said 401 SESSION_NOT_VERIFIED). Output: value. Side effects: none. Role: return shape."""

    update: dict[str, Any] = field(default_factory=dict)
    snapshot: dict[str, Any] | None = None
    stale: bool = False
    verified_cleared: bool = False

    @property
    def current_pending(self) -> dict[str, Any] | None:
        """What: MeContext.pending_action of the snapshot (the session's open pending action), or None.
        Use when: doc 07 s7.3 "current pending". Inputs: none. Output: PendingPublic dict or None.
        Side effects: none. Role: accessor."""
        return (self.snapshot or {}).get("pending_action")


async def refresh_memory(core: CoreAgentClient, control: TurnControl, values: dict[str, Any]) -> MemoryRefresh:
    """What: GET /v1/me/context for a verified session, clamped to min(2 s, remaining - 3 s reserve), with one
    retry on core_unavailable after 200 ms only when the budget check allows it.
    Use when: ingress step 11, and right after a successful verify (s6.3 step 5 "load MeContext").
    Inputs: agent-token core client, the turn's control, current thread values (verified, context_snapshot).
    Output: MemoryRefresh. Side effects: up to two core calls; memory step events.
    Role: success stores the snapshot and claims; any other failure keeps the previous snapshot and marks it
    stale; 401 SESSION_NOT_VERIFIED (claims expired after 120 minutes) clears verified and both snapshots, so
    the next model call sees V0 (s5.2)."""
    previous = values.get("context_snapshot")
    if not values.get("verified"):
        return MemoryRefresh()
    step = Step.start(control.events, "memory", "me_context", "Checking your account")
    attempt = 1
    while True:
        timeout = clamp_timeout(limits.CORE_READ_TIMEOUT_S, control.remaining_s())
        if timeout is None:
            step.done("skipped", "Used the earlier account details", detail="Not enough time left", attempt=attempt)
            return MemoryRefresh(snapshot=previous, stale=True)
        try:
            ctx = await core.get_context(control, timeout)
        except ToolCallError as err:
            if err.status == 401 and err.code == "SESSION_NOT_VERIFIED":
                step.done("error", "Verification expired", detail="Please verify again", attempt=attempt)
                return MemoryRefresh(update={"verified": False, "claims_snapshot": None, "context_snapshot": None},
                                     verified_cleared=True)
            if (err.retryable and attempt <= limits.CORE_READ_RETRIES
                    and retry_fits(limits.CORE_READ_TIMEOUT_S, control.remaining_s(), limits.CORE_READ_RETRY_BACKOFF_S)):
                await asyncio.sleep(limits.CORE_READ_RETRY_BACKOFF_S)
                attempt += 1
                continue
            step.done("timeout" if err.kind == "timeout" else "error", "Used the earlier account details",
                      detail="The account service did not respond", attempt=attempt)
            return MemoryRefresh(snapshot=previous, stale=True)
        step.done("ok", "Checked your account", attempt=attempt)
        return MemoryRefresh(update={"context_snapshot": ctx, "claims_snapshot": ctx.get("claims")}, snapshot=ctx)
