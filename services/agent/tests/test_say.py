"""What: the `say` builder (doc 07 s6.2 FinalAnswer.say, s3.4 TicketRef): unit cases for markdown, ids, amounts,
dates and numbers; a snapshot of the spoken form of EVERY fixed line in the agent (no markdown, no raw id, no digit);
and end to end that voice finals carry a speakable say.
Use when: pytest. Set UPDATE_SAY_SNAPSHOT=1 to rewrite tests/snapshots/say_fixed_lines.txt after a deliberate change.
Inputs: none. Output: pass/fail. Side effects: none (writes the snapshot only when asked).
Role: Done when of CS-E-05.S2.T1."""
from __future__ import annotations

import os
import re
from pathlib import Path

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from agent import escalation, ingress
from agent.middleware import router
from agent.say import speak
from agent.structured import confirm, handler, verification, voice_yes
from escalation_world import OWNER, Boom, World, error_response

SNAPSHOT = Path(__file__).parent / "snapshots" / "say_fixed_lines.txt"
UNSPEAKABLE = re.compile(r"[*_`#\[\]<>|$%]|\d|\b[A-Z]{2,4}-[0-9A-F]{4,}\b|https?://")
# Lines that only go to the thread for the model to read (never shown or spoken to the customer).
THREAD_ONLY = (verification.VERIFIED_THREAD_LINE, verification.CARD_SUBMITTED_LINE, verification.NO_PIN_PRESSED)
DISPLAY, SHORT, REF = "ticket ending 8213", "8213", "0a1f"


@pytest.mark.parametrize("raw,spoken", [
    ("$96.00", "ninety-six dollars"),
    ("$96.50", "ninety-six dollars and fifty cents"),
    ("$1,200.00", "one thousand two hundred dollars"),
    ("$0.99", "ninety-nine cents"),
    ("ticket ending 8213", "ticket ending eight two one three"),
    ("Ticket TCK-26118213 is open", "Ticket ending eight two one three is open"),
    ("charge CH-26112102", "charge ending two one zero two"),
    ("request PA-3F2A9C1B", "request ending nine C one B"),
    ("quote reference 0a1f.", "quote reference zero A one F."),
    ("on 21 Nov", "on the twenty-first of November"),
    ("on 3 November 2026", "on the third of November twenty twenty-six"),
    ("on 2026-11-21.", "on the twenty-first of November twenty twenty-six."),
    ("You have 2 tries left.", "You have two tries left."),
    ("a 50% discount", "a fifty percent discount"),
    ("**Refund** of `CH-26112102` per [our policy](https://x.test/p).", "Refund of charge ending two one zero two per our policy."),
    ("- first\n- second", "first. second"),
    ("## Refunds\nWithin 30 days.", "Refunds. Within thirty days."),
])
def test_speak_unit_cases(raw: str, spoken: str) -> None:
    assert speak(raw) == spoken


def test_speak_is_idempotent_and_keeps_already_spoken_text() -> None:
    line = "Done. I've refunded ninety-six dollars for the charge ending zero four one three."
    assert speak(line) == line and speak(speak("$96.00 on 21 Nov")) == speak("$96.00 on 21 Nov")


def fixed_lines() -> dict[str, str]:
    """What: every fixed customer-facing line of the agent, with example values filled in.
    Use when: the snapshot. Inputs: none. Output: {name: chat line}. Side effects: none. Role: the line inventory."""
    lines: dict[str, str] = {
        "router.verify": router.VERIFY_LINE, "router.verify_say": router.VERIFY_SAY,
        "router.first_verify": f"{router.FIRST_VERIFY_LEAD} {router.VERIFY_LINE}",  # D-074
        "router.first_verify_say": f"{router.FIRST_VERIFY_LEAD} {router.VERIFY_SAY}",
        "router.decline_off_topic": router.DECLINE_OFF_TOPIC_LINE,
        "ingress.cap": ingress.CAP_LINE, "ingress.deadline": ingress.DEADLINE_MESSAGE, "ingress.swept": ingress.SWEPT_MESSAGE,
        "escalation.model_unavailable": escalation.MODEL_UNAVAILABLE_LINE,
        "escalation.ticket_offer_opened": escalation.TICKET_OFFER_OPENED.format(short=SHORT),
        "escalation.nothing_to_offer": escalation.NOTHING_TO_OFFER_LINE,
        "escalation.deadline_ticket": escalation.DEADLINE_TICKET_LINE.format(display=DISPLAY),
        "escalation.deadline_no_ticket": f"{escalation.DEADLINE_LEAD} {router.NO_TICKET_CLAUSE.format(ref=REF)}",
        "handler.not_current": handler.NOT_CURRENT_LINE,
        "handler.verify_expired": handler.VERIFY_EXPIRED_LINE, "voice_yes.reprompt": voice_yes.REPROMPT_LINE,
        "confirm.verify_lapsed": confirm.VERIFY_LAPSED_LINE,
        "verification.failed": verification.FAILED_LINE.format(n=2, tries="tries"),
        "verification.locked": verification.LOCKED_LINE, "verification.no_pin": verification.NO_PIN_LINE, "verification.held_ticket":
            verification.HELD_TICKET_LINE.format(name="Natasha", clause=router.TICKET_CLAUSE.format(display=DISPLAY)),
        "escalation.hold": f"{router.ESCALATION_LEADS['not_in_docs']} {escalation.HOLD_SAY}",
        "escalation.hold_chat": f"{router.ESCALATION_LEADS['not_in_docs']} {escalation.HOLD_LINE}",
        "escalation.card_pending": escalation.CARD_PENDING_LINE, "escalation.no_requester": escalation.NO_REQUESTER_LINE,
        "verification.conflict": verification.CONFLICT_LINE, "verification.unavailable": verification.UNAVAILABLE_LINE,
        "confirm.cancelled": confirm.CANCELLED_LINE, "confirm.timed_out": confirm.TIMED_OUT_LINE,
        "confirm.not_active": confirm.NOT_ACTIVE_LINE, "confirm.step_up": confirm.STEP_UP_LINE,
        "confirm.refused": confirm.REFUSED_LINE, "confirm.cancel_failed": confirm.CANCEL_FAILED_LINE,
        "confirm.commit_failed": f"{confirm.COMMIT_FAILED_LEAD} {confirm.FAILED_TICKET.format(short=SHORT)}",
        "confirm.submit_failed": f"{confirm.SUBMIT_FAILED_LEAD} {confirm.FAILED_TICKET.format(short=SHORT)}",
        "confirm.commit_unconfirmed": confirm.COMMIT_UNCONFIRMED.format(short=SHORT),
        "confirm.submit_unconfirmed": confirm.SUBMIT_UNCONFIRMED.format(short=SHORT),
        "confirm.commit_unconfirmed_no_ticket": confirm.COMMIT_UNCONFIRMED_NO_TICKET.format(
            clause=router.NO_TICKET_CLAUSE.format(ref=REF)),
        "confirm.submit_unconfirmed_no_ticket": confirm.SUBMIT_UNCONFIRMED_NO_TICKET.format(
            clause=router.NO_TICKET_CLAUSE.format(ref=REF)),
        "confirm.submitted_from_status": confirm.SUBMITTED_FROM_STATUS.format(display=DISPLAY),
        "confirm.committed_from_status": confirm.COMMITTED_FROM_STATUS,
        "confirm.rules_changed_tail": confirm.RULES_CHANGED_TAIL,
    }
    for reason, lead in {**router.ESCALATION_LEADS, **{f"tool_failure.{k}": v for k, v in
                                                       router.TOOL_FAILURE_LEADS.items()}}.items():
        lines[f"router.{reason}.ticket"] = f"{lead} {router.TICKET_CLAUSE.format(display=DISPLAY)}"
        lines[f"router.{reason}.no_ticket"] = f"{lead} {router.NO_TICKET_CLAUSE.format(ref=REF)}"
    lines["router.generic.ticket"] = f"{router.GENERIC_ESCALATION_LEAD} {router.TICKET_CLAUSE.format(display=DISPLAY)}"
    return lines


def test_every_fixed_line_has_a_say_with_no_markdown_or_raw_id() -> None:
    rendered = "\n".join(f"{name}: {speak(line)}" for name, line in sorted(fixed_lines().items())) + "\n"
    bad = {name: speak(line) for name, line in fixed_lines().items() if UNSPEAKABLE.search(speak(line))}
    assert bad == {}
    if os.environ.get("UPDATE_SAY_SNAPSHOT") == "1":
        SNAPSHOT.parent.mkdir(exist_ok=True)
        SNAPSHOT.write_text(rendered)
    assert SNAPSHOT.read_text() == rendered


def test_the_line_inventory_misses_no_line_constant() -> None:
    """A new *_LINE, *_LEAD, *_CLAUSE or line template constant must be added to fixed_lines()."""
    covered = list(fixed_lines().values()) + list(THREAD_ONLY)
    pattern = re.compile(r"(_LINE|_LEAD|_CLAUSE|_MESSAGE|_TICKET|_UNCONFIRMED(_NO_TICKET)?|_FROM_STATUS|_TAIL|_SAY|"
                         r"_OPENED)$")
    missing = []
    for module in (router, ingress, escalation, handler, verification, confirm, voice_yes):
        for name in dir(module):
            value = getattr(module, name)
            if isinstance(value, str) and pattern.search(name):
                head = value.split("{")[0]
                if not any(head in line for line in covered):
                    missing.append(f"{module.__name__}.{name}")
    assert missing == []


async def test_voice_finals_are_spoken_end_to_end() -> None:
    w = World([AIMessage("", tool_calls=[{"name": "list_charges", "args": {}, "id": "c1", "type": "tool_call"}])],
              overrides={("GET", "/v1/me/charges"): lambda r: error_response()})
    await w.seed({"messages": [HumanMessage("hi"), AIMessage("hello", id="a0")], **OWNER})
    outcome, _ = await w.send(mode="voice", text="show my charges")
    assert outcome.final.text.endswith("ticket ending 8213.")
    assert outcome.final.say.endswith("ticket ending eight two one three.") and not UNSPEAKABLE.search(outcome.final.say)
    thread = (await w.values())["messages"]
    assert thread[-2].content == outcome.final.say  # voice thread lines are the spoken form
    assert str(thread[-1].id).startswith("record-")  # then the code record of the ticket (D-071)


async def test_model_answers_get_a_plain_say_and_keep_markdown_in_text() -> None:
    w = World([AIMessage("**Refunds** take 5 days for $96.00.")], verified=False)
    outcome, _ = await w.send(text="how long do refunds take?")
    assert outcome.final.text == "**Refunds** take 5 days for $96.00."
    assert outcome.final.say == "Refunds take five days for ninety-six dollars."


async def test_apology_say_in_voice() -> None:
    w = World(model=Boom(messages=iter([])), fallback=Boom(messages=iter([])))
    outcome, _ = await w.send(mode="voice", text="help")
    assert outcome.final.say == escalation.MODEL_UNAVAILABLE_LINE


@pytest.mark.parametrize("raw,spoken", [
    # review PR #54 I1: invalid dates are spoken as numbers, never raise
    ("batch 2026-13-45", "batch two thousand twenty-six-thirteen-forty-five"),
    ("on 2026-00-10", "on two thousand twenty-six-zero-ten"),
    ("on 45 Nov", "on forty-five Nov"),
    # I2: negative amounts keep their sign
    ("-$20.00", "minus twenty dollars"),
    ("a refund of -$20.00 was applied", "a refund of minus twenty dollars was applied"),
    ("−$20.00", "minus twenty dollars"),
    ("$-20.00", "minus twenty dollars"),
    ("balance -5", "balance minus five"),
    # I3: ordinals
    ("the 1st", "the first"),
    ("2nd attempt", "second attempt"),
    ("3rd time", "third time"),
    ("the 21st", "the twenty-first"),
    ("November 21st", "the twenty-first of November"),
    ("21st Nov 2026", "the twenty-first of November twenty twenty-six"),
    ("billed on the 1st of each month", "billed on the first of each month"),
    # I4: multi-part ids, last four characters of the whole id
    ("INV-2026-0412", "invoice ending zero four one two"),
    ("invoice INV-2026-0412 is paid", "invoice ending zero four one two is paid"),
    ("TCK-26118213", "ticket ending eight two one three"),
    # M1: only real month names
    ("3 Marketing seats", "three Marketing seats"),
    ("Mark 2 as paid", "Mark two as paid"),
    ("2 Separate charges", "two Separate charges"),
])
def test_speak_review_cases(raw: str, spoken: str) -> None:
    assert speak(raw) == spoken


def test_speak_never_raises_on_random_text(caplog: pytest.LogCaptureFixture) -> None:
    """Review I1 fuzz: random strings built from the characters the rules look at must never raise, and the rules
    themselves must handle them (the logged catch-all fallback is never needed)."""
    import random
    alphabet = "0123456789-$.,:%/ −stndrhNovMarDecINVTCK#*_`[]()\n"
    tokens = ["2026-13-45", "2026-00-00", "99 Nov", "Nov 99", "0th", "$", "-$", "$-", "INV-", "TCK-0000-",
              "1st", "31st", "Dec 0", "0 Jan 0000", "9" * 40, "$" + "9" * 40]
    rng = random.Random(54)
    for _ in range(3000):
        parts = [rng.choice(tokens) if rng.random() < 0.3 else
                 "".join(rng.choice(alphabet) for _ in range(rng.randint(0, 12))) for _ in range(rng.randint(1, 6))]
        text = " ".join(parts)
        out = speak(text)
        assert isinstance(out, str), text
    assert [r for r in caplog.records if r.name == "agent.say"] == []


def test_speak_falls_back_to_the_stripped_text_and_logs_on_an_unexpected_error(
        monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    import agent.say as say

    def broken(_: str) -> str:
        """What: a rule that raises. Use when: this test. Inputs: text. Output: never. Side effects: raises.
        Role: double."""
        raise IndexError("rule bug")

    monkeypatch.setattr(say, "_spoken", broken)
    assert speak("**Your** total is $5") == "Your total is $5"
    assert any(r.name == "agent.say" and r.exc_info for r in caplog.records)


@pytest.mark.parametrize("raw,spoken", [
    # re-review N1: a minus sign directly before the amount (no space) is spoken as "minus"
    ("-$49", "minus forty-nine dollars"),
    ("−$49", "minus forty-nine dollars"),
    ("$-49", "minus forty-nine dollars"),
    ("total -$49.50", "total minus forty-nine dollars and fifty cents"),
    ("(-$5.00)", "(minus five dollars)"),
    ("adjustment: -$0.50", "adjustment: minus fifty cents"),
    ("-$1,234.56 balance", "minus one thousand two hundred thirty-four dollars and fifty-six cents balance"),
    ("a minus $20 change", "a minus twenty dollars change"),
    ("a credit of $96.00", "a credit of ninety-six dollars"),
    ("balance -5", "balance minus five"),
])
def test_a_sign_touching_the_amount_is_minus(raw: str, spoken: str) -> None:
    assert speak(raw) == spoken


@pytest.mark.parametrize("raw,spoken", [
    # re-review N1: a spaced dash between words is punctuation, never "minus"
    ("Pro plan - $49 per month", "Pro plan - forty-nine dollars per month"),
    ("Starter - $19/mo, Pro - $49/mo", "Starter - nineteen dollars/mo, Pro - forty-nine dollars/mo"),
    ("between $10 - $20", "between ten dollars - twenty dollars"),
    ("Team plan − $99", "Team plan − ninety-nine dollars"),
    ("Refund - $96.00 to your card", "Refund - ninety-six dollars to your card"),
    ("$10-$20", "ten dollars-twenty dollars"),
    ("3-5 business days", "three-five business days"),
    ("seats - 5 left", "seats - five left"),
    ("Annual -  $490", "Annual - four hundred ninety dollars"),
    ("Pro: $49 - billed monthly", "Pro: forty-nine dollars - billed monthly"),
])
def test_a_spaced_dash_is_punctuation_not_minus(raw: str, spoken: str) -> None:
    assert speak(raw) == spoken
