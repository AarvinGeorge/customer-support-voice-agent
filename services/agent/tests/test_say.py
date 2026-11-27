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
from agent.structured import confirm, handler, verification
from escalation_world import OWNER, Boom, World, error_response

SNAPSHOT = Path(__file__).parent / "snapshots" / "say_fixed_lines.txt"
UNSPEAKABLE = re.compile(r"[*_`#\[\]<>|$%]|\d|\b[A-Z]{2,4}-[0-9A-F]{4,}\b|https?://")
# Lines that only go to the thread for the model to read (never shown or spoken to the customer).
THREAD_ONLY = (verification.VERIFIED_THREAD_LINE, verification.CARD_SUBMITTED_LINE)
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
        "router.decline_off_topic": router.DECLINE_OFF_TOPIC_LINE,
        "ingress.cap": ingress.CAP_LINE, "ingress.deadline": ingress.DEADLINE_MESSAGE, "ingress.swept": ingress.SWEPT_MESSAGE,
        "escalation.model_unavailable": escalation.MODEL_UNAVAILABLE_LINE,
        "escalation.ticket_offer_opened": escalation.TICKET_OFFER_OPENED.format(short=SHORT),
        "escalation.nothing_to_offer": escalation.NOTHING_TO_OFFER_LINE,
        "escalation.deadline_ticket": escalation.DEADLINE_TICKET_LINE.format(display=DISPLAY),
        "escalation.deadline_no_ticket": f"{escalation.DEADLINE_LEAD} {router.NO_TICKET_CLAUSE.format(ref=REF)}",
        "handler.not_current": handler.NOT_CURRENT_LINE,
        "verification.failed": verification.FAILED_LINE.format(n=2, tries="tries"),
        "verification.locked": verification.LOCKED_LINE.format(display=DISPLAY),
        "verification.locked_no_ticket": verification.LOCKED_NO_TICKET_LINE,
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
    for module in (router, ingress, escalation, handler, verification, confirm):
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
    assert thread[-1].content == outcome.final.say  # voice thread lines are the spoken form


async def test_model_answers_get_a_plain_say_and_keep_markdown_in_text() -> None:
    w = World([AIMessage("**Refunds** take 5 days for $96.00.")], verified=False)
    outcome, _ = await w.send(text="how long do refunds take?")
    assert outcome.final.text == "**Refunds** take 5 days for $96.00."
    assert outcome.final.say == "Refunds take five days for ninety-six dollars."


async def test_apology_say_in_voice() -> None:
    w = World(model=Boom(messages=iter([])), fallback=Boom(messages=iter([])))
    outcome, _ = await w.send(mode="voice", text="help")
    assert outcome.final.say == escalation.MODEL_UNAVAILABLE_LINE
