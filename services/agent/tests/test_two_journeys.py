"""What: CS-E-03.S1.T9 agent half. D-074: on the first customer message of an unverified session code always offers
the verification card; the model decides only whether the message needs account data (request_verification) and
phrases its answer. D-075: one identity per conversation; verifying as a different person in a verified session gets
the fixed line and changes nothing, re-verifying as the same person is allowed.
Use when: pytest (fake models, the contracts core stub over ASGI, in-memory checkpointer).
Inputs: none. Output: pass/fail. Side effects: none.
Role: red tests first for D-074 and D-075 in the agent (core's half is tests/pg/test_identity_pg.py)."""
from __future__ import annotations

from typing import Any

import httpx
from contracts.stubs import examples as ex
from langchain_core.messages import AIMessage

from agent.first_turn import OFFER_PROMPT
from agent.middleware.router import FIRST_VERIFY_LEAD, VERIFY_LINE, VERIFY_SAY
from agent.structured.verification import CONFLICT_LINE
from escalation_world import OWNER, SID, World, of
from test_escalation import call

DIFFERENT_ACCOUNT_LINE = "To help a different account, please start a new conversation."


class Flags:
    """What: records agent.sessions.awaiting_verification writes. Use when: asserting the card is on record.
    Inputs: none. Output: set_awaiting_verification. Side effects: records. Role: fake SessionFlags."""

    def __init__(self) -> None:
        """What: start empty. Use when: arranging. Inputs: none. Output: none. Side effects: none. Role: constructor."""
        self.writes: list[tuple[str, bool]] = []

    async def set_awaiting_verification(self, session_id: str, awaiting: bool,
                                        verified_user_id: str | None = None) -> None:
        """What: record a write. Use when: the runner marks the card on screen. Inputs: id, flag, user.
        Output: none. Side effects: records. Role: fake write."""
        self.writes.append((session_id, awaiting))


def types(events: list[Any]) -> list[str]:
    """What: event types in order. Use when: asserting order. Inputs: events. Output: list. Side effects: none.
    Role: helper."""
    return [e.type for e in events]


# D-074 (1): account help waits for verification; code writes the greeting and the card ask.

async def test_first_message_that_needs_account_data_greets_and_asks_for_the_card() -> None:
    w = World([call("request_verification", {"reason": "account_lookup"})], verified=False)
    outcome, events = await w.send(text="Can you check my last invoice?")
    assert outcome.end_reason == "verification_requested"
    assert outcome.final.text == f"{FIRST_VERIFY_LEAD} {VERIFY_LINE}"
    assert FIRST_VERIFY_LEAD.startswith("Hi") and "confirm it's you first" in FIRST_VERIFY_LEAD
    (req,) = of(events, "verification.required")
    assert req.reason == "account_lookup" and req.prompt_text == outcome.final.text
    assert (await w.values())["messages"][-1].content == outcome.final.text  # the thread says what the customer saw


async def test_first_voice_message_that_needs_account_data_says_the_greeting_and_the_card() -> None:
    w = World([call("request_verification", {"reason": "account_lookup"})], verified=False)
    outcome, events = await w.send(mode="voice", text="Can you check my last invoice?")
    assert outcome.final.say.startswith(FIRST_VERIFY_LEAD) and "on your screen" in outcome.final.say
    assert VERIFY_SAY.split(". ")[-1] in outcome.final.say  # "I can't take them by voice."
    assert len(of(events, "verification.required")) == 1


async def test_a_later_message_that_needs_account_data_keeps_the_plain_verify_line() -> None:
    w = World([call("request_verification", {"reason": "account_lookup"})], verified=False, user_turns=1)
    outcome, _ = await w.send(text="Can you check my last invoice?")
    assert outcome.final.text == VERIFY_LINE


# D-074 (2): a general question is answered AND the card is offered, non-blocking.

async def test_first_general_question_is_answered_and_code_offers_the_card() -> None:
    flags = Flags()
    w = World([AIMessage(content="SSO is set up under Settings.")], verified=False, flags=flags)
    outcome, events = await w.send(text="How do I set up SSO?")
    assert outcome.end_reason == "answered" and outcome.final.text == "SSO is set up under Settings."
    (req,) = of(events, "verification.required")
    assert req.reason == "account_lookup" and req.prompt_text == OFFER_PROMPT and req.fields == ["account_id", "pin"]
    order = types(events)
    assert order.index("verification.required") < order.index("turn.completed")
    assert flags.writes == [(SID, True)]  # agent.sessions records the card on screen (Gate 2 session facts read it)


async def test_first_greeting_is_answered_and_code_offers_the_card() -> None:
    w = World([AIMessage(content="Hello! How can I help today?")], verified=False)
    outcome, events = await w.send(text="hi")
    assert outcome.final.text == "Hello! How can I help today?"
    assert len(of(events, "verification.required")) == 1


async def test_first_voice_question_also_gets_the_card_on_screen() -> None:
    w = World([AIMessage(content="SSO is set up under Settings.")], verified=False)
    _, events = await w.send(mode="voice", text="How do I set up SSO?")
    assert len(of(events, "verification.required")) == 1


async def test_the_offer_is_only_on_the_first_message() -> None:
    w = World([AIMessage(content="SSO is set up under Settings.")], verified=False, user_turns=1)
    _, events = await w.send(text="How do I set up SSO?")
    assert of(events, "verification.required") == []


async def test_a_verified_session_never_gets_the_card() -> None:
    w = World([AIMessage(content="SSO is set up under Settings.")], verified=True)
    await w.seed(OWNER)
    outcome, events = await w.send(text="How do I set up SSO?")
    assert outcome.end_reason == "answered" and of(events, "verification.required") == []


async def test_the_offer_never_doubles_a_card_the_turn_already_asked_for() -> None:
    w = World([call("request_verification", {"reason": "action"})], verified=False)
    _, events = await w.send(text="I want a refund")
    assert len(of(events, "verification.required")) == 1


# D-075: one identity per conversation, enforced where the agent binds the claim.

def other_person(request: httpx.Request) -> httpx.Response:
    """What: core verifies someone else (a core without the D-075 check). Use when: the agent's own bind guard.
    Inputs: request. Output: 200 Claims for another user and account. Side effects: none. Role: override."""
    claims = ex.CLAIMS.model_dump(mode="json") | {"user_id": "U-0999", "account_id": "QS-99999",
                                                    "display_name": "Someone Else"}
    return httpx.Response(200, json=claims)


def identity_conflict(request: httpx.Request) -> httpx.Response:
    """What: core refuses a second identity. Use when: the D-075 line. Inputs: request. Output: 409.
    Side effects: none. Role: override."""
    return httpx.Response(409, json={"error": {"class": "core_conflict", "code": "IDENTITY_CONFLICT", "message": "x",
                                               "retryable": False, "changed": False, "fault_injected": False,
                                               "details": {}}})


def test_the_conflict_line_is_the_d075_line() -> None:
    assert CONFLICT_LINE == DIFFERENT_ACCOUNT_LINE


async def test_verifying_as_a_different_account_gets_the_fixed_line_from_core() -> None:
    w = World([], verified=True, overrides={("POST", "/v1/identity/verify"): identity_conflict})
    await w.seed(OWNER)
    outcome, events = await w.send(verification={"account_id": "QS-99999", "pin": "1357"})
    assert outcome.final.text == DIFFERENT_ACCOUNT_LINE and outcome.end_reason == "answered"
    assert [e.status for e in of(events, "verification.result")] == ["conflict"]
    assert (await w.values())["claims_snapshot"]["user_id"] == ex.CLAIMS.user_id


async def test_the_agent_never_rebinds_a_verified_thread_to_another_person() -> None:
    w = World([], verified=True, overrides={("POST", "/v1/identity/verify"): other_person})
    await w.seed(OWNER)
    outcome, events = await w.send(verification={"account_id": "QS-99999", "pin": "1357"})
    assert outcome.final.text == DIFFERENT_ACCOUNT_LINE
    assert [e.status for e in of(events, "verification.result")] == ["conflict"]
    values = await w.values()
    assert values["claims_snapshot"]["user_id"] == ex.CLAIMS.user_id  # identity unchanged
    assert values["claims_snapshot"]["account_id"] == ex.ACCOUNT_ID


async def test_re_verifying_as_the_same_person_is_allowed() -> None:
    w = World([AIMessage(content="You're verified.")], verified=True)
    await w.seed(OWNER)
    outcome, events = await w.send(verification={"account_id": ex.ACCOUNT_ID, "pin": ex.STUB_PIN})
    assert [e.status for e in of(events, "verification.result")] == ["verified"]
    assert outcome.final.text != DIFFERENT_ACCOUNT_LINE
    assert (await w.values())["claims_snapshot"]["user_id"] == ex.CLAIMS.user_id
