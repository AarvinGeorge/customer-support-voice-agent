"""What: ToolGuardMiddleware (doc 07 s2.5 item 10, s5.1 failure rules, s5.3, s8.1, s8.2; D-046, D-048): the one
layer around every tool call. In order: hidden-tool block, the combined tool limiter (total 8, search_kb 3,
request_verification 1), one prepare per turn, then the call with a clamped timeout, one budget-checked retry
on a timeout or an unavailable service, failure counting (the second failure of the same tool in a turn sets
route escalate(tool_failure); a failed search_kb sets escalate(retrieval_unavailable) at once, Gate 1 row 4),
step events, and untrusted wrapping of every result.
Use when: last in the create_agent stack (wrap_tool_call). The agent runs async only (D-048), so only
awrap_tool_call is defined.
Inputs: the tool call request (call, state) and the handler that runs the tool; TurnControl bound for the turn.
Output: ToolGuardMiddleware, ToolSpec, SPECS, clamp_timeout, retry_fits.
Side effects: mutates TurnControl counters and route; emits step.started, step.completed and status events.
Role: exactly one ToolMessage per tool call id, whatever blocks it (the spike 17c failure). Counters count
EXECUTED calls only and are incremented before the first await, so parallel calls in one batch cannot race.
A call that fails argument validation gives its counts back (it never left the process). A 4xx from
core is an answer (returned to the model, never retried, not a failure); a timeout, 5xx, unreachable service or
any unexpected exception (for example a malformed 200 body) is a failure. A failed, skipped, blocked or not-run result says "unknown", never empty.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from contracts import limits
from contracts.redact import redact_system
from contracts.tools import STATUS_ONE_ACTION_AT_A_TIME, STATUS_TOOL_NOT_AVAILABLE
from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import ToolMessage

from agent.clients.http import ToolCallError
from agent.deadline import clamp_timeout, retry_fits  # shared doc 07 s8.2 helpers (re-exported)
from agent.gates import identity_key, retrieval_unavailable
from agent.middleware.visibility import visible_tool_names
from agent.turn_control import CURRENT_CALL, CallScope, Route, TurnControl, require_control
from agent.untrusted import unknown_result, wrap_result, wrap_search

_log = logging.getLogger(__name__)
PER_TOOL_LIMITS: dict[str, int] = {"search_kb": limits.SEARCH_KB_PER_TURN,
                                   "request_verification": limits.REQUEST_VERIFICATION_PER_TURN}


@dataclass(frozen=True)
class ToolSpec:
    """What: one tool's timeout, retries and step wording. Use when: ToolGuard runs that tool.
    Inputs: listed timeout (None: no outbound call), retries, backoff, present-tense label, past-tense title,
    failure title, status line for the retry, whether it is a prepare. Output: value. Side effects: none.
    Role: doc 07 s5.1 timeout column and the s6.2 fixed title table."""

    timeout_s: float | None
    retries: int
    label: str
    title: str
    failed: str
    retry_message: str = "Retrying..."
    backoff_s: float = limits.CORE_READ_RETRY_BACKOFF_S
    prepare: bool = False


_BILLING_RETRY = "Retrying the billing system..."
_ACCOUNT_RETRY = "Retrying the account system..."
SPECS: dict[str, ToolSpec] = {
    "search_kb": ToolSpec(limits.KNOWLEDGE_SEARCH_TIMEOUT_S, limits.KNOWLEDGE_SEARCH_RETRIES,
                          "Searching help articles", "Searched help articles", "Help article search failed"),
    "request_verification": ToolSpec(None, 0, "Asking you to verify", "Asked you to verify", "Verification request failed"),
    "get_account_summary": ToolSpec(limits.CORE_READ_TIMEOUT_S, limits.CORE_READ_RETRIES, "Loading your account summary",
                                    "Loaded account summary", "Account summary failed", _ACCOUNT_RETRY),
    "list_charges": ToolSpec(limits.CORE_READ_TIMEOUT_S, limits.CORE_READ_RETRIES, "Checking your billing history",
                             "Checked billing history", "Billing history check failed", _BILLING_RETRY),
    "list_tickets": ToolSpec(limits.CORE_READ_TIMEOUT_S, limits.CORE_READ_RETRIES, "Checking your tickets",
                             "Checked tickets", "Ticket check failed", _ACCOUNT_RETRY),
    "prepare_refund": ToolSpec(limits.CORE_PREPARE_TIMEOUT_S, limits.CORE_PREPARE_RETRIES, "Preparing your refund",
                               "Prepared refund", "Refund prep failed", _BILLING_RETRY, prepare=True),
    "create_ticket": ToolSpec(limits.CORE_TICKET_TIMEOUT_S, limits.CORE_TICKET_RETRIES, "Opening a ticket",
                              "Opened a ticket", "Ticket creation failed", "Retrying the ticket system..."),
}
_UNKNOWN_SPEC = ToolSpec(None, 0, "Using a tool", "Used a tool", "Tool failed")


def _detail(name: str, artifact: Any) -> str | None:
    """What: the short step detail (at most 80 chars) from a tool's raw result. Use when: a step succeeded.
    Inputs: tool name, artifact. Output: text or None. Side effects: none. Role: chip detail (no raw ids)."""
    if not isinstance(artifact, dict):
        return None
    if name == "search_kb":
        return f"{len(artifact.get('docs') or [])} relevant sections"
    if name == "list_charges":
        return f"{len(artifact.get('charges') or [])} charges"
    if name == "list_tickets":
        return f"{len(artifact.get('tickets') or [])} tickets"
    if name == "prepare_refund" and artifact.get("decision"):
        return str(artifact["decision"]).replace("_", " ")
    if name == "create_ticket" and isinstance(artifact.get("ticket"), dict):
        return str(artifact["ticket"].get("display") or "")[:80] or None
    return None


def _rule(name: str, artifact: Any) -> dict[str, Any] | None:
    """What: the step.completed `rule` of a prepare step (decision, primary reason, first policy ref).
    Use when: a prepare succeeded. Inputs: name, artifact. Output: dict or None. Side effects: none.
    Role: doc 07 s6.2 prepare step field."""
    if not (SPECS.get(name, _UNKNOWN_SPEC).prepare and isinstance(artifact, dict)):
        return None
    codes, refs = artifact.get("reason_codes") or [], artifact.get("policy_refs") or []
    if not codes or not refs:
        return None
    return {"decision": artifact["decision"], "reason_code": codes[0], "policy_ref": refs[0]}


def _kept_artifact(name: str, artifact: Any, state: Any = None) -> dict[str, Any]:
    """What: the part of a result kept on the ToolMessage (checkpointed, never sent to a model): bound_to, the
    verification it was fetched under (agent.gates.identity_key: "user|account" or None), on every result
    (review PR #71 I3); for search_kb also the relevant chunks with their citation fields, so Gate 2 can cite them
    in later turns (D-065); for create_ticket the ticket id and display (session_state, D-071).
    Use when: the success path. Inputs: name, raw artifact, the agent state at the call.
    Output: {"bound_to": ..., "grounded_chunks"?: [...]}. Side effects: none.
    Role: code-held provenance for session evidence."""
    kept: dict[str, Any] = {"bound_to": identity_key(state if isinstance(state, dict) else {})}
    if name == "search_kb" and isinstance(artifact, dict) and isinstance(artifact.get("grounded_chunks"), list):
        kept["grounded_chunks"] = artifact["grounded_chunks"]
    if name == "create_ticket" and isinstance(artifact, dict) and isinstance(artifact.get("ticket"), dict):
        t = artifact["ticket"]
        kept["ticket"] = {"ticket_id": t.get("ticket_id"), "display": t.get("display")}  # D-071 session_state
    return kept


def _content(name: str, artifact: Any, raw_content: Any) -> str:
    """What: the wrapped content the model sees for a successful call. Use when: success.
    Inputs: name, artifact (raw result), the tool's own content (used when there is no artifact).
    Output: wrapped text. Side effects: none. Role: s5.3 wrapping, search_kb as verdict plus <doc> blocks."""
    if name == "search_kb" and isinstance(artifact, dict) and "docs" in artifact:
        return wrap_search(str(artifact.get("verdict")), artifact.get("instruction"), artifact["docs"])
    if artifact is not None:
        return wrap_result(name, artifact)
    return wrap_result(name, {"text": raw_content if isinstance(raw_content, str) else str(raw_content)})


class ToolGuardMiddleware(AgentMiddleware):
    """What: see module. Use when: item 10 of the stack. Inputs: optional visibility function (default: the
    s5.2 rule on the call's state). Output: one ToolMessage per call. Side effects: see module.
    Role: tool guard and combined tool limiter."""

    def __init__(self, visible: Callable[[Any], tuple[str, ...]] = visible_tool_names) -> None:
        """What: keep the visibility rule. Use when: build_middleware or tests. Inputs: visible function.
        Output: none. Side effects: none. Role: constructor."""
        super().__init__()
        self.visible = visible

    # --- events -------------------------------------------------------------------------------------------
    @staticmethod
    def _start(control: TurnControl, name: str, spec: ToolSpec, args: dict[str, Any]) -> tuple[str | None, int]:
        """What: emit step.started. Use when: before an attempt or a block. Inputs: control, name, spec, args.
        Output: (step id or None without a sink, start ms). Side effects: one event. Role: helper."""
        ev = control.events
        if ev is None:
            return None, 0
        step_id = ev.next_step_id()
        ev.emit("step.started", step_id=step_id, kind="tool", name=name, label=spec.label,
                args_redacted=redact_system(args))  # D-071: model-written args (the model never had a PIN)
        return step_id, ev.ms()

    @staticmethod
    def _done(control: TurnControl, step: tuple[str | None, int], status: str, title: str, attempt: int,
              detail: str | None = None, rule: dict[str, Any] | None = None) -> None:
        """What: emit step.completed. Use when: an attempt or a block ends. Inputs: control, step, status,
        title, attempt, detail, rule. Output: none. Side effects: one event. Role: helper."""
        ev, (step_id, t0) = control.events, step
        if ev is None or step_id is None:
            return
        ev.emit("step.completed", step_id=step_id, status=status, ms=max(ev.ms() - t0, 0), title=title,
                detail=(detail or None) and detail[:80], summary={}, attempt=attempt, rule=rule)

    def _blocked(self, control: TurnControl, call: dict[str, Any], payload: dict[str, Any], title: str) -> ToolMessage:
        """What: a call that is not run: one error ToolMessage plus a blocked step. Use when: hidden tool, limit,
        second prepare. Inputs: control, call, payload, title. Output: ToolMessage. Side effects: events.
        Role: never two results for one call id."""
        name = call["name"]
        spec = SPECS.get(name, _UNKNOWN_SPEC)
        self._done(control, self._start(control, name, spec, call.get("args") or {}), "blocked", title, 1)
        return ToolMessage(content=wrap_result(name, payload), tool_call_id=call["id"], name=name, status="error")

    # --- gates (synchronous: run before the first await) ---------------------------------------------------
    def _gate(self, control: TurnControl, request: Any) -> ToolMessage | None:
        """What: hidden-tool block, combined limiter, one prepare per turn; increments the counters of a call
        that passes. Use when: first thing for every call, with no await before it. Inputs: control, request.
        Output: a blocked ToolMessage, or None when the call may run. Side effects: counters, events.
        Role: D-048 limiter (executed calls only; a hidden or blocked call is not counted, not a failure)."""
        call = request.tool_call
        name = call["name"]
        if name not in self.visible(request.state) or name not in SPECS:
            return self._blocked(control, call, dict(STATUS_TOOL_NOT_AVAILABLE), "Not available")
        per_limit = PER_TOOL_LIMITS.get(name)
        if control.tool_calls >= limits.TOOL_CALLS_PER_TURN or (
                per_limit is not None and control.per_tool.get(name, 0) >= per_limit):
            return self._blocked(control, call, unknown_result("tool_limit_reached",
                                 message="This tool was not run: the per-message tool limit was reached."),
                                 "Step limit reached")
        spec = SPECS[name]
        if spec.prepare and control.prepares >= limits.PREPARES_PER_TURN:
            return self._blocked(control, call, dict(STATUS_ONE_ACTION_AT_A_TIME), "One action at a time")
        control.tool_calls += 1
        control.per_tool[name] = control.per_tool.get(name, 0) + 1
        if spec.prepare:
            control.prepares += 1
        return None

    async def awrap_tool_call(self, request: Any, handler: Callable[[Any], Awaitable[Any]]) -> Any:
        """What: gate the call, then run it with the clamp, the retry and the failure rules.
        Use when: every tool call (each runs in its own task). Inputs: request, handler.
        Output: exactly one ToolMessage. Side effects: see module. Role: entry point."""
        control = require_control()
        blocked = self._gate(control, request)
        if blocked is not None:
            return blocked
        call = request.tool_call
        CURRENT_CALL.set(CallScope(tool_call_id=call["id"], state=request.state))  # this call's task only
        return await self._run(control, request, handler, SPECS[call["name"]])

    async def _run(self, control: TurnControl, request: Any, handler: Callable[[Any], Awaitable[Any]],
                   spec: ToolSpec) -> ToolMessage:
        """What: attempts with clamp, retry and failure counting. Use when: a gated call. Inputs: control,
        request, handler, spec. Output: ToolMessage. Side effects: events, counters, route. Role: core loop."""
        call = request.tool_call
        name, args = call["name"], call.get("args") or {}
        attempt = 1
        while True:
            timeout = None if spec.timeout_s is None else clamp_timeout(spec.timeout_s, control.remaining_s())
            step = self._start(control, name, spec, args)
            if spec.timeout_s is not None and timeout is None:
                self._done(control, step, "skipped", spec.failed, attempt, detail="Not enough time left")
                control.set_route(Route("escalate", reason="deadline", tool=name))
                return self._message(call, unknown_result("skipped", reason="deadline"), error=True)
            try:
                coro = handler(request)
                msg = await (asyncio.wait_for(coro, timeout) if timeout is not None else coro)
            except (TimeoutError, asyncio.TimeoutError):
                err = ToolCallError("timeout", message="The service did not answer in time.")
            except ToolCallError as exc:
                err = exc
            except Exception as exc:  # malformed 2xx body or a tool bug: a failure, never a dangling call id
                _log.warning("tool %s raised %s; treated as a tool failure", name, type(exc).__name__)
                err = ToolCallError("unavailable", message="The service returned an unexpected answer.")
            else:
                return self._success(control, step, call, spec, attempt, msg, request.state)
            if not err.retryable:  # 4xx: core's answer, given to the model; no retry, not a failure
                self._done(control, step, "error", spec.failed, attempt, detail=err.message[:80] or None)
                return self._message(call, {"status": "error", "code": err.code, "message": err.message,
                                            "details": err.details}, error=True)
            control.failures[name] = control.failures.get(name, 0) + 1
            failures = control.failures[name]
            if (attempt <= spec.retries and failures < limits.SAME_TOOL_FAILURES_BEFORE_TICKET
                    and retry_fits(spec.timeout_s or 0.0, control.remaining_s(), spec.backoff_s)):
                self._done(control, step, "retrying", f"{spec.failed}, retrying", attempt,
                           detail="The service did not respond")
                if control.events is not None:
                    control.events.emit("status", kind="retrying", message=spec.retry_message)
                await asyncio.sleep(spec.backoff_s)
                attempt += 1
                continue
            self._done(control, step, "timeout" if err.kind == "timeout" else "error", spec.failed, attempt,
                       detail="The service did not respond")
            if name == "search_kb":  # Gate 1 row 4: a failed search is retrieval_unavailable at once (doc 07 s6.5)
                retrieval_unavailable(control)
            if failures >= limits.SAME_TOOL_FAILURES_BEFORE_TICKET:
                control.set_route(Route("escalate", reason="tool_failure", tool=name))
            status = "timeout" if err.kind == "timeout" else "error"
            return self._message(call, unknown_result(status, changed=err.changed, message=err.message), error=True)

    def _success(self, control: TurnControl, step: tuple[str | None, int], call: dict[str, Any], spec: ToolSpec,
                 attempt: int, msg: Any, state: Any = None) -> ToolMessage:
        """What: finish a call whose handler returned: wrap its result (or its argument error) for the model.
        Use when: the handler returned a ToolMessage. Inputs: control, step, call, spec, attempt, message.
        Output: ToolMessage without the raw artifact. Side effects: one event. Role: success path."""
        name = call["name"]
        if not isinstance(msg, ToolMessage):  # tools here never return Command; refuse anything else loudly
            raise TypeError(f"tool {name} returned {type(msg).__name__}, expected ToolMessage")
        if msg.status == "error":  # argument validation error from ToolNode: tell the model, not a failure
            self._refund(control, name, spec)
            self._done(control, step, "error", spec.failed, attempt, detail="Invalid arguments")
            return self._message(call, {"status": "invalid_arguments", "message": str(msg.content)[:500]}, error=True)
        artifact = msg.artifact
        self._done(control, step, "ok", spec.title, attempt, detail=_detail(name, artifact), rule=_rule(name, artifact))
        return ToolMessage(content=_content(name, artifact, msg.content), tool_call_id=call["id"], name=name,
                           artifact=_kept_artifact(name, artifact, state))

    @staticmethod
    def _refund(control: TurnControl, name: str, spec: ToolSpec) -> None:
        """What: give back the counts _gate took for a call that failed argument validation.
        Use when: the handler returned an argument error. Inputs: control, name, spec. Output: none.
        Side effects: decrements tool_calls, per_tool[name] and (for a prepare) prepares.
        Role: D-048 executed calls only; the corrected call can still run (review of PR #35, I1)."""
        control.tool_calls = max(control.tool_calls - 1, 0)
        control.per_tool[name] = max(control.per_tool.get(name, 0) - 1, 0)
        if spec.prepare:
            control.prepares = max(control.prepares - 1, 0)

    @staticmethod
    def _message(call: dict[str, Any], payload: dict[str, Any], *, error: bool) -> ToolMessage:
        """What: a wrapped ToolMessage for a payload. Use when: failure, skip or 4xx. Inputs: call, payload,
        error flag. Output: ToolMessage. Side effects: none. Role: helper."""
        return ToolMessage(content=wrap_result(call["name"], payload), tool_call_id=call["id"], name=call["name"],
                           status="error" if error else "success")
