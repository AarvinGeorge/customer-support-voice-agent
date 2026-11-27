"""What: ingress step 13 in the doc 07 s2.4 order: the verification body, the ticket_offer body (CS-E-03.S4.T1), the
confirm body, then a strict yes or no on typed text against the current pending action (s7.3).
Use when: TurnRunner(structured=StructuredInput(core, confirmer, flags)).
Inputs: the agent-token core client, the Confirmer, optional session flags; per turn the stream, the IngressInput
(its `body` is the parsed TurnRequest) and the TurnPrep (thread values and the step 11 refresh).
Output: StructuredInput, the fixed lines.
Side effects: core calls through verification and the Confirmer; verification.*, confirmation.required and
action.result events.
Role: decides whether the turn ends with a deterministic reply or goes to the model; the model can never commit.
Current pending = MeContext.pending_action read from core this turn (the step 11 snapshot).
"""
from __future__ import annotations

from typing import Any

from contracts.events import FinalAnswer
from langchain_core.messages import AIMessage, HumanMessage

from agent.clients.core_agent import CoreAgentClient
from agent.prompt import TurnContext, build_turn_message
from agent.sse import TurnOutcome, TurnStream
from agent.structured.confirm import ActionReply, Confirmer
from agent.structured.grammar import is_strict_no, is_strict_yes
from agent.structured.pending import confirm_label, ingress_message_id, is_adjacent, pending_ref
from agent.structured.verification import verify_card
from agent.turn_body import SessionFlags, StructuredResult, TurnPrep

NOT_CURRENT_LINE = "That request is no longer active. Ask me again if you still need it."
TICKET_OFFER_LINE = "I can't open that ticket from here just now. Please send your question again and I'll help."
CONFIRM_PRESSED = "[customer pressed Confirm]"
CANCEL_PRESSED = "[customer pressed Cancel]"


class StructuredInput:
    """What: the step 13 handler. Use when: one per app. Inputs: core client, confirmer, flags.
    Output: __call__(stream, inp, prep) -> StructuredResult. Side effects: see module. Role: s7.3 dispatcher."""

    def __init__(self, core: CoreAgentClient, confirmer: Confirmer, *, flags: SessionFlags | None = None) -> None:
        """What: keep dependencies. Use when: app start or tests. Inputs: see class. Output: none.
        Side effects: none. Role: constructor."""
        self.core, self.confirmer, self.flags = core, confirmer, flags

    async def __call__(self, stream: TurnStream, inp: Any, prep: TurnPrep) -> StructuredResult:
        """What: apply the s2.4 step 13 order. Use when: TurnRunner. Inputs: stream, IngressInput, TurnPrep.
        Output: StructuredResult. Side effects: see module. Role: entry."""
        body = getattr(inp, "body", None)
        if body is not None and getattr(body, "verification", None) is not None:
            return await verify_card(stream, prep.control, body.verification, self.core, self.flags)
        if body is not None and getattr(body, "ticket_offer", None) is not None:  # CS-E-03.S4.T1 owns this flow
            return self._reply(stream, prep, "[customer pressed Create ticket]", TICKET_OFFER_LINE, "answered")
        pending = prep.memory.current_pending
        confirm = getattr(body, "confirm", None) if body is not None else None
        if confirm is not None:
            human = CONFIRM_PRESSED if confirm.decision == "confirm" else CANCEL_PRESSED
            if pending is None or pending.get("pending_action_id") != confirm.pending_action_id:
                return self._not_current(stream, prep, human, confirm.pending_action_id)
            if confirm.decision == "confirm":  # yes signal 1
                return self._action(stream, prep, human, pending, await self.confirmer.confirm(prep.control, pending))
            return self._action(stream, prep, human, pending, await self.confirmer.cancel(prep.control, pending))
        text = inp.text
        if text is None or pending is None:  # a yes or no with no current pending answers something else
            return StructuredResult()
        yes, no = is_strict_yes(text), is_strict_no(text)
        if not (yes or no):  # anything else while a pending exists: the model, told the pending awaits yes or no
            return StructuredResult(pending_summary=str(pending.get("readback_text") or ""))
        human_msg = build_turn_message(text, TurnContext(mode="voice" if inp.mode == "voice" else "chat",
                                                         verified=bool(prep.values.get("verified"))))
        if not is_adjacent(prep.values, pending):
            return self._readback(stream, prep, human_msg, pending)
        if yes:  # yes signal 2 (typed chat, adjacent); step-up re-prompt is out of the thin slice
            return self._action(stream, prep, human_msg, pending, await self.confirmer.confirm(prep.control, pending))
        return self._action(stream, prep, human_msg, pending, await self.confirmer.cancel(prep.control, pending))

    @staticmethod
    def _human(line: Any) -> Any:
        """What: a thread human message for a press line or the built text message. Use when: replies.
        Inputs: str or message. Output: message. Side effects: none. Role: helper."""
        return HumanMessage(line) if isinstance(line, str) else line

    def _reply(self, stream: TurnStream, prep: TurnPrep, human: Any, text: str, end_reason: str, *,
               say: str | None = None, final_extra: dict[str, Any] | None = None,
               state: dict[str, Any] | None = None) -> StructuredResult:
        """What: a deterministic reply: human line plus AI line (id ingress-{turn_id}) go to the thread.
        Use when: every ending of step 13. Inputs: stream, prep, human line, text, end reason, say, final fields,
        state. Output: StructuredResult with an outcome. Side effects: none. Role: s2.4 step 15 append."""
        say = say if say is not None else text
        voice = prep.control.mode == "voice"
        outcome = TurnOutcome.fixed(text, end_reason=end_reason, say=say)
        if final_extra:
            outcome.final = FinalAnswer.model_validate({**outcome.final.model_dump(mode="json"), **final_extra})
        msg = AIMessage(say if voice else text, id=ingress_message_id(prep.control.turn_id))
        return StructuredResult(outcome=outcome, messages=[self._human(human), msg], state=state or {})

    def _not_current(self, stream: TurnStream, prep: TurnPrep, human: str, pending_id: str) -> StructuredResult:
        """What: a press for a pending that is not the current one: no core call, outcome expired, changed false.
        Use when: confirm body mismatch. Inputs: stream, prep, line, the pressed id. Output: result.
        Side effects: one action.result. Role: s7.3 "does not qualify" row 3."""
        stream.emit("action.result", pending_action_id=pending_id, action_type="unknown", outcome="expired",
                    changed=False, reason_codes=[], policy_refs=[], result_text=NOT_CURRENT_LINE)
        return self._reply(stream, prep, human, NOT_CURRENT_LINE, "answered")

    def _readback(self, stream: TurnStream, prep: TurnPrep, human: Any, pending: dict[str, Any]) -> StructuredResult:
        """What: repeat the read-back in code and show the card again; sets readback_turn_id and clears
        readback_interrupted. Use when: a strict yes or no that is not adjacent, or core said CONFIRMATION_REQUIRED.
        Inputs: stream, prep, human line, pending. Output: result (end_reason readback). Side effects: one
        confirmation.required. Role: s7.3 row 1."""
        stream.emit("confirmation.required", pending=pending, confirm_label=confirm_label(pending))
        return self._reply(stream, prep, human, str(pending.get("readback_text") or ""), "readback",
                           say=str(pending.get("readback_say") or pending.get("readback_text") or ""),
                           final_extra={"pending_action": pending},
                           state={"pending": pending_ref(pending, prep.control.turn_id)})

    def _action(self, stream: TurnStream, prep: TurnPrep, human: Any, pending: dict[str, Any],
                reply: ActionReply) -> StructuredResult:
        """What: emit action.result and end the turn with the outcome's line. Use when: after the Confirmer.
        Inputs: stream, prep, human line, pending, reply. Output: result. Side effects: events.
        Role: s7.3 outcome table to the wire (action.result plus the final)."""
        if reply.repeat_readback:
            return self._readback(stream, prep, human, pending)
        voice = prep.control.mode == "voice"
        stream.emit("action.result", pending_action_id=pending.get("pending_action_id"),
                    action_type=str(pending.get("action_type") or "unknown"), outcome=reply.outcome,
                    changed=reply.changed, reason_codes=reply.reason_codes, policy_refs=reply.policy_refs,
                    result_text=reply.say if voice else reply.text, ticket=reply.ticket)
        state = {} if reply.keep_pending else {"pending": None}
        return self._reply(stream, prep, human, reply.text, reply.end_reason, say=reply.say, state=state,
                           final_extra={"escalated": reply.end_reason == "escalated", "ticket": reply.ticket})
