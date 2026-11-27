"""What: the masked verification card body (doc 07 s6.3 steps 4 to 6): core verify on the agent token and its five
results (verified, failed, locked, conflict, unavailable), with the thread line "[customer verified via secure card]"
that never contains the account id or the PIN.
Use when: ingress step 13, first in order, when the TurnRequest carries `verification`.
Inputs: the turn stream, the turn's TurnControl, the VerificationInput, the agent-token core client, optional
session flags writer, the turn mode and whether the thread was verified before.
Output: verify_card, the fixed lines.
Side effects: one core verify (0 retries), a MeContext load on success, verification.result and (on failure)
verification.required events, a verify step, agent.sessions.awaiting_verification cleared on success.
Role: the PIN goes to core only; nothing here returns or logs it.
"""
from __future__ import annotations

from typing import Any

from contracts import limits
from contracts.agent_api import VerifiedPerson
from contracts.events import FinalAnswer
from langchain_core.messages import AIMessage, HumanMessage

from agent.clients.core_agent import CoreAgentClient
from agent.clients.http import ToolCallError
from agent.memory import refresh_memory
from agent.middleware.router import VERIFY_LINE
from agent.middleware.tool_guard import clamp_timeout
from agent.sse import TurnOutcome, TurnStream
from agent.steps import Step
from agent.structured.pending import ingress_message_id
from agent.turn_body import SessionFlags, StructuredResult
from agent.turn_control import TurnControl

VERIFIED_THREAD_LINE = "[customer verified via secure card]"
CARD_SUBMITTED_LINE = "[customer submitted the secure verification card]"
FAILED_LINE = "That account ID and PIN didn't match. You have {n} {tries} left."
LOCKED_LINE = "I couldn't verify you after three tries, so I've opened a ticket for our team, {display}."
LOCKED_NO_TICKET_LINE = "I couldn't verify you after three tries, so verification is locked for this conversation."
CONFLICT_LINE = ("This conversation is already verified as someone else. Please start a new conversation to verify "
                 "as another person.")
UNAVAILABLE_LINE = "I couldn't check your details just now. Please try the card again in a moment."


def _fixed(stream: TurnStream, control: TurnControl, text: str, end_reason: str, *,
           ticket: dict[str, Any] | None = None, escalated: bool = False) -> StructuredResult:
    """What: a deterministic verification reply: the card-submitted line and the reply go to the thread.
    Use when: every non-success result. Inputs: stream, control, line, end reason, ticket, escalated.
    Output: StructuredResult with an outcome. Side effects: none. Role: helper (s2.4 step 15)."""
    outcome = TurnOutcome.fixed(text, end_reason=end_reason)
    outcome.final = FinalAnswer.model_validate({**outcome.final.model_dump(mode="json"), "escalated": escalated,
                                                "ticket": ticket})
    return StructuredResult(outcome=outcome, messages=[HumanMessage(CARD_SUBMITTED_LINE),
                                                       AIMessage(text, id=ingress_message_id(control.turn_id))])


def _retry_prompt(stream: TurnStream, text: str, attempts_left: int) -> None:
    """What: re-emit verification.required reason retry. Use when: failed or unavailable. Inputs: stream, prompt,
    attempts left. Output: none. Side effects: one event. Role: s6.3 step 5 failure."""
    stream.emit("verification.required", reason="retry", prompt_text=f"{text} {VERIFY_LINE}".strip(),
                attempts_left=attempts_left, fields=["account_id", "pin"])


async def verify_card(stream: TurnStream, control: TurnControl, body: Any, core: CoreAgentClient,
                      flags: SessionFlags | None = None) -> StructuredResult:
    """What: run the card verification and decide the turn.
    Use when: step 13 with TurnRequest.verification. Inputs: stream, control, VerificationInput, core client, flags.
    Output: StructuredResult: on success no outcome (continue into the agent loop with the thread line, turn context
    verification "just_verified" and the new claims and MeContext in state); otherwise a fixed reply.
    Side effects: see module. Role: s6.3 steps 4 and 5."""
    step = Step.start(control.events, "verify", "verify", "Checking your details")
    timeout = clamp_timeout(limits.CORE_VERIFY_TIMEOUT_S, control.remaining_s())
    try:
        if timeout is None:
            raise ToolCallError("timeout", message="skipped")
        claims = await core.verify(control, body.account_id, body.pin, timeout)
    except ToolCallError as err:
        code = err.code or ""
        if code == "VERIFY_FAILED":
            n = int(err.details.get("attempts_left") or 0)
            control.verify_attempts_left = n
            step.done("error", "Details did not match", detail=f"{n} tries left")
            stream.emit("verification.result", status="failed", attempts_left=n, ticket=err.details.get("ticket"))
            text = FAILED_LINE.format(n=n, tries="try" if n == 1 else "tries")
            _retry_prompt(stream, text, n)
            return _fixed(stream, control, text, "verification_requested")
        if code == "VERIFY_LOCKED":
            ticket = err.details.get("ticket")
            step.done("error", "Verification locked")
            stream.emit("verification.result", status="locked", attempts_left=0, ticket=ticket)
            # L1 s7 "verification fails 3 times": core owns this ticket (doc 07 s3.4), so origin core, no agent ticket
            stream.emit("escalated", reason="verify_locked", ticket=ticket, origin="core", offer_ticket_button=False)
            text = (LOCKED_LINE.format(display=ticket.get("display") or f"ticket ending {ticket.get('short_id')}")
                    if ticket else LOCKED_NO_TICKET_LINE)
            return _fixed(stream, control, text, "escalated", ticket=ticket, escalated=True)
        if code == "IDENTITY_CONFLICT":
            step.done("error", "Already verified as someone else")
            stream.emit("verification.result", status="conflict")
            return _fixed(stream, control, CONFLICT_LINE, "answered")
        step.done("timeout" if err.kind == "timeout" else "error", "Could not check your details")
        stream.emit("verification.result", status="unavailable", attempts_left=control.verify_attempts_left)
        _retry_prompt(stream, UNAVAILABLE_LINE, control.verify_attempts_left)
        return _fixed(stream, control, UNAVAILABLE_LINE, "verification_requested")
    person = VerifiedPerson(display_name=claims["display_name"], role=claims["role"], tier=claims["tier"],
                            contract_status=claims["contract_status"], account_id=claims["account_id"])
    step.done("ok", "Verified your details", detail=person.display_name)
    stream.emit("verification.result", status="verified", person=person.model_dump(mode="json"))
    control.awaiting_verification = False
    if flags is not None:
        await flags.set_awaiting_verification(control.session_id, False, claims.get("user_id"))
    memory = await refresh_memory(core, control, {"verified": True})  # s6.3 step 5 "load MeContext"
    state: dict[str, Any] = {"verified": True, "claims_snapshot": claims}
    if memory.snapshot is not None:
        state["context_snapshot"] = memory.snapshot
    return StructuredResult(model_text=VERIFIED_THREAD_LINE, verification="just_verified", state=state,
                            context_stale=memory.stale)
