"""What: RouterMiddleware (doc 07 s2.5 item 1, s6.3 step 3, s6.5) and the fixed route lines: before each model
call it reads TurnControl.route and, when a route is set, ends the turn in code with a fixed AI message and
jump_to end, for verify(reason), readback, escalate(reason) and decline_off_topic.
Use when: first in the create_agent stack; the SSE adapter calls route_reply to build the turn's final and
end_reason from the same route.
Inputs: the bound TurnControl (route, mode, turn id); optional open_ticket hook (kept for callers that want the
ticket inside the loop; by default the escalation module, agent.escalation, opens the failure-path ticket AFTER the
loop, in the 3 s reserve, and replaces the line in the thread, CS-E-03.S4.T1).
Output: RouteReply, route_reply, route_message, escalate_from_hook, RouterMiddleware, the fixed line constants.
Side effects: writes one AI message (and, for readback, state.pending) to the thread; may call open_ticket.
Role: deterministic replies skip the model and Gate 2 (after_model hooks do not run after a jump, spike 1).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from langchain.agents.middleware import AgentMiddleware, hook_config
from langchain_core.messages import AIMessage, RemoveMessage

from agent.turn_control import Route, TurnControl, current_control

VERIFY_LINE = ("To look at your account I need to verify you. Please enter your account ID and support PIN in "
               "the secure card.")
VERIFY_SAY = ("To look at your account I need to verify you. Please tell me your account ID and your four-digit "
              "support PIN, or type them into the card on screen.")
DECLINE_OFF_TOPIC_LINE = ("I can only help with Quillstack: your account, billing, tickets and how the product "
                          "works. Is there something there I can help with?")
# Escalation leads per reason (doc 07 s6.5 lines; tool_failure from the S5 fixture); the ticket clause follows.
ESCALATION_LEADS: dict[str, str] = {
    "not_in_docs": "Our help articles don't cover that.",
    "retrieval_unavailable": "I couldn't search our help articles just now, so I won't guess.",
    "tool_failure": "Our systems didn't respond, so nothing was changed.",
    "grounding_failed": "I couldn't confirm that answer against our policies, so I won't guess.",
    "needs_commitment": "That needs a decision from our team, so I won't promise it here.",
    "limit_reached": "I couldn't finish that within the steps I'm allowed for one message.",
    "deadline": "I couldn't finish that in time.",
}
TOOL_FAILURE_LEADS: dict[str, str] = {
    "prepare_refund": "Our billing system didn't respond, so nothing was changed.",
    "list_charges": "Our billing system didn't respond, so I couldn't check your charges.",
    "get_account_summary": "Our account system didn't respond, so I couldn't check that.",
    "list_tickets": "Our ticket system didn't respond, so I couldn't check your tickets.",
    "create_ticket": "Our ticket system didn't respond.",
    "search_kb": "I couldn't search our help articles just now, so I won't guess.",
}
GENERIC_ESCALATION_LEAD = "I can't finish that here."
TICKET_CLAUSE = "I've opened a ticket for our team, {display}."
# doc 07 s8.1 failure-path row: on a failed (or not yet built) ticket the reply gives the turn id's last 4.
NO_TICKET_CLAUSE = "I couldn't open a ticket just now. If you contact us, please quote reference {ref}."

OpenTicket = Callable[[TurnControl], Awaitable[dict[str, Any] | None]]


@dataclass(frozen=True)
class RouteReply:
    """What: the deterministic reply for a route. Use when: the router writes the line; the adapter builds the
    final. Inputs: chat text, voice say, end reason, escalated flag, ticket, verification reason.
    Output: value. Side effects: none. Role: one source for the line and the end reason."""

    text: str
    say: str
    end_reason: str
    escalated: bool = False
    ticket: dict[str, Any] | None = None
    verification_reason: str | None = None


def _escalation_text(control: TurnControl) -> str:
    """What: lead plus ticket clause for an escalate route. Use when: route_reply. Inputs: control.
    Output: text. Side effects: none. Role: helper."""
    route = control.route
    assert route is not None
    lead = (TOOL_FAILURE_LEADS.get(route.tool or "") if route.reason == "tool_failure" else None) \
        or ESCALATION_LEADS.get(route.reason or "", GENERIC_ESCALATION_LEAD)
    ticket = route.ticket
    clause = (TICKET_CLAUSE.format(display=ticket.get("display") or f"ticket ending {ticket.get('short_id')}")
              if ticket else NO_TICKET_CLAUSE.format(ref=control.turn_id.replace("-", "")[-4:]))
    return f"{lead} {clause}"


def route_reply(control: TurnControl) -> RouteReply:
    """What: the fixed reply and end_reason for the control's route.
    Use when: RouterMiddleware writes the line; stream_agent_turn builds turn.completed.
    Inputs: control with a route. Output: RouteReply. Side effects: none.
    Role: end reasons verification_requested, readback, escalated, declined_off_topic (doc 07 s6.2 EndReason)."""
    route = control.route
    if route is None:
        raise ValueError("no route set")
    if route.kind == "verify":
        return RouteReply(VERIFY_LINE, VERIFY_SAY, "verification_requested", verification_reason=route.reason)
    if route.kind == "readback":
        rb = route.readback or {}
        return RouteReply(str(rb.get("chat", "")), str(rb.get("voice") or rb.get("chat", "")), "readback")
    if route.kind == "decline_off_topic":
        return RouteReply(DECLINE_OFF_TOPIC_LINE, DECLINE_OFF_TOPIC_LINE, "declined_off_topic")
    text = _escalation_text(control)
    end_reason = route.reason if route.reason in ("deadline", "limit_reached") else "escalated"
    return RouteReply(text, text, end_reason, escalated=True, ticket=route.ticket)


def route_message(control: TurnControl) -> AIMessage:
    """What: the thread AI message for the control's route (voice gets the say, chat the text), id route-{turn_id},
    recorded on control.route_message_id so the SSE adapter knows the turn ended on a route line.
    Use when: RouterMiddleware, and any hook that ends the turn in code (escalate_from_hook). Inputs: control with a
    route. Output: AIMessage. Side effects: sets control.route_message_id. Role: one builder for route lines."""
    reply = route_reply(control)
    msg = AIMessage(content=reply.say if control.mode == "voice" else reply.text, id=f"route-{control.turn_id}")
    control.route_message_id = msg.id
    return msg


def escalate_from_hook(control: TurnControl, reason: str, *, remove_id: str | None = None) -> dict[str, Any]:
    """What: end the turn from an after_model hook with escalate(reason): optionally remove the draft, write the
    route line, jump to end. The ticket is opened after the loop by agent.escalation (one per turn).
    Use when: Gate 2 (doc 07 s6.4): second consecutive grounding failure gives escalate(grounding_failed),
    commitment true gives escalate(needs_commitment); GroundingMiddleware (CS-E-02.S4.T2) returns this update.
    Inputs: control, escalation reason, the draft message id to remove. Output: state update with jump_to end.
    Side effects: sets the route (first decision wins) and route_message_id. Role: the Gate 2 escalation hook."""
    control.set_route(Route("escalate", reason=reason))
    messages: list[Any] = [RemoveMessage(id=remove_id)] if remove_id else []
    return {"messages": [*messages, route_message(control)], "jump_to": "end"}


class RouterMiddleware(AgentMiddleware):
    """What: ends the turn with the route's fixed line before the next model call. Use when: item 1 of the
    stack. Inputs: optional open_ticket hook. Output: None (no route) or {messages, [pending], jump_to: end}.
    Side effects: may call open_ticket; records the line's message id on TurnControl. Role: route slot."""

    def __init__(self, open_ticket: OpenTicket | None = None) -> None:
        """What: keep the ticket hook. Use when: build_middleware. Inputs: hook or None (CS-E-03.S4.T1 adds it).
        Output: none. Side effects: none. Role: constructor."""
        super().__init__()
        self.open_ticket = open_ticket

    @hook_config(can_jump_to=["end"])
    async def abefore_model(self, state: Any, runtime: Any) -> dict[str, Any] | None:
        """What: route check. Use when: before each model call. Inputs: state, runtime.
        Output: None, or the fixed AI message plus jump_to end (readback also sets state.pending).
        Side effects: see class. Role: doc 07 s2.5 item 1."""
        control = current_control()
        if control is None or control.route is None:
            return None
        route = control.route
        if route.kind == "escalate" and route.ticket is None and self.open_ticket is not None:
            route.ticket = await self.open_ticket(control)
        msg = route_message(control)
        update: dict[str, Any] = {"messages": [msg], "jump_to": "end"}
        if route.kind == "readback" and route.pending and route.pending.get("id"):
            update["pending"] = {"id": route.pending["id"], "kind": route.pending.get("action_type") or "refund",
                                 "readback_turn_id": control.turn_id, "readback_interrupted": False}
        return update
