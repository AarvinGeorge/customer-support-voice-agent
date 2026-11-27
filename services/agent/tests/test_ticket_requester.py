"""What: D-070 in the agent: an escalation in an unverified session never opens a ticket. With no verification card
on screen the code says the fixed line, requests verification and holds the escalation on agent.sessions; when the
card verifies the customer, code opens the held ticket for that person, writes the line to the thread and clears the
hold. While the card is already pending the escalation does not fire. A locked session, a lockout and a forgotten
PIN get the fixed "How to reach support" line and no ticket. create_ticket is not offered while unverified.
Use when: pytest (fake models, the contracts core stub over ASGI, in-memory checkpointer and hold store).
Inputs: none. Output: pass/fail. Side effects: none.
Role: CS-E-03.S3.T3 red tests (RCA build-log/reports/rca-unverified-ticket-2026-11-27.md fix B)."""
from __future__ import annotations

import json
from typing import Any

from contracts.stubs import examples as ex
from langchain_core.messages import AIMessage, HumanMessage

from agent.escalation import CARD_PENDING_LINE, HOLD_LINE, NO_REQUESTER_LINE, says_pin_is_unavailable
from agent.middleware.router import ESCALATION_LEADS
from agent.middleware.visibility import visible_tool_names
from agent.structured.verification import LOCKED_LINE
from escalation_world import SID, World, of
from test_escalation import call, routed_search

LEAD = ESCALATION_LEADS["not_in_docs"]
QUESTION = "do you sign a HIPAA BAA?"


class MemoryHolds:
    """What: in-memory EscalationHolds (agent.sessions.held_escalation plus "a card was shown"). Use when: tests.
    Inputs: card_shown flag. Output: escalation_facts, hold_escalation. Side effects: records writes.
    Role: fake of PgSessionFlags' hold methods."""

    def __init__(self, card_shown: bool = False) -> None:
        """What: start empty. Use when: arranging. Inputs: card_shown. Output: none. Side effects: none.
        Role: constructor."""
        self.card_shown = card_shown
        self.held: dict[str, Any] | None = None
        self.writes: list[dict[str, Any] | None] = []

    async def escalation_facts(self, session_id: str) -> tuple[bool, dict[str, Any] | None]:
        """What: (card shown earlier, held escalation). Use when: the escalator. Inputs: session id.
        Output: pair. Side effects: none. Role: fake read."""
        return self.card_shown, self.held

    async def hold_escalation(self, session_id: str, held: dict[str, Any] | None) -> None:
        """What: set or clear the hold. Use when: the escalator. Inputs: session id, hold or None. Output: none.
        Side effects: records. Role: fake write."""
        self.held = held
        self.writes.append(held)


def created(w: World) -> list[dict[str, Any]]:
    """What: bodies of ticket POSTs core accepted (the stub refuses unverified sessions). Use when: asserting.
    Inputs: world. Output: list of bodies. Side effects: none. Role: helper."""
    assert w.stub is not None
    return [json.loads(r.content) for r in w.transport.tickets if r.headers.get("x-session-id") in w.stub.state.verified]


def test_create_ticket_is_not_offered_while_unverified() -> None:
    assert visible_tool_names({"verified": False}) == ("request_verification", "search_kb")
    assert "create_ticket" in visible_tool_names({"verified": True, "claims_snapshot": {"role": "member"}})


async def test_unverified_escalation_holds_and_requests_verification_with_no_ticket() -> None:
    holds = MemoryHolds()
    w = World([call("search_kb", {"query": "hipaa baa"})], tools={"search_kb": routed_search("not_in_docs")},
              verified=False, holds=holds)
    outcome, events = await w.send(text=QUESTION)
    assert created(w) == []
    assert outcome.end_reason == "verification_requested"
    assert outcome.final.text == f"{LEAD} {HOLD_LINE}"
    assert outcome.final.escalated is False and outcome.final.ticket is None
    assert of(events, "escalated") == []
    (req,) = of(events, "verification.required")
    assert req.reason == "action" and req.prompt_text == outcome.final.text
    assert holds.held is not None and holds.held["reason"] == "not_in_docs" and QUESTION in holds.held["summary"]
    assert (await w.values())["messages"][-1].content == outcome.final.text  # the thread says what the customer saw


async def test_verifying_opens_the_held_ticket_for_that_person_and_clears_the_hold() -> None:
    holds = MemoryHolds()
    w = World([call("search_kb", {"query": "hipaa baa"})], tools={"search_kb": routed_search("not_in_docs")},
              verified=False, holds=holds)
    await w.send(text=QUESTION)
    assert created(w) == []
    refused = len(w.transport.tickets)  # the escalation's attempt that core refused (no requester)
    outcome, events = await w.send(verification={"account_id": ex.ACCOUNT_ID, "pin": ex.STUB_PIN})
    (body,) = [json.loads(r.content) for r in w.transport.tickets[refused:]]
    assert body["escalation_reason"] == "not_in_docs" and body["origin"] == "code" and QUESTION in body["summary"]
    assert [(e.reason, e.origin) for e in of(events, "escalated")] == [("not_in_docs", "code")]
    assert of(events, "verification.result")[0].status == "verified"
    assert outcome.end_reason == "escalated" and outcome.final.ticket.short_id == "8213"
    assert outcome.final.text == f"Thanks, {ex.CLAIMS.display_name}. I've opened a ticket for our team, ticket ending 8213."
    assert holds.held is None
    messages = (await w.values())["messages"]
    assert isinstance(messages[-2], HumanMessage) and messages[-1].content == outcome.final.text  # code record


async def test_no_escalation_fires_while_the_verification_card_is_pending() -> None:
    holds = MemoryHolds(card_shown=True)
    w = World([call("search_kb", {"query": "okay"})], tools={"search_kb": routed_search("grounding_failed")},
              verified=False, holds=holds)
    outcome, events = await w.send(text="Okay.")
    assert created(w) == [] and holds.writes == []
    assert outcome.final.text == f"{ESCALATION_LEADS['grounding_failed']} {CARD_PENDING_LINE}"
    assert outcome.end_reason == "verification_requested" and outcome.final.escalated is False
    assert of(events, "escalated") == []


async def test_locked_session_gets_the_reach_support_line_and_no_hold() -> None:
    holds = MemoryHolds()
    w = World([call("search_kb", {"query": "hipaa baa"})], tools={"search_kb": routed_search("not_in_docs")},
              verified=False, holds=holds)
    assert w.stub is not None
    w.stub.state.failed[SID] = 3  # the stub session is locked (core says verify_locked)
    outcome, events = await w.send(text=QUESTION)
    assert created(w) == [] and holds.held is None
    assert outcome.final.text == f"{LEAD} {NO_REQUESTER_LINE}"
    assert of(events, "verification.required") == [] and of(events, "escalated") == []


async def test_lockout_on_the_card_says_reach_support_clears_the_hold_and_opens_nothing() -> None:
    holds = MemoryHolds()
    holds.held = {"reason": "not_in_docs", "category": "other", "summary": "s", "turn_id": "t"}
    w = World(verified=False, holds=holds)
    for _ in range(2):
        await w.send(verification={"account_id": ex.ACCOUNT_ID, "pin": "0000"})
    outcome, events = await w.send(verification={"account_id": ex.ACCOUNT_ID, "pin": "0000"})
    assert outcome.final.text == LOCKED_LINE and outcome.final.ticket is None and outcome.final.escalated is False
    assert of(events, "escalated") == [] and of(events, "verification.result")[0].status == "locked"
    assert w.transport.tickets == [] and holds.held is None


async def test_forgotten_pin_gets_the_fixed_line_with_no_model_call_and_no_ticket() -> None:
    holds = MemoryHolds(card_shown=True)
    holds.held = {"reason": "not_in_docs", "category": "other", "summary": "s", "turn_id": "t"}
    w = World([AIMessage("model must not run")], verified=False, holds=holds)
    outcome, events = await w.send(text="I forgot my PIN")
    assert outcome.final.text == NO_REQUESTER_LINE and outcome.end_reason == "answered"
    assert w.transport.tickets == [] and holds.held is None and of(events, "escalated") == []
    assert [e for e in of(events, "step.started") if e.kind == "model"] == []


def test_pin_unavailable_phrases() -> None:
    for text in ("I forgot my PIN", "i don't remember my support pin", "I lost my PIN, what now?",
                 "How do I reset my PIN?", "I don't have a PIN"):
        assert says_pin_is_unavailable(text), text
    for text in ("my PIN is 4821", "where do I enter my PIN", "I forgot my password", "what is a PIN for?"):
        assert not says_pin_is_unavailable(text), text
