"""What: the escalation module (doc 07 s3.4 owners, s6.2 `escalated`, s6.5, s6.7, s8.1 failure-path ticket row, s8.2
deadline path; L1 s7 trigger table): the one-attempt code ticket with the deterministic key, the `escalated` event,
the fallback line when the ticket cannot be opened, the deadline path, the limit_reached ticket, the both-models-
unavailable apology and the ticket_offer flow; and D-070, the escalation of an unverified session: core refuses
the ticket (REQUESTER_UNKNOWN), so the code holds the escalation on agent.sessions and asks for the verification
card (or, when the card is already pending, only points at it; when verification is locked or the PIN is
unavailable, gives the fixed "How to reach support" line). The verification card opens the held ticket.
Use when: TurnRunner after the agent loop (route escalations, limit, deadline, model failure), the step 13 handler
(ticket_offer body, voice yes after the apology, commit or submit failure tickets, the verification card).
Inputs: the agent-token core client and an optional EscalationHolds store; per call the turn stream, TurnControl,
the compiled agent and its run config.
Output: Escalator, EscalationHolds, NoRequester, the fixed lines, REASON_CATEGORY, last_customer_text,
apology_is_last, EscalationFacts, model-unavailable ids.
Side effects: at most one core ticket per turn (POST /v1/tickets, 3 s clamped to the time left, 0 retries),
escalation step events, `escalated` events, verification.required for a hold, hold writes, best-effort thread
writes inside the time left (each code ticket with its code-written record, agent.records, D-071 part 2).
Role: runs in the 3 s reserve: the loop stops 3 s before the deadline (TurnRunner), so a ticket attempt (at most
3 s) always fits. L1 s7 rows and their owner:

| L1 s7 situation | Reason | Ticket by |
|---|---|---|
| Off-topic | (decline_off_topic) | nobody |
| Not in our docs / weak twice | not_in_docs | this module |
| Retrieval failure | retrieval_unavailable | this module |
| Grounding fails twice (a commitment draft counts as a fail, D-085) | grounding_failed | this module |
| Customer asks for a human / commitment | customer_asked_human / needs_commitment | model create_ticket |
| Action needs approval | (submitted) | core, inside submit |
| Same tool fails twice; commit or submit failure | tool_failure | this module |
| Verification fails 3 times | (no ticket, D-070) | nobody: fixed "How to reach support" line |
| Any row above while unverified | (held, D-070) | this module, after the verification card succeeds |
| Model call limit; deadline | limit_reached; deadline | this module |
| Both models unavailable | model_unavailable | this module, only after the Create ticket button or a voice yes |
"""
from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from typing import Any, Protocol

from contracts import limits
from contracts.errors import ErrorBody
from contracts.events import FinalAnswer
from langchain_core.messages import AIMessage, HumanMessage

from agent import line_ids
from agent.clients.core_agent import CoreAgentClient
from agent.clients.http import ToolCallError
from agent.records import ticket_record
from agent.memory import dangling_calls
from agent.middleware.router import ESCALATION_LEADS, TICKET_CLAUSE, escalation_lead, no_ticket_clause
from agent.middleware.tool_guard import clamp_timeout
from agent.say import speak
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
UNAVAILABLE_ID_PREFIX = line_ids.UNAVAILABLE
# D-070: an unverified session cannot get a ticket. These replace the ticket clause after the escalation lead.
HOLD_LINE = ("To open a ticket for our team, I need to know who you are first. Please enter your account ID and "
             "support PIN in the secure card.")
HOLD_SAY = ("To open a ticket for our team, I need to know who you are first. Please enter your account ID and "
            "support PIN in the secure card on your screen. I can't take them by voice.")
CARD_PENDING_LINE = "Please finish verifying in the secure card first, then I can open a ticket for our team."
NO_REQUESTER_LINE = ("I can't open a ticket without verifying who you are, and I can't verify you in this "
                     "conversation. The How to reach support section of our Support, response times and escalation "
                     "article explains what to do next.")

REASON_CATEGORY: dict[str, str] = {
    "not_in_docs": "other", "retrieval_unavailable": "other", "grounding_failed": "other",
    "limit_reached": "other", "deadline": "other", "model_unavailable": "other",
    "tool_failure": "other",
}
TOOL_CATEGORY: dict[str, str] = {"prepare_refund": "refund", "list_charges": "billing"}
REASON_SUMMARY: dict[str, str] = {
    "not_in_docs": "The help articles do not cover the customer's question.",
    "retrieval_unavailable": "Help article search was unavailable, so the agent did not answer.",
    "grounding_failed": "The agent's draft answer failed the policy check twice.",
    "tool_failure": "A system the agent depends on failed twice in one message.",
    "limit_reached": "The agent reached its step limit for one message.",
    "deadline": "The agent did not finish within the time limit for one message.",
    "model_unavailable": "The assistant's models were unavailable; the customer asked for a ticket.",
}
_CUSTOMER_RE = re.compile(r"<customer_message>\n?(.*?)\n?</customer_message>", re.S)


@dataclass(frozen=True)
class EscalationFacts:
    """What: the agent.sessions facts an unverified escalation needs. Use when: returned by EscalationHolds.
    Inputs: awaiting (agent.sessions.awaiting_verification: the card is on screen; set when it is shown, cleared on
    verify, dismiss and topic change, doc 07 s6.3 and s7.3), held (the held escalation or None), verified_user_id (who
    the session last verified as, None if never). Output: value. Side effects: none.
    Role: explicit state only, never inferred from turn history (PR #78 review I-2)."""

    awaiting: bool = False
    held: dict[str, Any] | None = None
    verified_user_id: str | None = None


class EscalationHolds(Protocol):
    """What: the agent.sessions facts an unverified escalation needs (D-070). Use when: typing the Escalator store.
    Inputs: see methods. Output: see methods. Side effects: database. Role: port (PgSessionFlags implements it)."""

    async def escalation_facts(self, session_id: str) -> EscalationFacts:
        """What: the session's awaiting flag, held escalation and last verified user.
        Use when: an escalation was refused for no requester, and on a successful verify. Inputs: session id.
        Output: EscalationFacts. Side effects: one SELECT. Role: port method."""
        ...

    async def hold_escalation(self, session_id: str, held: dict[str, Any] | None) -> None:
        """What: write or clear agent.sessions.held_escalation. Use when: holding, after the held ticket, on lockout
        or the card's no-PIN press. Inputs: session id, {reason, category, summary, turn_id, prior_user_id} or None.
        Output: none.
        Side effects: one UPDATE. Role: port method."""
        ...


@dataclass(frozen=True)
class NoRequester:
    """What: how an escalation ends when core refused the ticket for no verified requester.
    Use when: returned by Escalator.no_requester. Inputs: kind (hold: verification requested and the escalation
    held; card_pending: the card is already on screen; unavailable: verification is locked), the chat clause, the
    voice clause. Output: value. Side effects: none. Role: one decision, worded by each caller after its lead."""

    kind: str
    clause: str
    say: str

    @property
    def end_reason(self) -> str:
        """What: the end reason for a route escalation that ended this way. Use when: finish. Inputs: none.
        Output: verification_requested while a card is (or now is) on screen, else answered. Side effects: none.
        Role: D-070 (nothing was escalated)."""
        return "answered" if self.kind == "unavailable" else "verification_requested"


def ticket_key(control: TurnControl, reason: str) -> str:
    """What: the code escalation Idempotency-Key {session_id}:{turn_id}:esc:{reason} (doc 07 s3.4).
    Use when: every code ticket. Inputs: control, reason. Output: key. Side effects: none. Role: deterministic key."""
    return f"{control.session_id}:{control.turn_id}:esc:{reason}"


def fallback_clause(control: TurnControl) -> str:
    """What: "I couldn't open a ticket just now. If you contact us, please quote reference abcd."
    Use when: a failure-path ticket could not be opened. Inputs: control. Output: text. Side effects: none.
    Role: doc 07 s8.1 fallback line."""
    return no_ticket_clause(control.turn_id)


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
    Output: FinalAnswer. Side effects: none. Role: helper (`say` is the spoken form, agent.say)."""
    return FinalAnswer.model_validate({"text": text, "say": speak(say if say is not None else text), "citations": [],
                                       "escalated": escalated, "ticket": ticket})


class Escalator:
    """What: opens the turn's one code ticket and words the escalation outcomes. Use when: one per app (TurnRunner
    and StructuredInput build one from their core client). Inputs: agent-token core client.
    Output: open_ticket, emit, summary, finish, deadline, model_unavailable, ticket_offer. Side effects: see module.
    Role: escalation code (doc 07 s3.4 "agent code (origin code)" rows)."""

    def __init__(self, core: CoreAgentClient, *, holds: EscalationHolds | None = None) -> None:
        """What: keep the client and the hold store. Use when: app start or tests. Inputs: core client, optional
        EscalationHolds (None: an unverified escalation still asks for the card but nothing is held).
        Output: none. Side effects: none. Role: constructor."""
        self.core = core
        self.holds = holds

    async def open_ticket(self, control: TurnControl, reason: str, *, summary: str, category: str | None = None,
                          key_reason: str | None = None) -> dict[str, Any] | None:
        """What: one ticket attempt, clamped to min(3 s, time left) with no reserve (this IS the reserve), key
        {sid}:{turn}:esc:{reason}, origin code; reuses a ticket already opened this turn and makes no second attempt after a recorded failure
        (one attempt per turn).
        Use when: any code-owned escalation. Inputs: control, escalation reason, summary, category, key reason
        (defaults to reason). Output: TicketRef dict, or None on failure or when under 1 s is left.
        Side effects: one escalation step and at most one core call; sets control.ticket, or control.requester_refusal
        when core answers REQUESTER_UNKNOWN (D-070); always records the result on control.ticket_attempt (opened,
        failed or refused, CS-E-03.F12), the one fact the ticket clause of a line is chosen from. Role: s8.1 row."""
        if control.ticket is not None:
            control.ticket_attempt = "opened"
            return control.ticket
        if control.ticket_attempt == "failed":  # one attempt per turn: the in-loop hook already tried (review PR #87 M-1)
            return None
        step = Step.start(control.events, "escalation", "create_ticket", "Opening a ticket")
        timeout = clamp_timeout(limits.CORE_TICKET_TIMEOUT_S, control.remaining_s(), reserve_s=0.0)
        if timeout is None:
            step.done("skipped", "Couldn't open a ticket", detail="Not enough time left")
            control.ticket_attempt = "failed"
            return None
        body = {"category": category or REASON_CATEGORY.get(reason, "other"), "summary": summary[:500] or reason,
                "steps_tried": [], "blocking": False, "origin": "code", "escalation_reason": reason,
                "transcript_ref": control.session_id}
        try:
            res = await asyncio.wait_for(self.core.create_ticket(control, body, ticket_key(control, key_reason or reason)),
                                         timeout)
        except (ToolCallError, TimeoutError, asyncio.TimeoutError) as err:
            if isinstance(err, ToolCallError) and err.code == "REQUESTER_UNKNOWN":  # D-070: core needs a verified customer
                control.requester_refusal = dict(err.details or {})
                control.ticket_attempt = "refused"
                step.done("blocked", "Needs verification first", detail="A ticket needs a verified customer")
                return None
            step.done("timeout" if not isinstance(err, ToolCallError) or err.kind == "timeout" else "error",
                      "Couldn't open a ticket", detail="The ticket system did not respond")
            control.ticket_attempt = "failed"
            return None
        ticket = res.get("ticket") if isinstance(res, dict) else None
        if not isinstance(ticket, dict):
            step.done("error", "Couldn't open a ticket", detail="The ticket system gave no ticket")
            control.ticket_attempt = "failed"
            return None
        control.ticket, control.ticket_attempt = ticket, "opened"
        step.done("ok", "Opened a ticket", detail=str(ticket.get("display") or ""))
        return ticket

    async def no_requester(self, control: TurnControl, reason: str, *, summary: str,
                           category: str | None = None) -> NoRequester | None:
        """What: the D-070 decision after core refused the ticket for no verified requester: verification locked
        gives the "How to reach support" clause (nothing held); a card on screen (agent.sessions.awaiting_verification,
        or requested this turn) gives the card-pending clause (nothing held, the escalation does not fire while the
        customer is mid-verification);
        otherwise the escalation is held on agent.sessions and control.awaiting_verification is set (the caller
        emits verification.required with its full line, request_card).
        Use when: right after open_ticket returned None. Inputs: control, reason, summary, category.
        Output: NoRequester, or None when the ticket failed for another reason (the s8.1 fallback applies).
        Side effects: at most one read and one write of the hold store.
        Role: one place for the unverified escalation rule (route, limit, deadline and ticket_offer paths)."""
        refusal = control.requester_refusal
        if refusal is None:
            return None
        if refusal.get("verify_locked"):
            return NoRequester("unavailable", NO_REQUESTER_LINE, NO_REQUESTER_LINE)
        facts = await self.facts(control)
        if facts.awaiting or control.awaiting_verification:
            return NoRequester("card_pending", CARD_PENDING_LINE, CARD_PENDING_LINE)
        # prior_user_id binds the hold to the verification lifetime it was made in (PR #78 review I-3)
        held = {"reason": reason, "category": category or REASON_CATEGORY.get(reason, "other"),
                "summary": summary[:500] or reason, "turn_id": control.turn_id,
                "prior_user_id": facts.verified_user_id}
        await self.hold(control, held)
        control.awaiting_verification = True
        return NoRequester("hold", HOLD_LINE, HOLD_SAY)

    @staticmethod
    def request_card(control: TurnControl, decision: NoRequester, text: str) -> None:
        """What: verification.required reason action with the turn's full line as the prompt, for a hold only.
        Use when: after the caller worded its lead and clause. Inputs: control, decision, line. Output: none.
        Side effects: at most one event. Role: reuses the existing verification card (doc 07 s6.3)."""
        if decision.kind == "hold" and control.events is not None:
            control.events.emit("verification.required", reason="action", prompt_text=text,
                                attempts_left=control.verify_attempts_left, fields=["account_id", "pin"])

    async def facts(self, control: TurnControl) -> EscalationFacts:
        """What: the hold store's facts; empty facts without a store or on a failure (logged).
        Use when: no_requester, and verify_card before it records the new identity. Inputs: control.
        Output: EscalationFacts. Side effects: one read. Role: a store failure never fails the turn."""
        if self.holds is None:
            return EscalationFacts()
        try:
            return await self.holds.escalation_facts(control.session_id)
        except Exception:  # noqa: BLE001 - the reply still goes out; nothing is held
            log.warning("escalation facts read failed session_id=%s", control.session_id, exc_info=True)
            return EscalationFacts()

    @staticmethod
    def hold_is_for(held: dict[str, Any], before: EscalationFacts, user_id: str | None) -> bool:
        """What: true when a hold may become a ticket for the person who just verified: it was made in the
        session's current verification lifetime (its prior_user_id is still the session's verified user) and that
        lifetime was either never verified or belonged to this same person.
        Use when: verify_card success, with the facts read before the new identity is recorded. Inputs: hold,
        facts before, the newly verified user id. Output: bool. Side effects: none.
        Role: PR #78 review I-3: verify as A, expire, verify as B never files A's held escalation under B."""
        prior = held.get("prior_user_id")
        return prior == before.verified_user_id and prior in (None, user_id)

    async def hold(self, control: TurnControl, held: dict[str, Any] | None) -> None:
        """What: write or clear the held escalation (logged on failure). Use when: holding, after the held ticket,
        on lockout and on a forgotten PIN. Inputs: control, hold or None. Output: none. Side effects: one write.
        Role: helper."""
        if self.holds is None:
            return
        try:
            await self.holds.hold_escalation(control.session_id, held)
        except Exception:  # noqa: BLE001
            log.warning("escalation hold write failed session_id=%s", control.session_id, exc_info=True)

    async def held_ticket(self, control: TurnControl, held: dict[str, Any]) -> dict[str, Any] | None:
        """What: open the held escalation's ticket for the person just verified (one attempt, origin code, the held
        reason and summary), emit `escalated`, and clear the hold (also when the attempt failed, the s8.1 one-attempt
        rule). Use when: verify_card success after hold_is_for said yes. Inputs: control, hold.
        Output: the ticket or None. Side effects: at most one ticket, one event, one write.
        Role: D-070 "once verification completes it creates the ticket for that person"."""
        reason = str(held.get("reason") or "other")
        ticket = await self.open_ticket(control, reason, summary=str(held.get("summary") or reason),
                                        category=held.get("category"))
        await self.hold(control, None)
        if ticket is not None:
            self.emit(control, reason, ticket)
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

    @staticmethod
    def records(control: TurnControl, ticket: dict[str, Any] | None, reason: str) -> list[AIMessage]:
        """What: the code-written record of a ticket this module opened (D-071 part 2), for the same thread write as
        its line; nothing when no ticket was opened. Use when: every thread write after a code ticket.
        Inputs: control, ticket or None, reason. Output: [record] or []. Side effects: none. Role: helper."""
        return [ticket_record(control, ticket, reason)] if ticket else []

    @staticmethod
    def _line(control: TurnControl, text: str) -> str:
        """What: the thread form of a line: the spoken form in voice, the chat text otherwise (as the router does).
        Use when: thread writes. Inputs: control, text. Output: text. Side effects: none. Role: helper."""
        return speak(text) if control.mode == "voice" else text

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

    async def _messages(self, agent: Any, config: dict[str, Any], control: TurnControl) -> list[Any]:
        """What: the thread messages (empty on failure or timeout), the read bounded to min(2 s, time left) (doc 07
        s8.1 checkpointer) so a hung read cannot eat the reserve. Use when: summaries. Inputs: agent, config, control.
        Output: messages. Side effects: one read. Role: helper."""
        try:
            limit = max(min(limits.POSTGRES_CHECKPOINTER_TIMEOUT_S, control.remaining_s()), 0.0)
            state = await asyncio.wait_for(agent.aget_state(config), limit)
            return list((state.values or {}).get("messages") or [])
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
            decision: NoRequester | None = None
            if route.ticket is None:
                messages = await self._messages(agent, config, control)
                category = TOOL_CATEGORY.get(route.tool or "") if reason == "tool_failure" else None
                summary = self.summary(reason, messages)
                route.ticket = await self.open_ticket(control, reason, summary=summary, category=category)
                if route.ticket is None:
                    decision = await self.no_requester(control, reason, summary=summary, category=category)
            if decision is not None:  # D-070: unverified, so no ticket and no `escalated` event
                return await self._no_requester_outcome(stream, control, outcome, agent, config, decision,
                                                        lead=escalation_lead(control),
                                                        end_reason=reason if reason in ("deadline", "limit_reached")
                                                        else decision.end_reason)
            new = route_outcome(stream, control, outcome.usage, outcome.model_used)
            if reason == "deadline":
                new.status, new.error = "failed", deadline_error()
            self.emit(control, reason, route.ticket)
            line = new.final.say if control.mode == "voice" else new.final.text
            await self._thread(agent, config, control, {"messages": [line_ids.code_line(line_ids.ROUTE, control.turn_id, line,
                                                                                         control.identity),
                                                                     *self.records(control, route.ticket, reason)]})
            return new
        if outcome.end_reason == "limit_reached":
            messages = await self._messages(agent, config, control)
            summary = self.summary("limit_reached", messages)
            ticket = await self.open_ticket(control, "limit_reached", summary=summary)
            decision = None if ticket else await self.no_requester(control, "limit_reached", summary=summary)
            if decision is not None:
                control.route_message_id = line_ids.line_id(line_ids.LIMIT, control.turn_id)
                return await self._no_requester_outcome(stream, control, outcome, agent, config, decision,
                                                        lead=ESCALATION_LEADS["limit_reached"],
                                                        end_reason="limit_reached")
            text = f"{ESCALATION_LEADS['limit_reached']} " + (
                TICKET_CLAUSE.format(display=ticket.get("display") or f"ticket ending {ticket.get('short_id')}")
                if ticket else fallback_clause(control))
            self.emit(control, "limit_reached", ticket)
            await self._thread(agent, config, control, {"messages": [line_ids.code_line(line_ids.LIMIT, control.turn_id,
                                                                                  self._line(control, text),
                                                                                  control.identity),
                                                                     *self.records(control, ticket, "limit_reached")]})
            outcome.final = _final(text, escalated=True, ticket=ticket)
            return outcome
        if control.ticket is not None and outcome.final.ticket is None:
            outcome.final = FinalAnswer.model_validate({**outcome.final.model_dump(mode="json"), "escalated": True,
                                                        "ticket": control.ticket})
        return outcome

    async def _no_requester_outcome(self, stream: TurnStream, control: TurnControl, outcome: TurnOutcome, agent: Any,
                                    config: dict[str, Any], decision: NoRequester, *, lead: str,
                                    end_reason: str) -> TurnOutcome:
        """What: the reply of a route or limit escalation in an unverified session (D-070): lead plus the decision's
        clause, verification.required for a hold, no ticket and no `escalated` event; the thread line is replaced.
        Use when: finish, after no_requester decided. Inputs: stream, control, loop outcome, agent, config,
        decision, lead, end reason (deadline and limit_reached keep theirs). Output: TurnOutcome.
        Side effects: at most one event, one thread write. Role: helper."""
        text, say = f"{lead} {decision.clause}", f"{lead} {decision.say}"
        self.request_card(control, decision, text)
        # a deadline route already emitted its error event at the top of finish
        status, error = ("failed", deadline_error()) if end_reason == "deadline" else ("completed", None)
        new = TurnOutcome(final=_final(text, escalated=False, ticket=None, say=say), usage=outcome.usage,
                          end_reason=end_reason, status=status, error=error, model_used=outcome.model_used)
        msg_id = control.route_message_id or line_ids.line_id(line_ids.ROUTE, control.turn_id)
        await self._thread(agent, config, control, {"messages": [AIMessage(speak(say) if control.mode == "voice"
                                                                           else text, id=msg_id)]})
        return new

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
        messages = await self._messages(agent, config, control)
        summary = self.summary("deadline", messages)
        ticket = await self.open_ticket(control, "deadline", summary=summary)
        decision = None if ticket else await self.no_requester(control, "deadline", summary=summary)
        if decision is not None:  # D-070: unverified, so the escalation is held or pointed at the card
            text, say = f"{DEADLINE_LEAD} {decision.clause}", f"{DEADLINE_LEAD} {decision.say}"
            self.request_card(control, decision, text)
            repair = [not_run_tool_message(c) for c in dangling_calls(messages)]
            await self._thread(agent, config, control, {"messages": [
                *repair, line_ids.code_line(line_ids.DEADLINE, control.turn_id,
                                            speak(say) if control.mode == "voice" else text, control.identity)]})
            return TurnOutcome(final=_final(text, escalated=False, ticket=None, say=say), end_reason="deadline",
                               status="failed", error=err)
        text = (DEADLINE_TICKET_LINE.format(display=ticket.get("display") or f"ticket ending {ticket.get('short_id')}")
                if ticket else f"{DEADLINE_LEAD} {fallback_clause(control)}")
        self.emit(control, "deadline", ticket)
        repair = [not_run_tool_message(c) for c in dangling_calls(messages)]
        await self._thread(agent, config, control, {"messages": [*repair, line_ids.code_line(line_ids.DEADLINE, control.turn_id,
                                                                                        self._line(control, text),
                                                                                        control.identity),
                                                                 *self.records(control, ticket, "deadline")]})
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
            line_ids.code_line(line_ids.UNAVAILABLE, control.turn_id, self._line(control, MODEL_UNAVAILABLE_LINE),
                               control.identity)]})
        return TurnOutcome(final=_final(MODEL_UNAVAILABLE_LINE, escalated=True, ticket=None), end_reason="escalated")

    async def ticket_offer(self, control: TurnControl, messages: list[Any]) -> tuple[str, str, dict[str, Any] | None]:
        """What: the Create ticket button (or a voice strict yes) after the apology: one ticket, origin code,
        category other, reason model_unavailable, summary from the last user message, key
        {sid}:{turn}:esc:model_unavailable, no model call. Without the apology as the last AI message nothing is
        created. Use when: step 13. Inputs: control, thread messages. Output: (text, end_reason, ticket).
        Side effects: at most one ticket and one `escalated` event. Role: doc 07 s6.7 button flow."""
        if not apology_is_last(messages):
            return NOTHING_TO_OFFER_LINE, "answered", None
        summary = self.summary("model_unavailable", messages)
        ticket = await self.open_ticket(control, "model_unavailable", summary=summary)
        decision = None if ticket else await self.no_requester(control, "model_unavailable", summary=summary)
        if decision is not None:  # D-070: unverified, held until the card verifies the customer
            self.request_card(control, decision, decision.clause)
            return decision.clause, decision.end_reason, None
        self.emit(control, "model_unavailable", ticket)
        if ticket is None:
            return fallback_clause(control), "escalated", None
        return TICKET_OFFER_OPENED.format(short=ticket.get("short_id")), "escalated", ticket
