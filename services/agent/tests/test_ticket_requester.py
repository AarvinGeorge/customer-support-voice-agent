"""What: D-070 in the agent: an escalation in an unverified session never opens a ticket. With no verification card
on screen the code says the fixed line, requests verification and holds the escalation on agent.sessions; when the
card verifies the customer, code opens the held ticket for that person, writes the line to the thread and clears the
hold. While the card is pending (agent.sessions.awaiting_verification) the escalation does not fire. A locked
session, a lockout and the card's "I don't have my PIN" press get the fixed "How to reach support" line and no ticket.
A hold is bound to the verification lifetime it was made in. create_ticket is not offered while unverified.
Use when: pytest (fake models, the contracts core stub over ASGI, in-memory checkpointer and hold store).
Inputs: none. Output: pass/fail. Side effects: none.
Role: CS-E-03.S3.T3 red tests (RCA build-log/reports/rca-unverified-ticket-2026-11-27.md fix B)."""
from __future__ import annotations

import json
from typing import Any

from contracts.stubs import examples as ex
from langchain_core.messages import AIMessage, HumanMessage

from agent.escalation import CARD_PENDING_LINE, HOLD_LINE, NO_REQUESTER_LINE, EscalationFacts
from agent.middleware.router import ESCALATION_LEADS
from agent.middleware.visibility import visible_tool_names
from agent.structured.verification import LOCKED_LINE, NO_PIN_LINE
from escalation_world import OWNER, SID, World, of
from test_escalation import call, routed_search

LEAD = ESCALATION_LEADS["not_in_docs"]
QUESTION = "do you sign a HIPAA BAA?"


class MemoryHolds:
    """What: in-memory EscalationHolds (agent.sessions awaiting_verification, held_escalation, verified_user_id).
    Use when: tests. Inputs: awaiting flag, the session's verified user. Output: escalation_facts, hold_escalation.
    Side effects: records writes. Role: fake of PgSessionFlags' hold methods."""

    def __init__(self, awaiting: bool = False, verified_user_id: str | None = None) -> None:
        """What: start empty. Use when: arranging. Inputs: awaiting, verified user. Output: none. Side effects: none.
        Role: constructor."""
        self.awaiting, self.verified_user_id = awaiting, verified_user_id
        self.held: dict[str, Any] | None = None
        self.writes: list[dict[str, Any] | None] = []

    async def escalation_facts(self, session_id: str) -> EscalationFacts:
        """What: the session's facts. Use when: the escalator. Inputs: session id. Output: EscalationFacts.
        Side effects: none. Role: fake read."""
        return EscalationFacts(awaiting=self.awaiting, held=self.held, verified_user_id=self.verified_user_id)

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
    assert holds.held["prior_user_id"] is None  # made in a never-verified session
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
    assert isinstance(messages[-3], HumanMessage) and messages[-2].content == outcome.final.text  # the line
    assert str(messages[-1].id).startswith("record-")  # then the code record of the ticket (D-071 part 2)


async def test_no_escalation_fires_while_the_verification_card_is_pending() -> None:
    holds = MemoryHolds(awaiting=True)  # the card is on screen: agent.sessions.awaiting_verification
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


async def test_an_old_verification_turn_does_not_suppress_a_new_card() -> None:
    """Review I-2: only the explicit awaiting flag means "card pending"; with it cleared (verified, dismissed or the
    topic changed) a new escalation holds and shows the card again, whatever older turns asked for."""
    holds = MemoryHolds(awaiting=False)
    w = World([call("request_verification", {"reason": "action"}, "v1"),
               call("search_kb", {"query": "hipaa baa"})],
              tools={"search_kb": routed_search("not_in_docs")}, verified=False, holds=holds)
    first, _ = await w.send(text="refund please")
    assert first.end_reason == "verification_requested"
    holds.awaiting = False  # the customer moved on (doc 07 s7.3 topic change clears the flag)
    outcome, events = await w.send(text=QUESTION)
    assert outcome.final.text == f"{LEAD} {HOLD_LINE}" and holds.held is not None
    assert [e.reason for e in of(events, "verification.required")] == ["action"]


async def test_the_card_no_pin_press_gives_the_reach_support_line_with_no_model_call_and_no_ticket() -> None:
    """Review I-4: an explicit structured input from the card, never text matching on customer messages."""
    holds = MemoryHolds(awaiting=True)
    holds.held = {"reason": "not_in_docs", "category": "other", "summary": "s", "turn_id": "t", "prior_user_id": None}
    w = World([AIMessage("model must not run")], verified=False, holds=holds)
    outcome, events = await w.send(verification={"no_pin": True})
    assert outcome.final.text == NO_PIN_LINE and outcome.end_reason == "answered"
    assert w.transport.tickets == [] and holds.held is None and of(events, "escalated") == []
    assert [e for e in of(events, "step.started") if e.kind == "model"] == []
    assert w.stub is not None and [c for c in w.stub.state.calls if c.path.endswith("/identity/verify")] == []
    messages = (await w.values())["messages"]
    assert messages[-2].content == "[customer pressed I don't have my PIN]" and messages[-1].content == NO_PIN_LINE


async def test_pin_talk_in_text_goes_to_the_model_as_usual() -> None:
    """Review I-4 regression: "I forgot my PIN" typed in chat is an ordinary message (grounded answer path)."""
    w = World([AIMessage("You can verify in the secure card; the article explains what to do without a PIN.")],
              verified=False, holds=MemoryHolds())
    outcome, events = await w.send(text="I forgot my PIN")
    assert outcome.final.text != NO_PIN_LINE
    assert [e for e in of(events, "step.started") if e.kind == "model"] != []


async def test_a_hold_from_another_verification_lifetime_never_becomes_a_ticket_for_someone_else() -> None:
    """Review I-3 probe: verified as A, expired, escalation held, verify as B: no ticket for B, hold dropped."""
    holds = MemoryHolds(verified_user_id="U-AAAA")  # the session belonged to A before it expired
    w = World([call("search_kb", {"query": "hipaa baa"}), AIMessage("Thanks, how can I help?")],
              tools={"search_kb": routed_search("not_in_docs")}, verified=False, holds=holds)
    await w.send(text=QUESTION)
    assert holds.held is not None and holds.held["prior_user_id"] == "U-AAAA"
    refused = len(w.transport.tickets)
    outcome, events = await w.send(verification={"account_id": ex.ACCOUNT_ID, "pin": ex.STUB_PIN})  # B (the stub)
    assert w.transport.tickets[refused:] == [] and of(events, "escalated") == []
    assert holds.held is None and outcome.final.ticket is None


async def test_expiry_of_the_verification_clears_the_hold() -> None:
    """Review I-3: any change of verified identity, expiry included, clears the hold."""
    holds = MemoryHolds(verified_user_id=ex.CLAIMS.user_id)
    holds.held = {"reason": "not_in_docs", "category": "other", "summary": "s", "turn_id": "t",
                  "prior_user_id": ex.CLAIMS.user_id}
    w = World([AIMessage("Please verify again.")], verified=False, holds=holds)  # core says SESSION_NOT_VERIFIED
    await w.seed({"messages": [], **OWNER})
    await w.send(text="hello again")
    assert holds.held is None


async def test_the_held_ticket_opened_after_verification_gets_its_code_record_bound_to_that_person() -> None:
    # D-071 part 2 with D-070: the ticket the code opens after the card succeeds is written to the thread as a code
    # record, stamped with the verification it was opened under, so the model and Gate 2 know it exists.
    from agent.gates import identity_key
    from agent.records import RECORD_ID_PREFIX, session_tickets

    holds = MemoryHolds()
    w = World([call("search_kb", {"query": "hipaa baa"})], tools={"search_kb": routed_search("not_in_docs")},
              verified=False, holds=holds)
    await w.send(text=QUESTION)
    outcome, _ = await w.send(verification={"account_id": ex.ACCOUNT_ID, "pin": ex.STUB_PIN})
    values = await w.values()
    records = [m for m in values["messages"] if str(m.id or "").startswith(RECORD_ID_PREFIX)]
    assert len(records) == 1 and records[0].response_metadata["qs_action"]["kind"] == "ticket"
    assert "ticket ending 8213" in str(records[0].content)
    key = identity_key(values)
    assert key is not None and records[0].response_metadata["bound_to"] == key
    line = [m for m in values["messages"] if m.content == outcome.final.text][-1]
    assert line.response_metadata["bound_to"] == key  # the line is written under the new verification (N3)
    assert session_tickets(values["messages"], key) == ["ticket ending 8213"]
