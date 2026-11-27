"""What: SSE envelope, the 14 event types, FinalAnswer, Usage and EndReason (doc 07 s6.1, s6.2).
Use when: the agent emits or stores an event, the browser reduces one, evals read turn_events.
Inputs: none.
Output: one model per event type, the SseEvent discriminated union, parse_event, format_sse.
Side effects: none.
Role: the streaming contract between agent and web, including the r4 additions (past-tense step
title and detail, per-model-step tokens, prepare rule, citation url and excerpt)."""
import json
from typing import Annotated, Any, Literal, Union, get_args

from pydantic import Field, TypeAdapter

from contracts.agent_api import VerifiedPerson
from contracts.base import StrictModel
from contracts.core_api import PendingPublic, PolicyRef, TicketRef
from contracts.errors import ErrorBody

Mode = Literal["chat", "voice"]
EndReason = Literal["answered", "clarify", "readback", "verification_requested", "verified",
                    "committed", "submitted", "cancelled", "escalated", "declined_off_topic",
                    "cap_reached", "limit_reached", "deadline", "error", "swept"]
END_REASONS: tuple[str, ...] = get_args(EndReason)


class Envelope(StrictModel):
    """What: fields on every SSE event. Use when: base of every event class.
    Inputs: v, seq, session, turn, trace ids, time, mode. Output: model. Side effects: none.
    Role: doc 07 s6.1 Envelope (seq from 1 per turn; trace_id equals turn_id inside a turn)."""

    v: Literal[1]
    seq: int = Field(ge=1)
    session_id: str
    turn_id: str
    trace_id: str
    ts: str
    mode: Mode


class TurnStartedEvent(Envelope):
    """What: first event of every turn. Use when: ingress step 9. Inputs: replica, input kind,
    contract version. Output: model. Side effects: none. Role: doc 07 s6.2 turn.started."""

    type: Literal["turn.started"]
    served_by: str
    input_kind: Literal["text", "confirm", "verification", "ticket_offer", "voice"]
    contract_version: str


StepKind = Literal["tool", "model", "gate", "verify", "commit", "submit", "cancel", "escalation",
                   "repair", "memory"]


class StepStartedEvent(Envelope):
    """What: a step chip starts. Use when: before a tool, model, gate or ingress step.
    Inputs: step id "{turn_id}:{n}", kind, name, present-tense label, redacted args.
    Output: model. Side effects: none. Role: doc 07 s6.2 step.started."""

    type: Literal["step.started"]
    step_id: str
    kind: StepKind
    name: str
    label: str
    args_redacted: dict[str, Any]


class StepRule(StrictModel):
    """What: the rule outcome shown on a prepare step. Use when: step.completed for prepare_*.
    Inputs: decision, reason code, policy ref. Output: model. Side effects: none. Role: doc 07 r4."""

    decision: Literal["allowed", "needs_approval", "declined"]
    reason_code: str
    policy_ref: PolicyRef


class StepCompletedEvent(Envelope):
    """What: a step chip ends. Use when: after each step. Inputs: status, ms, past-tense title,
    short detail, summary, attempt; model steps add model and token counts; prepare steps add rule.
    Output: model. Side effects: none. Role: doc 07 s6.2 step.completed (r4 fields)."""

    type: Literal["step.completed"]
    step_id: str
    status: Literal["ok", "error", "retrying", "blocked", "timeout", "skipped"]
    ms: int
    title: str
    detail: str | None = Field(default=None, max_length=80)
    summary: dict[str, Any]
    attempt: int
    model: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_read_tokens: int | None = None
    rule: StepRule | None = None


class GateClaim(StrictModel):
    """What: one claim checked by Gate 2. Use when: gate event claims list.
    Inputs: text, supported, source id. Output: model. Side effects: none. Role: doc 07 s6.2."""

    text: str
    supported: bool
    source_id: str | None = None


class GateEvent(Envelope):
    """What: a Gate 1 (retrieval) or Gate 2 (grounding) decision. Use when: after search or draft.
    Inputs: gate, verdict, decision, scores, thresholds, chunk ids, claim counts.
    Output: model. Side effects: none. Role: doc 07 s6.2 gate."""

    type: Literal["gate"]
    step_id: str
    gate: Literal["retrieval", "grounding"]
    verdict: str
    decision: Literal["answer", "clarify", "escalate", "decline", "skip"]
    top_cosine: float | None = None
    tau_low: float | None = None
    tau_high: float | None = None
    calibration_status: str | None = None
    code_match: bool | None = None
    chunk_ids: list[str]
    claims_total: int | None = None
    claims_supported: int | None = None
    claims: list[GateClaim] | None = None
    commitment: bool | None = None


class StatusEvent(Envelope):
    """What: a transient status line. Use when: slow model, retry, policy check.
    Inputs: kind, message. Output: model. Side effects: none. Role: doc 07 s6.2 status."""

    type: Literal["status"]
    kind: Literal["slow", "retrying", "checking_policy"]
    message: str


class TokenEvent(Envelope):
    """What: answer text chunk (only after Gate 2 passed or skipped). Use when: flushing a draft.
    Inputs: text. Output: model. Side effects: none (not stored). Role: doc 07 s6.2 token."""

    type: Literal["token"]
    text: str


class VerificationRequiredEvent(Envelope):
    """What: ask the browser to show the verification card. Use when: route verify, retry, typed PIN.
    Inputs: reason, prompt, attempts left, fields. Output: model. Side effects: none. Role: doc 07 s6.3."""

    type: Literal["verification.required"]
    reason: Literal["account_lookup", "action", "ticket_status", "typed_pin_rejected", "retry"]
    prompt_text: str
    attempts_left: int
    fields: list[Literal["account_id", "pin"]]


class VerificationResultEvent(Envelope):
    """What: outcome of a verification attempt. Use when: after core verify.
    Inputs: status, person, attempts left, ticket. Output: model. Side effects: none. Role: doc 07 s6.2."""

    type: Literal["verification.result"]
    status: Literal["verified", "failed", "locked", "conflict", "unavailable"]
    person: VerifiedPerson | None = None
    attempts_left: int | None = None
    ticket: TicketRef | None = None


class ConfirmationRequiredEvent(Envelope):
    """What: show the confirmation card for a pending action. Use when: route readback.
    Inputs: pending, button label. Output: model. Side effects: none. Role: doc 07 s6.2."""

    type: Literal["confirmation.required"]
    pending: PendingPublic
    confirm_label: str


ActionOutcome = Literal["committed", "submitted", "cancelled", "declined", "rules_changed", "expired",
                        "failed", "unconfirmed", "step_up_failed", "step_up_locked", "act_alone_applied"]


class ActionResultEvent(Envelope):
    """What: outcome of a commit, submit, cancel or decline. Use when: after ingress confirm code.
    Inputs: id, type, outcome, changed, reasons, refs, text, ticket. Output: model.
    Side effects: none. Role: doc 07 s6.2 and s7.3 outcome table (changed null means unknown)."""

    type: Literal["action.result"]
    pending_action_id: str | None = None
    action_type: str
    outcome: ActionOutcome
    changed: bool | None = None
    reason_codes: list[str]
    policy_refs: list[PolicyRef]
    result_text: str
    ticket: TicketRef | None = None


class EscalatedEvent(Envelope):
    """What: the turn escalated to a human. Use when: any escalate route or core-created ticket.
    Inputs: reason, ticket, origin, button flag. Output: model. Side effects: none. Role: doc 07 s6.2."""

    type: Literal["escalated"]
    reason: Literal["not_in_docs", "grounding_failed", "retrieval_unavailable", "tool_failure",
                    "limit_reached", "deadline", "verify_locked", "needs_commitment",
                    "customer_asked_human", "model_unavailable"]
    ticket: TicketRef | None = None
    origin: Literal["code", "model", "core"]
    offer_ticket_button: bool


class ModelFallbackEvent(Envelope):
    """What: the fallback model took over. Use when: ModelFallbackMiddleware switches.
    Inputs: from, to, reason. Output: model. Side effects: none. Role: doc 07 s6.2."""

    type: Literal["model.fallback"]
    from_model: str
    to_model: str
    reason: Literal["rate_limit", "timeout", "5xx", "spend_cap", "auth", "fault_llm_429"]


class ErrorEvent(Envelope, ErrorBody):  # pyright: ignore[reportIncompatibleVariableOverride]  # trace_id is a str in both bases; wire shape is frozen (doc 07 s6.2)
    """What: an error inside the stream (always followed by turn.completed).
    Use when: a turn fails after the stream opened. Inputs: ErrorBody fields ("class" key).
    Output: model. Side effects: none. Role: doc 07 s6.2 error."""

    type: Literal["error"]


class Citation(StrictModel):
    """What: one cited help-article chunk. Use when: FinalAnswer.citations.
    Inputs: chunk id, label, ref, spoken, public url, section title, excerpt, cosine.
    Output: model. Side effects: none. Role: doc 07 s6.2 (r4: url = Chunk.source_url, excerpt ..240)."""

    chunk_id: str
    label: str
    ref: str
    spoken: str
    url: str
    section_title: str
    excerpt: str = Field(max_length=240)
    score: float


class FinalConfidence(StrictModel):
    """What: gate summary on the final answer. Use when: FinalAnswer.confidence.
    Inputs: retrieval verdict, scores, thresholds, grounding. Output: model. Side effects: none.
    Role: doc 07 s6.2 (null when no model draft this turn)."""

    retrieval_verdict: str | None = None
    top_cosine: float | None = None
    tau_low: float | None = None
    tau_high: float | None = None
    grounding: Literal["pass", "fail", "skipped", "not_run"]


class FinalAnswer(StrictModel):
    """What: the turn's answer for chat (text) and voice (say). Use when: turn.completed.
    Inputs: text, say, citations, escalation, ticket, pending, confidence. Output: model.
    Side effects: none. Role: doc 07 s6.2 FinalAnswer."""

    text: str
    say: str
    citations: list[Citation]
    escalated: bool
    ticket: TicketRef | None = None
    pending_action: PendingPublic | None = None
    confidence: FinalConfidence | None = None


class Usage(StrictModel):
    """What: the turn's token, cost and latency totals. Use when: turn.completed, drawer.
    Inputs: token counts, cost, timings, call counts, models. Output: model. Side effects: none.
    Role: doc 07 s6.2 Usage."""

    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_write_tokens: int
    cost_usd_est: float
    first_status_ms: int | None = None
    ttft_ms: int | None = None
    latency_ms: int
    model_calls: int
    tool_calls: int
    models_used: list[str]
    fallback_used: bool


class TurnCompletedEvent(Envelope):
    """What: last event of every stream (also after error). Use when: ingress step 16 or replay.
    Inputs: final, usage, end reason, replayed. Output: model. Side effects: none. Role: doc 07 s6.2."""

    type: Literal["turn.completed"]
    final: FinalAnswer
    usage: Usage
    end_reason: EndReason
    replayed: bool


_EVENT_CLASSES = (TurnStartedEvent, StepStartedEvent, StepCompletedEvent, GateEvent, StatusEvent,
                  TokenEvent, VerificationRequiredEvent, VerificationResultEvent,
                  ConfirmationRequiredEvent, ActionResultEvent, EscalatedEvent, ModelFallbackEvent,
                  ErrorEvent, TurnCompletedEvent)

SseEvent = Annotated[Union[_EVENT_CLASSES], Field(discriminator="type")]  # type: ignore[valid-type]
EVENT_TYPES: tuple[str, ...] = tuple(get_args(c.model_fields["type"].annotation)[0] for c in _EVENT_CLASSES)

_ADAPTER: TypeAdapter[Any] = TypeAdapter(SseEvent)  # pyright: ignore[reportArgumentType]  # runtime Union of the event classes, not expressible as a static TypeForm


def parse_event(data: dict[str, Any] | str) -> Envelope:
    """What: validate one event (dict or JSON string) into its typed class.
    Use when: reading fixtures, turn_events rows or a captured stream.
    Inputs: event dict or JSON text. Output: the event model; raises ValidationError if invalid.
    Side effects: none. Role: the single parser for the event union."""
    if isinstance(data, str):
        return _ADAPTER.validate_json(data)
    return _ADAPTER.validate_python(data)


def format_sse(event: Envelope) -> str:
    """What: render one event in the SSE wire format.
    Use when: the agent writes an event to the response stream.
    Inputs: an event model. Output: "event: {type}\\nid: {seq}\\ndata: {json}\\n\\n".
    Side effects: none. Role: doc 07 s6.1 wire format."""
    payload = json.dumps(event.model_dump(mode="json", by_alias=True), separators=(",", ":"))
    return f"event: {event.type}\nid: {event.seq}\ndata: {payload}\n\n"  # type: ignore[attr-defined]


KEEPALIVE = ": keepalive\n\n"
