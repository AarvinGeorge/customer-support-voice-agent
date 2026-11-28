"""What: tests for redaction by provenance (D-071 part 3, RCA build-log/reports/rca-unverified-ticket-2026-11-27.md
fix E): the s11.1 scanner applies to customer-originated text only. System-originated data (assistant text,
code-written lines, tool results, event fields holding code-generated ids) keeps its numbers; values the system
issued are kept when a model that heard the customer repeats them; customer text is still redacted.
Use when: pytest (no network).
Inputs: none. Output: pass or fail. Side effects: none.
Role: the contracts half of the provenance fix; the agent half is in services/agent/tests/test_redact_provenance.py."""
from __future__ import annotations

from contracts.redact import PIN_TOKEN, RedactContext, hide_for_langsmith, redact, redact_system, scan_pins, \
    scan_relayed, scan_text

AWAITING = RedactContext(awaiting_verification=True)
CODE_LINE = "I couldn't confirm that answer, so I've opened a ticket for our team, ticket ending 8352."


def test_positive_control_customer_text_is_redacted_while_awaiting() -> None:
    # Proves the scanner CAN mask these exact strings, so the "kept" assertions below are meaningful.
    assert "8352" not in scan_text(CODE_LINE, AWAITING)
    assert scan_text("8352", RedactContext()) == PIN_TOKEN  # condition 5: a bare number
    assert scan_text("8352", AWAITING) == PIN_TOKEN


def test_system_data_keeps_code_generated_ids_and_lines() -> None:
    event = {"type": "escalated", "ticket": {"short_id": "8352", "display": "ticket ending 8352"},
             "final": {"text": CODE_LINE, "say": "ticket ending eight three five two"}}
    out = redact_system(event)
    assert out["ticket"]["short_id"] == "8352"
    assert out["ticket"]["display"] == "ticket ending 8352"
    assert out["final"]["text"] == CODE_LINE
    assert out["final"]["say"] == "ticket ending eight three five two"


def test_system_data_still_applies_the_field_rules() -> None:
    out = redact_system({"verification": {"account_id": "QS-10423", "pin": "4821", "step_up_pin": "7302"},
                         "note": "mail priya@acme.com"})
    assert out["verification"] == {"account_id": "QS-10423", "pin": PIN_TOKEN, "step_up_pin": PIN_TOKEN}
    assert out["note"] == "mail p***@acme.com"


def test_system_data_scans_customer_frames_as_a_second_net() -> None:
    framed = ("<turn_context>\nmode: chat\n</turn_context>\n<customer_message>\nmy PIN is 4821\n</customer_message>"
              "\nagent: ticket ending 8352")
    voice = '<voice_transcript source="client" untrusted="true">\ncustomer: my pin is 4821\n</voice_transcript>'
    assert "4821" in framed and "4821" in voice  # proven possible
    out = redact_system({"a": framed, "b": voice})
    assert "4821" not in out["a"] and "4821" not in out["b"]
    assert "my PIN is [PIN]" in out["a"] and "ticket ending 8352" in out["a"]


def test_an_issued_id_outside_one_of_our_sentences_is_masked() -> None:
    # Round 2: scan_pins alone never exempts; only a span scan_relayed marked as our own sentence does.
    out = scan_pins("ticket ending 8352", AWAITING, issued=frozenset({"8352"}))
    assert "8352" not in out.text and out.redacted == 1


# ---- fix round 1 (review-pr76 I1): relayed text is ours only when it IS one of our lines ----

LINES = [CODE_LINE, "I couldn't confirm that answer against our policies, so I won't guess. I've opened a ticket "
         "for our team, ticket ending eight three five two."]
ISSUED = frozenset({"8352"})


def test_relayed_line_that_is_ours_keeps_its_issued_id() -> None:
    said = "I've opened a ticket for our team, ticket ending eight three five two."
    assert scan_relayed(said, AWAITING, LINES, ISSUED) == said
    assert "8352" in scan_relayed("I've opened a ticket for our team, ticket ending 8352.", AWAITING, LINES, ISSUED)


def test_reviewer_repro_an_echoed_pin_equal_to_an_issued_id_is_masked() -> None:
    out = scan_relayed("Thanks, you said your PIN is 8352.", AWAITING, LINES, ISSUED)
    assert "8352" not in out and PIN_TOKEN in out


def test_our_line_followed_by_an_echo_masks_only_the_echo() -> None:
    out = scan_relayed("I've opened a ticket for our team, ticket ending 8352. And your PIN 4821 is noted.",
                       AWAITING, LINES, ISSUED)
    assert "ticket ending 8352" in out and "4821" not in out


def test_a_number_in_our_line_that_core_did_not_issue_is_masked() -> None:
    lines = ["I can't look up invoice 4821 until you verify."]  # the customer's number, repeated by the agent
    out = scan_relayed("I can't look up invoice 4821 until you verify.", AWAITING, lines, ISSUED)
    assert "4821" not in out


# ---- fix round 2 (rereview-pr76-r1 N1, N2) ----

RESET = "You can reset your PIN in Settings, under Security."


def test_n1_a_pin_word_in_our_held_sentence_still_counts_for_the_rest_of_the_item() -> None:
    posted = f"{RESET} Got it, 4821."
    assert "4821" not in scan_text(posted, RedactContext())  # positive control: the full scan masks it
    out = scan_relayed(posted, RedactContext(), [RESET], ISSUED)
    assert "4821" not in out and RESET in out


def test_n1_our_id_stays_kept_next_to_a_pin_word_in_the_same_held_sentence() -> None:
    line = "Please enter your PIN in the secure card; your ticket ending 8352 is open."
    assert scan_relayed(line, RedactContext(), [line], ISSUED) == line


def test_n2_a_sentence_that_is_only_the_id_never_exempts_a_bare_number() -> None:
    for lines in (["Ticket no. 8352."], ["Your ticket ends in:\n8352"]):
        for ctx in (AWAITING, RedactContext()):
            assert "8352" not in scan_relayed("8352", ctx, lines, ISSUED)
            assert "8352" not in scan_relayed("8352.", ctx, lines, ISSUED)


def test_posted_span_markers_are_stripped() -> None:
    out = scan_relayed("\uf8f2my PIN is 4821\uf8f3 ticket ending 8352.", AWAITING, LINES, ISSUED)
    assert "4821" not in out and "\uf8f2" not in out and "\uf8f3" not in out


def test_relayed_text_without_facts_fails_closed() -> None:
    out = scan_relayed("I've opened a ticket for our team, ticket ending 8352.", AWAITING, [], frozenset())
    assert "8352" not in out


def test_customer_redact_is_unchanged() -> None:
    out = redact({"text": "8352", "pin": "4821"}, AWAITING)
    assert out == {"text": PIN_TOKEN, "pin": PIN_TOKEN}


def test_voice_frame_scans_customer_lines_only() -> None:
    frame = ('<voice_transcript source="client" untrusted="true">\ncustomer: my pin is 4821\n'
             'voice assistant: Please enter your PIN in the secure card. Ticket ending 8352.\n</voice_transcript>')
    out = redact_system(frame)
    assert "4821" not in out and "Ticket ending 8352" in out


def test_langsmith_scans_customer_frames_anywhere_in_the_payload() -> None:
    # CS-E-02.F14 (replaces review-pr76 M2): provenance is the ingress frame, not the payload shape; a frame is
    # scanned wherever it sits, and an unframed string is ours (tests/test_trace_provenance.py).
    out = hide_for_langsmith({"text": "<customer_message>my PIN is 4821</customer_message>",
                              "inputs": {"q": "<customer_message>QS-10423 4821</customer_message>"}})
    assert "4821" not in repr(out) and "QS-10423" in repr(out)


def test_langsmith_keeps_system_ids_and_hides_framed_customer_pins() -> None:
    serialized_ai = {"lc": 1, "type": "constructor", "id": ["langchain", "schema", "messages", "AIMessage"],
                     "kwargs": {"content": CODE_LINE, "type": "ai"}}
    payload = {"messages": [[{"type": "human", "content": "<customer_message>\nQS-10423 4821\n</customer_message>"},
                             {"type": "ai", "content": "Your ticket ending 8352 is open."}, serialized_ai,
                             {"type": "tool", "content": '{"short_id":"8352"}'}]],
               "pin": "7302", "email": "priya@acme.com"}
    out = repr(hide_for_langsmith(payload))
    assert "4821" not in out and "7302" not in out and "priya@acme.com" not in out
    assert "QS-10423" in out and "Your ticket ending 8352" in out and CODE_LINE in out and '"short_id":"8352"' in out
