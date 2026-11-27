"""What: ingress step 9a (doc 07 s2.4): the s11.1 PIN scan of chat `text`, voice `question` and `user_transcript`,
and the s7.3 typed-PIN refusal for a credential-looking chat message.
Use when: Ingress runs scan_input on every turn right after turn.started, before the transcript merge, the agent
body, the thread, turn_events, transcript_items, logs or LangSmith see the text; a refused turn runs TypedPinRefusal.
Inputs: the IngressInput and the session row (awaiting_verification, verified_user_id).
Output: session_redact_context, scan_input, Scanned, TypedPinRefusal, refuse_without_thread, AwaitingFlag,
TYPED_PIN_LINE (production flag writer: agent.turn_body.PgSessionFlags).
Side effects: the refusal emits verification.required, appends the redacted text and the fixed line to the thread,
and sets agent.sessions.awaiting_verification.
Role: the only place raw customer text is turned into the redacted text that travels further (D-031: PIN never in
the model context in chat). Step-up (condition 4) is designed only (A2), so step_up_pending stays false here.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from typing import Any, Protocol

from contracts.agent_api import TurnRequest, VoiceAskRequest
from contracts.redact import RedactContext, scan_pins

from agent.gates import identity_key
from agent.line_ids import INGRESS, code_line
from agent.assembly import run_config
from agent.prompt import TurnContext, build_turn_message
from agent.sse import TurnOutcome, TurnStream

log = logging.getLogger("agent.pin_guard")

TYPED_PIN_LINE = "For your security, please enter your PIN in the secure card, not the chat."
VERIFY_ATTEMPTS = 3  # doc 07 s13.1 identity.max_failures_per_session; the card shows it, nothing was tried yet


def session_redact_context(session: Any) -> RedactContext:
    """What: the scanner context of a session row. Use when: step 9a and the hooks that store text.
    Inputs: SessionRow (awaiting_verification, verified_user_id). Output: RedactContext. Side effects: none.
    Role: s11.1 conditions 1 and 3 come from agent.sessions, the authority (doc 07 s2.4 Counting)."""
    return RedactContext(awaiting_verification=bool(session.awaiting_verification),
                         verified=session.verified_user_id is not None)


@dataclass(frozen=True)
class Scanned:
    """What: the step 9a result. Use when: Ingress decides to refuse or continue, and whether to set or clear
    agent.sessions.awaiting_verification. Inputs: the redacted input, the refusal flag, the number of PINs redacted,
    expects_pin (the message announced a PIN without giving one: set the flag), moved_on (a substantive message with
    no number or credential words: clear the flag). Output: value. Side effects: none. Role: scan result."""

    inp: Any
    refuse: bool
    redacted: int
    expects_pin: bool = False
    moved_on: bool = False


def scan_input(inp: Any, ctx: RedactContext) -> Scanned:
    """What: redact every free-text field of one turn input: `text`, and in the body TurnRequest.text or
    VoiceAskRequest.question and user_transcript. A chat text turn whose scan is credential-looking is refused.
    Use when: Ingress step 9a. Inputs: IngressInput, context. Output: Scanned (redact_context set on the input).
    Side effects: none. Role: doc 07 s2.4 9a, s7.3 "Chat text and the masked card"."""
    total = 0
    refuse = expects = False
    moved = True
    text = inp.text
    if text is not None:
        scan = scan_pins(text, ctx)
        text, total = scan.text, scan.redacted
        refuse = inp.mode == "chat" and inp.input_kind == "text" and scan.credential_like
        expects, moved = scan.expects_pin, scan.moved_on
    body = inp.body
    if isinstance(body, TurnRequest) and body.text is not None:
        body = body.model_copy(update={"text": text})
    elif isinstance(body, VoiceAskRequest):  # inp.text is the question: count it once
        q = scan_pins(body.question, ctx)
        said = scan_pins(body.user_transcript, ctx) if body.user_transcript is not None else None
        total = q.redacted + (said.redacted if said else 0)
        expects = q.expects_pin or bool(said and said.expects_pin)
        moved = q.moved_on and (said is None or said.moved_on)
        body = body.model_copy(update={"question": q.text, "user_transcript": said.text if said else None})
        text = q.text if text is not None else None
    if text is None and not isinstance(body, VoiceAskRequest):
        moved = False  # a card press or a confirm is not the customer moving on
    return Scanned(replace(inp, text=text, body=body, redact_context=ctx), refuse, total, expects, moved)


class AwaitingFlag(Protocol):
    """What: writes agent.sessions.awaiting_verification. Use when: typing. Inputs: see method. Output: none.
    Side effects: database. Role: port (agent.turn_body.PgSessionFlags satisfies it)."""

    async def set_awaiting_verification(self, session_id: str, awaiting: bool) -> None:
        """What: set or clear the flag. Use when: the typed-PIN refusal. Inputs: session id, flag. Output: none.
        Side effects: one write. Role: port method."""
        ...


def _emit_refusal(stream: TurnStream) -> TurnOutcome:
    """What: the refusal event and outcome. Use when: both refusal paths. Inputs: stream. Output: outcome.
    Side effects: one verification.required event. Role: doc 07 s7.3 refusal wire shape."""
    stream.emit("verification.required", reason="typed_pin_rejected", prompt_text=TYPED_PIN_LINE,
                attempts_left=VERIFY_ATTEMPTS, fields=["account_id", "pin"])
    return TurnOutcome.fixed(TYPED_PIN_LINE, end_reason="verification_requested")


async def refuse_without_thread(stream: TurnStream, inp: Any, row: Any) -> TurnOutcome:
    """What: the refusal when no thread writer is wired (tests, partial wiring): event and fixed line only.
    Use when: Ingress default. Inputs: stream, redacted input, row. Output: outcome. Side effects: one event.
    Role: fail safe (nothing raw is written; the thread simply does not get the line)."""
    log.warning("typed PIN refused without a thread writer session_id=%s turn_id=%s", inp.session_id, row.turn_id)
    return _emit_refusal(stream)


class TypedPinRefusal:
    """What: the s7.3 refusal of a credential-looking chat message: verification.required typed_pin_rejected, the
    redacted text and the fixed line appended to the thread, awaiting_verification set true. No model call.
    Use when: Ingress(refuse_typed_pin=TypedPinRefusal(agent, flags)). Inputs: compiled agent (its checkpointer),
    optional AwaitingFlag. Output: __call__(stream, inp, row) -> TurnOutcome. Side effects: see module.
    Role: deterministic reply at step 9a (step 15 append)."""

    def __init__(self, agent: Any, flags: AwaitingFlag | None = None) -> None:
        """What: keep dependencies. Use when: build_ingress. Inputs: agent, flags. Output: none. Side effects: none.
        Role: constructor."""
        self.agent, self.flags = agent, flags

    async def __call__(self, stream: TurnStream, inp: Any, row: Any) -> TurnOutcome:
        """What: refuse one turn. Use when: Scanned.refuse. Inputs: stream, redacted IngressInput, TurnRow.
        Output: outcome (end_reason verification_requested). Side effects: see class (the flag only for an unverified
        session). Role: entry."""
        outcome = _emit_refusal(stream)
        config = run_config(inp.session_id)
        values = (await self.agent.aget_state(config)).values or {}
        human = build_turn_message(inp.text or "", TurnContext(mode="chat", verified=bool(values.get("verified"))))
        await self.agent.aupdate_state(config, {"messages": [human, code_line(INGRESS, row.turn_id, TYPED_PIN_LINE,
                                                                              identity_key(dict(values)))]})
        verified = bool(inp.redact_context and inp.redact_context.verified)
        if verified:  # review round 2 N-I2: a verified session is never put back to awaiting
            pass
        elif self.flags is not None:
            await self.flags.set_awaiting_verification(inp.session_id, True)
        else:
            log.warning("typed PIN refused but awaiting_verification was not set session_id=%s", inp.session_id)
        return outcome
