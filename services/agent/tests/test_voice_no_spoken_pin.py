"""What: snapshot of every voice-mode verification line plus the realtime and system prompts, asserting that none
of them asks the customer to say, tell or type a PIN; verification is by the secure card only.
Use when: pytest (no network, no model calls).
Inputs: the fixed line constants, REALTIME_PROMPT, render_instructions, SYSTEM_PROMPT, ASK_SUPPORT_AGENT_TOOL.
Output: pass or fail.
Side effects: none.
Role: guards doc 07 s14 row 18 fallback (card-only verification in voice; rule V spoken verification is designed
only, doc 09 s1.2), the review-pr61 finding I1.
"""
from __future__ import annotations

import re

import pytest
from contracts.tools import ASK_SUPPORT_AGENT_TOOL

from agent.middleware import router as router_mod
from agent.middleware.router import VERIFY_LINE, VERIFY_SAY, route_reply
from agent.prompt import SYSTEM_PROMPT
from agent.realtime import REALTIME_PROMPT, SessionSummary, render_instructions
from agent.structured import confirm, handler, verification, voice_yes
from agent.turn_control import Route, TurnControl

SID = "00000000-0000-4000-8000-000000000001"
TID = "00000000-0000-4000-8000-000000000002"

# Any of these verbs in a sentence that names a PIN or an account ID is an invitation to speak or hand it over.
SPOKEN_VERB = re.compile(r"\b(tell|say|saying|said|speak|speaking|spoken|read|give|repeat|aloud|out loud)\b", re.I)
SECRET = re.compile(r"\b(pin|account id)\b", re.I)
NEGATION = re.compile(r"\b(never|not|don't|do not|can't|cannot|no need)\b", re.I)
INVITES = ("tell me your", "say your", "speak your", "read out your", "read me your", "give me your",
           "type them into the chat", "type it into the chat", "or say them", "or say it")


def sentences(text: str) -> list[str]:
    """What: split text into sentences (also on bullets and line breaks).
    Use when: the sentence-level checks below. Inputs: text. Output: list of sentences. Side effects: none.
    Role: helper."""
    return [s.strip() for s in re.split(r"(?<=[.!?])\s+|\n+", text) if s.strip()]


def assert_no_spoken_pin_invite(text: str, *, allow_negated: bool) -> None:
    """What: fail if any sentence invites the customer to speak, tell or type a PIN or account ID.
    Use when: every line and prompt in this file. Inputs: text; allow_negated lets an instruction sentence
    forbid the thing ("never ask them to say one"). Output: none (asserts). Side effects: none. Role: the check."""
    low = text.lower()
    for phrase in INVITES:
        assert phrase not in low, f"invitation {phrase!r} in: {text}"
    for s in sentences(text):
        if SECRET.search(s) and SPOKEN_VERB.search(s):
            assert allow_negated and NEGATION.search(s), f"spoken PIN invitation: {s!r}"


def voice_verify_say() -> str:
    """What: the say a voice turn gets when the router ends it with route verify.
    Use when: the snapshot below. Inputs: none. Output: say text. Side effects: none. Role: helper."""
    control = TurnControl(session_id=SID, turn_id=TID, mode="voice")
    control.set_route(Route("verify", reason="account_lookup"))
    return route_reply(control).say


# Every line a voice turn can speak on the verification and step-up paths (say equals text where no say exists).
VOICE_VERIFICATION_LINES = {
    "router.VERIFY_SAY": VERIFY_SAY,
    "router.VERIFY_LINE": VERIFY_LINE,
    "router.first_verify_say": f"{router_mod.FIRST_VERIFY_LEAD} {VERIFY_SAY}",  # D-074 first message
    "confirm.STEP_UP_LINE": confirm.STEP_UP_LINE,
    "confirm.VERIFY_LAPSED_LINE": confirm.VERIFY_LAPSED_LINE,
    "handler.VERIFY_EXPIRED_LINE": handler.VERIFY_EXPIRED_LINE,
    "verification.FAILED_LINE": verification.FAILED_LINE.format(n=2, tries="tries"),
    "verification.LOCKED_LINE": verification.LOCKED_LINE,
    "verification.CONFLICT_LINE": verification.CONFLICT_LINE,
    "verification.UNAVAILABLE_LINE": verification.UNAVAILABLE_LINE,
    "voice_yes.REPROMPT_LINE": voice_yes.REPROMPT_LINE,
}


def test_voice_verify_say_snapshot_is_card_only() -> None:
    assert VERIFY_SAY == ("To look at your account I need to verify you. Please enter your account ID and support "
                          "PIN in the secure card on your screen. I can't take them by voice.")
    assert voice_verify_say() == VERIFY_SAY


@pytest.mark.parametrize("name", sorted(VOICE_VERIFICATION_LINES))
def test_no_voice_verification_line_asks_for_a_spoken_pin(name: str) -> None:
    assert_no_spoken_pin_invite(VOICE_VERIFICATION_LINES[name], allow_negated=False)


@pytest.mark.parametrize("text", [VERIFY_SAY, VERIFY_LINE, confirm.VERIFY_LAPSED_LINE, handler.VERIFY_EXPIRED_LINE])
def test_lines_that_ask_for_verification_point_at_the_card(text: str) -> None:
    assert "card" in text


def test_verify_say_constant_is_the_only_spoken_verify_line() -> None:
    # A new say constant in the router would bypass the snapshot above.
    says = [n for n in dir(router_mod) if n.endswith("_SAY")]
    assert says == ["VERIFY_SAY"]


def test_realtime_prompt_forbids_asking_for_a_pin_and_names_the_card() -> None:
    assert_no_spoken_pin_invite(REALTIME_PROMPT, allow_negated=True)
    low = REALTIME_PROMPT.lower()
    assert "never ask the customer for a pin" in low
    assert "secure card" in low
    assert "a pin)" not in low  # the customer is not expected to answer with a PIN


def test_realtime_instructions_with_summary_hold_no_spoken_pin_invite() -> None:
    summary = SessionSummary(verified_name=None, readback_say=None,
                             exchanges=[("customer", "Did my refund go through?"), ("assistant", VERIFY_SAY)])
    assert_no_spoken_pin_invite(render_instructions(summary), allow_negated=True)


def test_system_prompt_forbids_a_spoken_or_typed_pin() -> None:
    assert_no_spoken_pin_invite(SYSTEM_PROMPT, allow_negated=True)
    low = SYSTEM_PROMPT.lower()
    assert "say one aloud" in low and "secure card" in low


def test_ask_support_agent_tool_description_states_the_card_rule() -> None:
    # The Realtime model reads this description too (doc 07 s6.6), so it is model-facing instruction text.
    desc = ASK_SUPPORT_AGENT_TOOL["description"]
    assert_no_spoken_pin_invite(desc, allow_negated=True)
    low = desc.lower()
    assert "pin)" not in low and "account id, pin" not in low  # a PIN is not an expected spoken answer
    assert "secure card" in low and "never ask" in low
    arg = ASK_SUPPORT_AGENT_TOOL["parameters"]["properties"]["question"]["description"]
    assert_no_spoken_pin_invite(arg, allow_negated=True)


def test_the_checker_can_fail() -> None:
    # Proves the zero above is measurable: the old voice say must trip the check.
    old = ("To look at your account I need to verify you. Please tell me your account ID and your four-digit "
           "support PIN, or type them into the card on screen.")
    with pytest.raises(AssertionError):
        assert_no_spoken_pin_invite(old, allow_negated=False)
    with pytest.raises(AssertionError):
        assert_no_spoken_pin_invite("Please say your PIN now.", allow_negated=True)
