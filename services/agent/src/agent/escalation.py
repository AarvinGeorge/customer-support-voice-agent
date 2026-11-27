"""What: the escalation module (doc 07 s3.4 owners, s6.2 `escalated`, s6.5, s6.7, s8.1 failure-path ticket row, s8.2
deadline path; L1 s7 trigger table): the one-attempt code ticket with the deterministic key, the `escalated` event,
the fallback line when the ticket cannot be opened, the deadline path, the limit_reached ticket, the both-models-
unavailable apology and the ticket_offer flow.
Use when: TurnRunner after the agent loop (route escalations, limit, deadline, model failure), the step 13 handler
(ticket_offer body, voice yes after the apology, commit or submit failure tickets) and the verify lockout.
Inputs: the agent-token core client; per call the turn stream, TurnControl, the compiled agent and its run config.
Output: Escalator, the fixed lines, REASON_CATEGORY, last_customer_text, apology_is_last, model-unavailable ids.
Side effects: at most one core ticket per turn (POST /v1/tickets, 3 s clamped to the time left, 0 retries),
escalation step events, `escalated` events, best-effort thread writes inside the time left.
Role: runs in the 3 s reserve: the loop stops 3 s before the deadline (TurnRunner), so a ticket attempt (at most
3 s) always fits. L1 s7 rows and their owner:

| L1 s7 situation | Reason | Ticket by |
|---|---|---|
| Off-topic | (decline_off_topic) | nobody |
| Not in our docs / weak twice | not_in_docs | this module |
| Retrieval failure | retrieval_unavailable | this module |
| Grounding fails twice / commitment draft | grounding_failed / needs_commitment | this module |
| Customer asks for a human / commitment | customer_asked_human / needs_commitment | model create_ticket |
| Action needs approval | (submitted) | core, inside submit |
| Same tool fails twice; commit or submit failure | tool_failure | this module |
| Verification fails 3 times | verify_locked | core, inside verify |
| Model call limit; deadline | limit_reached; deadline | this module |
| Both models unavailable | model_unavailable | this module, only after the Create ticket button or a voice yes |
"""
from __future__ import annotations

import asyncio
import logging
import re
from typing import Any

from contracts import limits
from contracts.errors import ErrorBody
from contracts.events import FinalAnswer
from langchain_core.messages import AIMessage, HumanMessage

from agent.clients.core_agent import CoreAgentClient
from agent.clients.http import ToolCallError
from agent.memory import dangling_calls
from agent.middleware.router import ESCALATION_LEADS, NO_TICKET_CLAUSE, TICKET_CLAUSE
from agent.middleware.tool_guard import clamp_timeout
from agent.sse import TurnOutcome, TurnStream, route_outcome
from agent.steps import Step
from agent.turn_control import TurnControl
from agent.untrusted import not_run_tool_message

log = logging.getLogger("agent.escalation")

MODEL_UNAVAILABLE_LINE = ("Our assistant is having trouble right now, so I haven't changed anything. Would you like me "
                          "to open a ticket for our team?")
TICKET_OFFER_OPENED = "I've opened ticket ending {short} for our team."
NOTHING_TO_OFFER_LINE = "There's nothing waiting for a ticket right now. What can I help you with?"
DEADLINE_LEAD = ESCALATION_LEADS["deadline"]
DEADLINE_TICKET_LINE = "I couldn't finish that in time, so I've opened a ticket for our team: {display}."
UNAVAILABLE_ID_PREFIX = "unavailable-"

REASON_CATEGORY: dict[str, str] = {
    "not_in_docs": "other", "retrieval_unavailable": "other", "grounding_failed": "other",
    "needs_commitment": "commitment", "limit_reached": "other", "deadline": "other", "model_unavailable": "other",
    "tool_failure": "other",
}
TOOL_CATEGORY: dict[str, str] = {"prepare_refund": "refund", "list_charges": "billing"}
REASON_SUMMARY: dict[str, str] = {
    "not_in_docs": "The help articles do not cover the customer's question.",
    "retrieval_unavailable": "Help article search was unavailable, so the agent did not answer.",
    "grounding_failed": "The agent's draft answer failed the policy check twice.",
    "needs_commitment": "The customer needs a commitment the agent cannot make.",
    "tool_failure": "A system the agent depends on failed twice in one message.",
    "limit_reached": "The agent reached its step limit for one message.",
    "deadline": "The agent did not finish within the time limit for one message.",
    "model_unavailable": "The assistant's models were unavailable; the customer asked for a ticket.",
}
_CUSTOMER_RE = re.compile(r"<customer_message>\n?(.*?)\n?</customer_message>", re.S)


def ticket_key(control: TurnControl, reason: str) -> str:
    """What: the code escalation Idempotency-Key {session_id}:{turn_id}:esc:{reason} (doc 07 s3.4).
    Use when: every code ticket. Inputs: control, reason. Output: key. Side effects: none. Role: deterministic key."""
    return f"{control.session_id}:{control.turn_id}:esc:{reason}"


def turn_ref(control: TurnControl) -> str:
    """What: the last 4 characters of the turn id (dashes removed). Use when: the fallback line. Inputs: control.
    Output: text. Side effects: none. Role: doc 07 s8.1 "gives the turn_id last 4 as a reference"."""
    return control.turn_id.replace("-", "")[-4:]


def fallback_clause(control: TurnControl) -> str:
    """What: "I couldn't open a ticket just now. If you contact us, please quote reference abcd."
    Use when: a failure-path ticket could not be opened. Inputs: control. Output: text. Side effects: none.
    Role: doc 07 s8.1 fallback line."""
    return NO_TICKET_CLAUSE.format(ref=turn_ref(control))


def customer_text(content: Any) -> str:
    """What: the customer's own words from a thread human message (inside <customer_message>, else the content).
    Use when: ticket summaries. Inputs: message content. Output: text. Side effects: none. Role: helper."""
    text = content if isinstance(content, str) else str(content)
    m = _CUSTOMER_RE.search(text)
    return (m.group(1) if m else text).strip()


def last_customer_text(messages: list[Any]) -> str:
    """What: the last human message's customer text, skipping bracketed press lines ("[customer pressed ...]").
    Use when: ticket summaries. Inputs: thread messages. Output: text ("" when none). Side effects: none.
    Role: "summary from the last user message" (doc 07 s6.7)."""
    for m in reversed(messages):
        if isinstance(m, HumanMessage):
            text = customer_text(m.content)
            if text and not (text.startswith("[") and text.endswith("]")):
                return text
    return ""


def apology_is_last(messages: list[Any]) -> bool:
    """What: true when the thread's last AI message is the both-models-unavailable apology.
    Use when: the ticket_offer body and the voice yes (doc 07 s6.7 "while the last assistant message is this
    apology"). Inputs: thread messages. Output: bool. Side effects: none. Role: adjacency check."""
    last_ai = next((m for m in reversed(messages) if isinstance(m, AIMessage)), None)
    return last_ai is not None and str(last_ai.id or "").startswith(UNAVAILABLE_ID_PREFIX)


def deadline_error() -> ErrorBody:
    """What: the turn_deadline_exceeded error body (doc 07 fixture edge_turns e3). Use when: the deadline path.
    Inputs: none. Output: ErrorBody (retryable, changed unknown). Side effects: none. Role: helper."""
    return ErrorBody.model_validate({"class": "turn_deadline_exceeded", "code": "TURN_DEADLINE",
                                     "message": DEADLINE_LEAD, "retryable": True, "changed": None,
                                     "fault_injected": False, "details": {}})


def _final(text: str, *, escalated: bool, ticket: dict[str, Any] | None, say: str | None = None) -> FinalAnswer:
    """What: a code-written FinalAnswer. Use when: escalation outcomes. Inputs: text, flags, ticket, say.
    Output: FinalAnswer. Side effects: none. Role: helper (the say builder, CS-E-05.S2.T1, shapes `say`)."""
    return FinalAnswer.model_validate({"text": text, "say": say if say is not None else text, "citations": [],
                                       "escalated": escalated, "ticket": ticket})


class Escalator:
    """What: opens the turn's one code ticket and words the escalation outcomes. Use when: one per app (TurnRunner
    and StructuredInput build one from their core client). Inputs: agent-token core client.
    Output: open_ticket, emit, summary, finish, deadline, model_unavailable, ticket_offer. Side effects: see module.
    Role: escalation code (doc 07 s3.4 "agent code (origin code)" rows)."""

    def __init__(self, core: CoreAgentClient) -> None:
        """What: keep the client. Use when: app start or tests. Inputs: core client. Output: none.
        Side effects: none. Role: constructor."""
        self.core = core

    async def open_ticket(self, control: TurnControl, reason: str, *, summary: str, category: str | None = None,
                          key_reason: str | None = None) -> dict[str, Any] | None:
        """What: one ticket attempt, clamped to min(3 s, time left) with no reserve (this IS the reserve), key
        {sid}:{turn}:esc:{reason}, origin code; reuses a ticket already opened this turn (exactly one per turn).
        Use when: any code-owned escalation. Inputs: control, escalation reason, summary, category, key reason
        (defaults to reason). Output: TicketRef dict, or None on failure or when under 1 s is left.
        Side effects: one escalation step and at most one core call; sets control.ticket. Role: s8.1 row."""
        if control.ticket is not None:
            return control.ticket
        step = Step.start(control.events, "escalation", "create_ticket", "Opening a ticket")
        timeout = clamp_timeout(limits.CORE_TICKET_TIMEOUT_S, control.remaining_s(), reserve_s=0.0)
        if timeout is None:
            step.done("skipped", "Couldn't open a ticket", detail="Not enough time left")
            return None
        body = {"category": category or REASON_CATEGORY.get(reason, "other"), "summary": summary[:500] or reason,
                "steps_tried": [], "blocking": False, "origin": "code", "escalation_reason": reason,
                "transcript_ref": control.session_id}
        try:
            res = await asyncio.wait_for(self.core.create_ticket(control, body, ticket_key(control, key_reason or reason)),
                                         timeout)
        except (ToolCallError, TimeoutError, asyncio.TimeoutError) as err:
            step.done("timeout" if not isinstance(err, ToolCallError) or err.kind == "timeout" else "error",
                      "Couldn't open a ticket", detail="The ticket system did not respond")
            return None
        ticket = res.get("ticket") if isinstance(res, dict) else None
        if not isinstance(ticket, dict):
            step.done("error", "Couldn't open a ticket", detail="The ticket system gave no ticket")
            return None
        control.ticket = ticket
        step.done("ok", "Opened a ticket", detail=str(ticket.get("display") or ""))
        return ticket

    @staticmethod
    def emit(control: TurnControl, reason: str, ticket: dict[str, Any] | None, *, origin: str = "code",
             offer: bool = False) -> None:
        """What: the `escalated` event. Use when: every escalation. Inputs: control, reason, ticket, origin (code,
        model, core), offer_ticket_button. Output: none. Side effects: one event. Role: doc 07 s6.2."""
        if control.events is not None:
            control.events.emit("escalated", reason=reason, ticket=ticket, origin=origin, offer_ticket_button=offer)

    @staticmethod
    def summary(reason: str, messages: list[Any], extra: str | None = None) -> str:
        """What: the factual ticket summary: what happened plus the customer's last words. Use when: tickets.
        Inputs: reason, thread messages, optional extra fact. Output: text (..500). Side effects: none. Role: helper."""
        said = last_customer_text(messages)
        parts = [REASON_SUMMARY.get(reason, reason.replace("_", " ")), extra or "",
                 f"Customer's last message: {said}" if said else ""]
        return " ".join(p for p in parts if p)[:500]

    async def _thread(self, agent: Any, config: dict[str, Any], control: TurnControl, update: dict[str, Any]) -> None:
        """What: best-effort thread write inside the time left (at most the 2 s checkpointer limit).
        Use when: after the ticket. Inputs: agent, config, control, update. Output: none.
        Side effects: one aupdate_state; failures are logged. Role: doc 07 s2.4 step 15 append."""
        timeout = min(limits.POSTGRES_CHECKPOINTER_TIMEOUT_S, control.remaining_s())
        if timeout <= 0.05:
            log.warning("escalation line not written to the thread (no time left) turn_id=%s", control.turn_id)
            return
        try:
            await asyncio.wait_for(agent.aupdate_state(config, update), timeout)
        except Exception:  # noqa: BLE001 - the reply already exists; the next turn's repair covers the thread
            log.exception("escalation thread write failed turn_id=%s", control.turn_id)

    async def _messages(self, agent: Any, config: dict[str, Any]) -> list[Any]:
        """What: the thread messages (empty on failure). Use when: summaries. Inputs: agent, config.
        Output: messages. Side effects: one read. Role: helper."""
        try:
            return list(((await agent.aget_state(config)).values or {}).get("messages") or [])
        except Exception:  # noqa: BLE001
            return []

    async def finish(self, stream: TurnStream, control: TurnControl, outcome: TurnOutcome, agent: Any,
                     config: dict[str, Any]) -> TurnOutcome:
        """What: complete a loop outcome: an escalate route line gets its ticket (in the reserve), the `escalated`
        event and the line rewritten in the thread; a limit_reached turn gets its code ticket; a model-opened
        ticket (create_ticket tool) marks the final escalated.
        Use when: TurnRunner after step 14 returned. Inputs: stream, control, loop outcome, agent, config.
        Output: the final outcome. Side effects: see module. Role: router escalate, ToolGuard second failure,
        Gate 2 hook and ToolGuard deadline skip, all finished here (one ticket)."""
        route = control.route
        if route is not None and route.kind == "escalate" and control.route_message_id and \
                outcome.end_reason in ("escalated", "deadline", "limit_reached"):
            reason = route.reason or "tool_failure"
            if reason == "deadline":
                stream.emit("error", **{k: v for k, v in deadline_error().model_dump(mode="json", by_alias=True).items()
                                        if k != "trace_id"})
            if route.ticket is None:
                messages = await self._messages(agent, config)
                category = TOOL_CATEGORY.get(route.tool or "") if reason == "tool_failure" else None
                route.ticket = await self.open_ticket(control, reason, summary=self.summary(reason, messages),
                                                      category=category)
            new = route_outcome(stream, control, outcome.usage, outcome.model_used)
            if reason == "deadline":
                new.status, new.error = "failed", deadline_error()
            self.emit(control, reason, route.ticket)
            line = new.final.say if control.mode == "voice" else new.final.text
            await self._thread(agent, config, control, {"messages": [AIMessage(line, id=control.route_message_id)]})
            return new
        if outcome.end_reason == "limit_reached":
            messages = await self._messages(agent, config)
            ticket = await self.open_ticket(control, "limit_reached", summary=self.summary("limit_reached", messages))
            text = f"{ESCALATION_LEADS['limit_reached']} " + (
                TICKET_CLAUSE.format(display=ticket.get("display") or f"ticket ending {ticket.get('short_id')}")
                if ticket else fallback_clause(control))
            self.emit(control, "limit_reached", ticket)
            await self._thread(agent, config, control, {"messages": [AIMessage(text, id=f"limit-{control.turn_id}")]})
            outcome.final = _final(text, escalated=True, ticket=ticket)
            return outcome
        if control.ticket is not None and outcome.final.ticket is None:
            outcome.final = FinalAnswer.model_validate({**outcome.final.model_dump(mode="json"), "escalated": True,
                                                        "ticket": control.ticket})
        return outcome

    async def deadline(self, stream: TurnStream, control: TurnControl, agent: Any, config: dict[str, Any]) -> TurnOutcome:
        """What: the deadline path (doc 07 s8.2): the loop was stopped 3 s before the deadline; emit the error, open
        the deadline ticket (one attempt, in the reserve), say so; then mark dangling tool calls not run and append
        the line to the thread if time is left. A commit or submit never runs in the loop (step 13 ends the turn
        first), so there is no earlier action result to report here.
        Use when: TurnRunner's loop timeout expired, or a model failure left no loop time. Inputs: stream, control,
        agent, config. Output: TurnOutcome (end_reason deadline, status failed, error turn_deadline_exceeded).
        Side effects: see module. Role: deadline path."""
        err = deadline_error()
        stream.emit("error", **{k: v for k, v in err.model_dump(mode="json", by_alias=True).items() if k != "trace_id"})
        messages = await self._messages(agent, config)
        ticket = await self.open_ticket(control, "deadline", summary=self.summary("deadline", messages))
        text = (DEADLINE_TICKET_LINE.format(display=ticket.get("display") or f"ticket ending {ticket.get('short_id')}")
                if ticket else f"{DEADLINE_LEAD} {fallback_clause(control)}")
        self.emit(control, "deadline", ticket)
        repair = [not_run_tool_message(c) for c in dangling_calls(messages)]
        await self._thread(agent, config, control, {"messages": [*repair, AIMessage(text, id=f"deadline-{control.turn_id}")]})
        return TurnOutcome(final=_final(text, escalated=True, ticket=ticket), end_reason="deadline", status="failed",
                           error=err)

    async def model_unavailable(self, stream: TurnStream, control: TurnControl, agent: Any,
                                config: dict[str, Any]) -> TurnOutcome:
        """What: both models unavailable (doc 07 s6.7): fixed apology with no model, `escalated` reason
        model_unavailable with no ticket and the Create ticket button, end_reason escalated; nothing is created.
        Use when: the model node failed (control.model_node_failed) with loop time left. Inputs: stream, control, agent, config.
        Output: TurnOutcome. Side effects: one event, one thread write (id unavailable-{turn_id}). Role: s6.7."""
        self.emit(control, "model_unavailable", None, offer=True)
        await self._thread(agent, config, control, {"messages": [
            AIMessage(MODEL_UNAVAILABLE_LINE, id=f"{UNAVAILABLE_ID_PREFIX}{control.turn_id}")]})
        return TurnOutcome(final=_final(MODEL_UNAVAILABLE_LINE, escalated=True, ticket=None), end_reason="escalated")

    async def ticket_offer(self, control: TurnControl, messages: list[Any]) -> tuple[str, str, dict[str, Any] | None]:
        """What: the Create ticket button (or a voice strict yes) after the apology: one ticket, origin code,
        category other, reason model_unavailable, summary from the last user message, key
        {sid}:{turn}:esc:model_unavailable, no model call. Without the apology as the last AI message nothing is
        created. Use when: step 13. Inputs: control, thread messages. Output: (text, end_reason, ticket).
        Side effects: at most one ticket and one `escalated` event. Role: doc 07 s6.7 button flow."""
        if not apology_is_last(messages):
            return NOTHING_TO_OFFER_LINE, "answered", None
        ticket = await self.open_ticket(control, "model_unavailable", summary=self.summary("model_unavailable", messages))
        self.emit(control, "model_unavailable", ticket)
        if ticket is None:
            return fallback_clause(control), "escalated", None
        return TICKET_OFFER_OPENED.format(short=ticket.get("short_id")), "escalated", ticket
