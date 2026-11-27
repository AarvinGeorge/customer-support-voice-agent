"""What: the SSE adapter (doc 07 s6.1, s6.2, s6.4, s8.1): per-turn envelope and seq, keepalive, buffered model
messages flushed as ~40-character tokens, status slow at 4 s, exactly one turn.completed, and a turn that keeps
running when the client disconnects.
Use when: ingress opens a stream for a turn (TurnStream), runs its work in the background (start_turn) and, for
an agent loop turn, uses stream_agent_turn as the body.
Inputs: TurnMeta; a body coroutine; write_final and after callbacks (final row, lock release); the compiled agent.
Output: TurnStream.frames() (the HTTP response iterator), TurnOutcome, the asyncio task.
Side effects: background tasks (kept referenced until done); events validated against packages/contracts.
Role: the only writer of the SSE wire format in the agent. Tokens come only from a complete draft of node
`model` that is still the final message after the run (Gate 2 runs inside the run), never from tool-call
messages, hook lines or chunks tagged internal:gate (spikes 1, 6).
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import Any, AsyncGenerator, Awaitable, Callable

from contracts import VERSION, limits
from contracts.errors import ErrorBody
from contracts.events import KEEPALIVE, Citation, Envelope, FinalAnswer, FinalConfidence, Usage, format_sse, parse_event
from langchain_core.messages import AIMessage, RemoveMessage, ToolMessage

from agent.middleware.router import route_reply
from agent.models.usage import add_usage, normalize_usage, token_usage
from agent.say import speak
from agent.structured.pending import confirm_label
from agent.turn_control import TURN_CONTROL, TurnControl, bind_turn_control

log = logging.getLogger("agent.sse")

FLUSH_CHARS = 40
LIMIT_PREFIX = "Model call limits exceeded:"  # ModelCallLimitMiddleware(exit_behavior="end") text (spike 17a)
# The thread line until the escalation module (agent.escalation, CS-E-03.S4.T1) rewrites it with the limit_reached
# ticket after the loop (same message id, limit-{turn_id}).
LIMIT_LINE = "I couldn't finish that within the steps I'm allowed for one message."
SLOW_LINE = "This is taking a little longer than usual."
INTERNAL_ERROR_CLASS = "core_unavailable"  # doc 07 s1.3 has no generic server class; closest internal class
_RUNNING: set[asyncio.Task[Any]] = set()



def chunk_text(text: str, size: int = FLUSH_CHARS) -> list[str]:
    """What: split text into consecutive pieces of `size` characters (last one shorter).
    Use when: flushing a passed draft as token events. Inputs: text, size. Output: pieces (empty for "").
    Side effects: none. Role: doc 07 s6.4 "chunks of about 40 characters, no artificial delay"."""
    return [text[i:i + size] for i in range(0, len(text), size)]


@dataclass(frozen=True)
class TurnMeta:
    """What: the per-turn envelope facts. Use when: opening a TurnStream.
    Inputs: session id, turn id (= trace id), mode, replica name, input kind. Output: value. Side effects: none.
    Role: doc 07 s6.1 envelope and s6.2 turn.started fields."""

    session_id: str
    turn_id: str
    mode: str
    served_by: str
    input_kind: str


@dataclass
class TurnOutcome:
    """What: what a turn ended with. Use when: write_final and turn.completed.
    Inputs: final answer, usage, end reason, status (completed or failed), error body, model fields.
    Output: value. Side effects: none. Role: one shape for the final row and the last event."""

    final: FinalAnswer
    usage: Usage = field(default_factory=token_usage)
    end_reason: str = "answered"
    status: str = "completed"
    error: ErrorBody | None = None
    model_used: str | None = None
    fallback_used: bool = False

    @classmethod
    def fixed(cls, text: str, *, end_reason: str, say: str | None = None, status: str = "completed",
              error: ErrorBody | None = None) -> "TurnOutcome":
        """What: an outcome for a code-written line (no model draft). Use when: cap, deadline, routes, errors.
        Inputs: text, end reason, optional say, status, error. Output: TurnOutcome (confidence null).
        Side effects: none. Role: constructor. `say` is the spoken form (agent.say.speak) of say, else of text."""
        return cls(final=FinalAnswer(text=text, say=speak(say if say is not None else text), citations=[],
                                     escalated=False), end_reason=end_reason, status=status, error=error)


class TurnStream:
    """What: one turn's event stream: builds envelopes, validates every event with the contracts union,
    renders SSE frames into a queue and remembers non-token events (for turn_events, T8).
    Use when: one per turn. Inputs: TurnMeta, keepalive interval, optional sink (TrajectoryWriter.add) that
    receives every non-token event. Output: emit(), frames(), events, timings.
    Side effects: queue writes; sink calls. Role: wire format owner."""

    def __init__(self, meta: TurnMeta, *, keepalive_s: float = limits.KEEPALIVE_INTERVAL_S,
                 sink: Callable[[Envelope], None] | None = None) -> None:
        """What: empty stream. Use when: ingress step 9. Inputs: meta, keepalive, sink (None: events are not
        persisted, for example the capped turn, which has no turns row). Output: none.
        Side effects: none. Role: constructor."""
        self.meta, self.keepalive_s, self.sink = meta, keepalive_s, sink
        self.seq, self.step_n = 0, 0
        self.events: list[Envelope] = []
        self.completed = False
        self.t0 = time.monotonic()
        self.first_status_ms: int | None = None
        self.ttft_ms: int | None = None
        self._queue: asyncio.Queue[str | None] = asyncio.Queue()

    def ms(self) -> int:
        """What: milliseconds since the stream opened. Use when: timings. Inputs: none. Output: int.
        Side effects: none. Role: clock."""
        return int((time.monotonic() - self.t0) * 1000)

    def next_step_id(self) -> str:
        """What: "{turn_id}:{n}" with n from 1. Use when: a step starts. Inputs: none. Output: step id.
        Side effects: increments. Role: doc 07 s6.2 step_id."""
        self.step_n += 1
        return f"{self.meta.turn_id}:{self.step_n}"

    def emit(self, type_: str, **fields: Any) -> Envelope:
        """What: build, validate and queue one event. Use when: anything is sent to the client.
        Inputs: event type and its fields. Output: the typed event.
        Side effects: queue write; sink call (errors logged, never raised); timings; raises RuntimeError on a second turn.completed and
        ValidationError on a malformed event (a bug, never sent). Role: the single emit point."""
        if self.completed:
            raise RuntimeError("turn.completed already sent for this turn")
        self.seq += 1
        m = self.meta
        ev = parse_event({"v": 1, "seq": self.seq, "session_id": m.session_id, "turn_id": m.turn_id,
                          "trace_id": m.turn_id, "ts": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
                          "mode": m.mode, "type": type_, **fields})
        now = self.ms()
        if type_ not in ("turn.started", "turn.completed", "token") and self.first_status_ms is None:
            self.first_status_ms = now
        if type_ == "token" and self.ttft_ms is None:
            self.ttft_ms = now
        if type_ != "token":
            self.events.append(ev)
            if self.sink is not None:
                try:
                    self.sink(ev)
                except Exception:  # noqa: BLE001 - doc 07 s12 G6: persistence never fails a turn
                    log.exception("event sink failed turn_id=%s", m.turn_id)
        self._queue.put_nowait(format_sse(ev))
        if type_ == "turn.completed":
            self.completed = True
            self._queue.put_nowait(None)
        return ev

    def close(self) -> None:
        """What: end the frame iterator (idempotent safety net). Use when: the driver finishes.
        Inputs: none. Output: none. Side effects: queue sentinel. Role: cleanup."""
        self._queue.put_nowait(None)

    async def frames(self) -> AsyncGenerator[str, None]:
        """What: SSE frames until turn.completed; ": keepalive" when idle for keepalive_s.
        Use when: the HTTP response body. Inputs: none. Output: frames. Side effects: none.
        Role: response iterator; closing it (client disconnect) does not touch the turn task."""
        while True:
            try:
                item = await asyncio.wait_for(self._queue.get(), self.keepalive_s)
            except (TimeoutError, asyncio.TimeoutError):
                yield KEEPALIVE
                continue
            if item is None:
                return
            yield item


def _internal_error() -> ErrorBody:
    """What: the customer-safe body for an unexpected failure. Use when: a body raised.
    Inputs: none. Output: ErrorBody. Side effects: none. Role: helper (decision in the report)."""
    return ErrorBody.model_validate({"class": INTERNAL_ERROR_CLASS, "code": "INTERNAL",
                                     "message": "Something went wrong on our side. Please try again.",
                                     "retryable": True, "changed": None, "fault_injected": False, "details": {}})


def _emit_error(stream: TurnStream, err: ErrorBody) -> None:
    """What: emit an error event; the envelope supplies trace_id (= turn id).
    Use when: a turn fails after the stream opened. Inputs: stream, body. Output: none.
    Side effects: one event. Role: doc 07 s6.2 error (always followed by turn.completed)."""
    fields = err.model_dump(mode="json", by_alias=True)
    fields.pop("trace_id", None)
    stream.emit("error", **fields)


async def _drive(stream: TurnStream, body: Callable[[TurnStream], Awaitable[TurnOutcome]],
                 write_final: Callable[[TurnOutcome], Awaitable[None]],
                 after: Callable[[TurnOutcome], Awaitable[None]] | None) -> TurnOutcome:
    """What: turn.started, body, final row, turn.completed, after (doc 07 s2.4 steps 9, 14, 16).
    Use when: start_turn. Inputs: stream, body, callbacks. Output: the outcome.
    Side effects: events, callbacks; failures of write_final and after are logged, never raised.
    Role: guarantees exactly one turn.completed per stream, also after an error."""
    outcome: TurnOutcome | None = None
    try:
        stream.emit("turn.started", served_by=stream.meta.served_by, input_kind=stream.meta.input_kind,
                    contract_version=VERSION)
        try:
            outcome = await body(stream)
        except Exception:  # noqa: BLE001 - the stream must still end cleanly
            log.exception("turn body failed turn_id=%s", stream.meta.turn_id)
            err = _internal_error()
            _emit_error(stream, err)
            outcome = TurnOutcome.fixed(err.message, end_reason="error", status="failed", error=err)
        if outcome.error is not None and outcome.status == "failed" and \
                not any(getattr(e, "type", None) == "error" for e in stream.events):
            _emit_error(stream, outcome.error)
        # doc 07 s6.2 FinalAnswer.say: every final is speakable (no markdown, ids as endings, amounts in words)
        outcome.final = outcome.final.model_copy(update={"say": speak(outcome.final.say)})
        latency = stream.ms()
        outcome.usage = outcome.usage.model_copy(update={
            "latency_ms": latency, "first_status_ms": stream.first_status_ms,
            "ttft_ms": stream.ttft_ms if stream.ttft_ms is not None else latency})
        try:
            await asyncio.wait_for(write_final(outcome), limits.POSTGRES_CHECKPOINTER_TIMEOUT_S)
        except Exception:  # noqa: BLE001 - doc 07 s2.4 step 16: failure is logged, never blocks
            log.exception("final row write failed turn_id=%s", stream.meta.turn_id)
        stream.emit("turn.completed", final=outcome.final.model_dump(mode="json"),
                    usage=outcome.usage.model_dump(mode="json"), end_reason=outcome.end_reason, replayed=False)
        return outcome
    finally:
        if after is not None:
            try:
                await after(outcome or TurnOutcome.fixed("", end_reason="error", status="failed"))
            except Exception:  # noqa: BLE001
                log.exception("after-turn hook failed turn_id=%s", stream.meta.turn_id)
        stream.close()


def start_turn(stream: TurnStream, body: Callable[[TurnStream], Awaitable[TurnOutcome]], *,
               write_final: Callable[[TurnOutcome], Awaitable[None]],
               after: Callable[[TurnOutcome], Awaitable[None]] | None = None) -> asyncio.Task[TurnOutcome]:
    """What: run the turn in its own task so a client disconnect never cancels it.
    Use when: ingress after the turn row exists. Inputs: stream, body, write_final, after (lock release).
    Output: the task (also kept in a module set until done). Side effects: schedules the task.
    Role: doc 06 s17 / 07 s6.1 "turn keeps running on client disconnect"."""
    task = asyncio.create_task(_drive(stream, body, write_final, after), name=f"turn:{stream.meta.turn_id}")
    _RUNNING.add(task)
    task.add_done_callback(_RUNNING.discard)
    return task


def _ai_messages(result: Any) -> list[AIMessage]:
    """What: AI messages written by a model task. Use when: task results. Inputs: task result (dict or
    list of (channel, value)). Output: AIMessages. Side effects: none. Role: helper."""
    items: list[Any] = []
    if isinstance(result, dict):
        items = list(result.get("messages") or [])
    elif isinstance(result, (list, tuple)):
        for entry in result:
            if isinstance(entry, (list, tuple)) and len(entry) == 2 and entry[0] == "messages":
                items.extend(entry[1] if isinstance(entry[1], list) else [entry[1]])
    return [m for m in items if isinstance(m, AIMessage)]


def _tool_messages(result: Any) -> int:
    """What: count ToolMessages written by a tools task. Use when: tool_calls usage. Inputs: task result.
    Output: count. Side effects: none. Role: helper."""
    msgs = result.get("messages") if isinstance(result, dict) else None
    if isinstance(msgs, list):
        return sum(isinstance(m, ToolMessage) for m in msgs)
    return 0


def final_confidence(control: TurnControl | None) -> FinalConfidence:
    """What: FinalAnswer.confidence for a turn with a model draft: the last Gate 1 verdict and scores, and the
    Gate 2 result (TurnControl.grounding: pass, fail, skipped, or not_run when Gate 2 did not run). Use when: classify_final.
    Inputs: the turn's control or None. Output: FinalConfidence. Side effects: none. Role: doc 07 s6.2."""
    r = (control.retrieval if control is not None else None) or {}
    return FinalConfidence(retrieval_verdict=r.get("verdict"), top_cosine=r.get("top_cosine"),
                           tau_low=r.get("tau_low"), tau_high=r.get("tau_high"),
                           grounding=control.grounding if control is not None else "not_run")  # type: ignore[arg-type]


def route_outcome(stream: TurnStream, control: TurnControl, usage: Usage, model_used: str | None) -> TurnOutcome:
    """What: the outcome of a turn the router ended: its fixed line, say and end_reason; a verify route also
    emits verification.required (doc 07 s6.3 step 3); a readback route with a card emits confirmation.required and
    puts the card in FinalAnswer.pending_action (CS-E-03.S7.T8).
    Use when: classify_final sees the router's line as the last message. Inputs: stream, control, usage,
    model id. Output: TurnOutcome. Side effects: one verification.required event for verify. Role: helper."""
    reply = route_reply(control)
    if reply.end_reason == "verification_requested" and reply.verification_reason in (
            "account_lookup", "action", "ticket_status"):
        stream.emit("verification.required", reason=reply.verification_reason, prompt_text=reply.text,
                    attempts_left=control.verify_attempts_left, fields=["account_id", "pin"])
    card = (control.route.pending or {}).get("card") if control.route is not None else None
    if reply.end_reason == "readback" and card:  # doc 07 s6.2: the card is shown on the read-back turn
        stream.emit("confirmation.required", pending=card, confirm_label=confirm_label(card))
    final = FinalAnswer.model_validate({"text": reply.text, "say": reply.say, "citations": [],
                                        "escalated": reply.escalated, "ticket": reply.ticket,
                                        "pending_action": card if reply.end_reason == "readback" else None})
    return TurnOutcome(final=final, usage=usage, end_reason=reply.end_reason, model_used=model_used)


def classify_final(stream: TurnStream, messages: list[Any], model_ids: set[str],
                   usage: Usage | None = None, model_used: str | None = None,
                   control: TurnControl | None = None) -> TurnOutcome:
    """What: decide the turn's final from the thread after the run, and flush a passed draft as tokens.
    Use when: end of stream_agent_turn. Inputs: stream, final messages, ids of messages written by node
    `model`, accumulated usage, last model id, the turn's control (router lines).
    Output: TurnOutcome.
    Side effects: token events for a model draft only (with the citations Gate 2 recorded); the Gate 2
    clarifying line ends "clarify" with no tokens. Role: doc 07 s6.4 flush rule and s2.4 step 15
    (the model-call limit line is never an answer); a router line ends with its route's end_reason."""
    usage = usage or token_usage()
    last = messages[-1] if messages else None
    if not isinstance(last, AIMessage):
        return replace(TurnOutcome.fixed("", end_reason="error", status="failed", error=_internal_error()),
                       usage=usage)
    if control is not None and control.route is not None and last.id == control.route_message_id:
        return route_outcome(stream, control, usage, model_used)
    text = last.text if isinstance(last.text, str) else str(last.content)
    if control is not None and control.gate_message_id is not None and last.id == control.gate_message_id:
        # Gate 2 first fail: the fixed clarifying line replaced the draft (doc 07 s6.4); written by code, no tokens.
        final = FinalAnswer(text=text, say=text, citations=[], escalated=False, confidence=final_confidence(control))
        return TurnOutcome(final=final, usage=usage, end_reason="clarify", model_used=model_used)
    if text.startswith(LIMIT_PREFIX):
        return replace(TurnOutcome.fixed(LIMIT_LINE, end_reason="limit_reached"), usage=usage, model_used=model_used)
    if last.id in model_ids and not last.tool_calls:
        for piece in chunk_text(text):
            stream.emit("token", text=piece)
        cites = [Citation.model_validate(c) for c in (control.citations if control is not None else [])]
        final = FinalAnswer(text=text, say=text, citations=cites, escalated=False, confidence=final_confidence(control))
        # Gate 1 row 2: a first weak turn ends with the model's one clarifying question.
        clarify = control is not None and (control.retrieval or {}).get("decision") == "clarify"
        return TurnOutcome(final=final, usage=usage, end_reason="clarify" if clarify else "answered",
                           model_used=model_used)
    # Another hook-written line (Gate 2): deterministic, no tokens.
    return replace(TurnOutcome.fixed(text, end_reason="answered"), usage=usage, model_used=model_used)


def _cut_detail(cuts: int, failed: bool) -> str | None:
    """What: the model step's drawer detail when replies were cut at max_tokens (review PR #68 I-1).
    Use when: step.completed of a model step. Inputs: cut replies in this step, whether the step failed.
    Output: a line of 80 characters or fewer, or None. Side effects: none. Role: helper."""
    if not cuts:
        return None
    what = "1 reply" if cuts == 1 else f"{cuts} replies"
    return f"{what} cut at the token cap; {'no model finished' if failed else 'discarded and retried'}"


async def stream_agent_turn(stream: TurnStream, agent: Any, agent_input: dict[str, Any], config: dict[str, Any], *,
                            slow_after_s: float = limits.SLOW_STATUS_AFTER_S,
                            control: TurnControl | None = None,
                            cost_of: Callable[[str, Usage], float] | None = None) -> TurnOutcome:
    """What: run the agent loop and translate it into step, status and token events.
    Use when: ingress step 14 for a turn that goes to the model. Inputs: stream, compiled agent, input,
    run config (turn_run_config: thread_id = session_id, run_id = turn_id), slow threshold, the turn's TurnControl
    (default: the bound one, else a new one for this stream; it is bound here, once, before astream, when not
    already bound), cost_of(model_name, usage) -> USD (ModelRegistry.cost_for_model; None: cost 0).
    Output: TurnOutcome with usage (tokens, cached tokens, cost_usd_est, model_calls = provider attempts counted by
    DeadlineAndFaultMiddleware plus the gate calls on TurnControl.gate_calls, doc 07 s2.3; tool_calls, models_used,
    fallback_used).
    Side effects: model and tool calls through the agent; a limit line is replaced in the thread; binds
    TURN_CONTROL for the run and resets it afterwards.
    Role: adapter between LangGraph streams (messages, tasks, values) and doc 07 s6.2 events."""
    control = control or TURN_CONTROL.get() or TurnControl(session_id=stream.meta.session_id,
                                                           turn_id=stream.meta.turn_id, mode=stream.meta.mode)
    if control.events is None:
        control.events = stream
    token = bind_turn_control(control) if TURN_CONTROL.get() is not control else None
    try:
        return await _stream_agent_turn(stream, agent, agent_input, config, slow_after_s, control, cost_of)
    finally:
        if token is not None:
            TURN_CONTROL.reset(token)


async def _stream_agent_turn(stream: TurnStream, agent: Any, agent_input: dict[str, Any], config: dict[str, Any],
                             slow_after_s: float, control: TurnControl,
                             cost_of: Callable[[str, Usage], float] | None = None) -> TurnOutcome:
    """What: the body of stream_agent_turn with the control already bound. Use when: stream_agent_turn.
    Inputs: as stream_agent_turn. Output: TurnOutcome. Side effects: as stream_agent_turn. Role: helper."""
    usage = token_usage()
    model_ids: set[str] = set()
    model_used: str | None = None
    values: dict[str, Any] = {}
    attempt: dict[str, Any] = {}

    async def slow_timer(step_id: str) -> None:
        """What: emit status slow once if the attempt shows no chunk in time. Use when: a model task starts.
        Inputs: step id. Output: none. Side effects: one status event. Role: doc 07 s8.1 "Slow" row."""
        await asyncio.sleep(slow_after_s)
        if attempt.get("step_id") == step_id and not attempt.get("chunk") and not stream.completed:
            stream.emit("status", kind="slow", message=SLOW_LINE)

    def stop_timer() -> None:
        """What: cancel the current slow timer. Use when: first chunk or task end. Inputs: none.
        Output: none. Side effects: cancels a task. Role: helper."""
        t = attempt.get("timer")
        if t is not None:
            t.cancel()

    try:
        async for mode, chunk in agent.astream(agent_input, config, stream_mode=["messages", "tasks", "values"]):
            if mode == "values":
                values = chunk
            elif mode == "messages":
                _msg, meta = chunk
                if meta.get("langgraph_node") == "model" and "internal:gate" not in (meta.get("tags") or []):
                    attempt["chunk"] = True
                    stop_timer()
            elif mode == "tasks" and chunk.get("name") == "model":
                if "result" not in chunk and "error" not in chunk:
                    step_id = stream.next_step_id()
                    stream.emit("step.started", step_id=step_id, kind="model", name="agent", label="Thinking",
                                args_redacted={})
                    attempt.clear()
                    attempt.update(step_id=step_id, t=stream.ms(), chunk=False, cut0=len(control.truncated_calls))
                    attempt["timer"] = asyncio.create_task(slow_timer(step_id))
                else:
                    stop_timer()
                    if chunk.get("error") is not None:  # the model node failed (doc 07 s6.7 trigger)
                        control.model_node_failed = True
                    msgs = _ai_messages(chunk.get("result"))
                    call = token_usage()
                    model_name = "unknown"
                    for m in msgs:
                        if m.id is not None:
                            model_ids.add(m.id)
                        call = add_usage(call, normalize_usage(m.usage_metadata))
                        model_name = (m.response_metadata or {}).get("model_name") or model_name
                    model_used = model_name
                    cost = cost_of(model_name, call) if cost_of is not None and msgs else 0.0
                    usage = add_usage(usage, call.model_copy(update={"model_calls": 1, "models_used": [model_name],
                                                                     "cost_usd_est": cost}))
                    cuts = len(control.truncated_calls) - attempt.get("cut0", 0)
                    stream.emit("step.completed", step_id=attempt.get("step_id") or stream.next_step_id(),
                                status="error" if chunk.get("error") else "ok", ms=stream.ms() - attempt.get("t", 0),
                                title="Thought it through", detail=_cut_detail(cuts, bool(chunk.get("error"))),
                                summary={"truncated": cuts} if cuts else {}, attempt=1, model=model_name,
                                input_tokens=call.input_tokens, output_tokens=call.output_tokens,
                                cache_read_tokens=call.cache_read_tokens)
            elif mode == "tasks" and chunk.get("name") == "tools" and "result" in chunk:
                usage = usage.model_copy(update={"tool_calls": usage.tool_calls + _tool_messages(chunk["result"])})
    finally:
        stop_timer()
    if control.model_attempts:  # provider calls made (retries and fallback included), as LangSmith counts llm runs
        usage = usage.model_copy(update={"model_calls": control.model_attempts})
    # Gate calls (scope classifier, Gate 2 verifier) are llm runs too (doc 07 s2.3): count them, with tokens and cost.
    for gate_model, meta in control.gate_calls:
        call = normalize_usage(meta) if meta else token_usage()
        cost = cost_of(gate_model, call) if cost_of is not None and meta else 0.0
        usage = add_usage(usage, call.model_copy(update={"model_calls": 1, "models_used": [gate_model],
                                                         "cost_usd_est": cost}))
    # Replies cut at max_tokens were discarded but billed: keep their tokens and cost (already in model_attempts).
    for cut_model, meta in control.truncated_calls:
        call = normalize_usage(meta) if meta else token_usage()
        cost = cost_of(cut_model, call) if cost_of is not None and meta else 0.0
        usage = add_usage(usage, call.model_copy(update={"models_used": [cut_model], "cost_usd_est": cost}))
    usage = usage.model_copy(update={"fallback_used": usage.fallback_used or control.fallback_used})
    messages = list(values.get("messages") or [])
    outcome = classify_final(stream, messages, model_ids, usage, model_used, control)
    outcome.fallback_used = outcome.usage.fallback_used
    if outcome.end_reason == "limit_reached" and messages:
        # doc 07 s2.4 step 15 / spike 17a: replace the framework line in the thread with ours.
        await agent.aupdate_state(config, {"messages": [RemoveMessage(id=messages[-1].id), AIMessage(LIMIT_LINE, id=f"limit-{control.turn_id}")]})
    return outcome
