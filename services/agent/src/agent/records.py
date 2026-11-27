"""What: code-written records of code-side actions (D-071 part 2): when our code, not the model, takes an action
(opens an escalation ticket, commits or submits a confirmed change), one AI message "[System record] ..." with the
structured facts in response_metadata["qs_action"] goes into the thread, so the model's history matches what the
system did; and session_tickets, the tickets opened in the session as the thread records them.
Use when: every code path that acts (agent.escalation, the step 13 handler, and any later path such as the held
escalation of D-070) calls action_record or ticket_record and writes the message with its own thread write.
Inputs: the turn's TurnControl, the action kind, a plain sentence, the facts.
Output: RECORD_ID_PREFIX, action_record, ticket_record, change_record, session_tickets (bound to the verification).
Side effects: none (builders only; the caller writes the message).
Role: one generic writer; ids are "record-{turn_id}-{kind}", so a repeated write of the same record replaces it.
response_metadata is checkpointed and never sent to a model; the content is what the model reads.
"""
from __future__ import annotations

from typing import Any

from langchain_core.messages import AIMessage, BaseMessage, ToolMessage

from agent import line_ids
from agent.turn_control import TurnControl

RECORD_ID_PREFIX = line_ids.RECORD
RECORD_LEAD = "[System record]"


def action_record(control: TurnControl, kind: str, text: str, **facts: Any) -> AIMessage:
    """What: one code-written record of an action the system took. Use when: any code-side action.
    Inputs: control (turn id), kind ("ticket", "commit", "submit", ...), a plain sentence, the facts (JSON-safe).
    Output: AIMessage(content "[System record] <text>", id record-{turn_id}-{kind}, response_metadata qs_action and
    bound_to, the verification the action ran under: control.identity, review PR #71 N3).
    Side effects: none. Role: the generic writer (D-071 part 2)."""
    return AIMessage(f"{RECORD_LEAD} {text}", id=f"{RECORD_ID_PREFIX}{control.turn_id}-{kind}",
                     response_metadata={"qs_action": {"kind": kind, **facts}, "bound_to": control.identity})


def ticket_record(control: TurnControl, ticket: dict[str, Any], reason: str) -> AIMessage:
    """What: the record of a ticket our code opened. Use when: an escalation ticket exists (route, limit, deadline,
    ticket offer). Inputs: control, the TicketRef dict, the escalation reason. Output: AIMessage.
    Side effects: none. Role: D-071 part 2 (RCA fix D)."""
    display = str(ticket.get("display") or f"ticket ending {ticket.get('short_id')}")
    return action_record(control, "ticket",
                         f"Our system opened {display} for the Quillstack support team (reason: "
                         f"{reason.replace('_', ' ')}). It exists; refer to it if the customer asks.",
                         ticket_id=ticket.get("ticket_id"), display=display, reason=reason)


def change_record(control: TurnControl, kind: str, text: str, *, pending_action_id: Any, outcome: str,
                  ticket: dict[str, Any] | None = None) -> AIMessage:
    """What: the record of a change our code carried out on the customer's yes (commit) or sent for approval
    (submit). Use when: the step 13 handler after the Confirmer. Inputs: control, kind, the result sentence, the
    pending action id, the outcome, the approval ticket. Output: AIMessage. Side effects: none.
    Role: D-071 part 2."""
    facts: dict[str, Any] = {"pending_action_id": pending_action_id, "outcome": outcome}
    if ticket:
        facts["display"] = str(ticket.get("display") or "")
        facts["ticket_id"] = ticket.get("ticket_id")
    return action_record(control, kind, f"Our system did this on the customer's yes: {text}", **facts)


def session_tickets(messages: list[BaseMessage], key: str | None) -> list[str]:
    """What: the display ids of the tickets opened in this session under the current verification, oldest first,
    each once: from code records (ticket, or a submit with its approval ticket) and from create_ticket results
    (ToolGuard's code-held artifact), each only when its bound_to stamp equals key (review PR #71 N3: tickets
    opened as another account, or before a lapse, are not listed).
    Use when: Gate 2 session_state. Inputs: thread messages, the current identity key. Output: list of displays.
    Side effects: none. Role: code-held facts only (never model or customer text)."""
    out: list[str] = []
    for m in messages:
        display, bound = None, _MISSING
        if isinstance(m, AIMessage) and str(m.id or "").startswith(RECORD_ID_PREFIX):
            meta = m.response_metadata or {}
            display, bound = (meta.get("qs_action") or {}).get("display"), meta.get("bound_to", _MISSING)
        elif isinstance(m, ToolMessage) and m.name == "create_ticket" and isinstance(m.artifact, dict):
            ticket = m.artifact.get("ticket")
            display = ticket.get("display") if isinstance(ticket, dict) else None
            bound = m.artifact.get("bound_to", _MISSING)
        if display and bound is not _MISSING and bound == key and display not in out:
            out.append(str(display))
    return out


_MISSING = object()
