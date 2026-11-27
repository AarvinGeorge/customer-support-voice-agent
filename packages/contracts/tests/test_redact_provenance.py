"""What: tests for redaction by provenance (D-071 part 3, RCA build-log/reports/rca-unverified-ticket-2026-11-27.md
fix E): the s11.1 scanner applies to customer-originated text only. System-originated data (assistant text,
code-written lines, tool results, event fields holding code-generated ids) keeps its numbers; values the system
issued are kept when a model that heard the customer repeats them; customer text is still redacted.
Use when: pytest (no network).
Inputs: none. Output: pass or fail. Side effects: none.
Role: the contracts half of the provenance fix; the agent half is in services/agent/tests/test_redact_provenance.py."""
from __future__ import annotations

from contracts.redact import PIN_TOKEN, RedactContext, hide_for_langsmith, issued_numbers, redact, redact_system, \
    scan_pins, scan_text

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


def test_issued_numbers_reads_digits_and_spoken_digits() -> None:
    assert issued_numbers(["ticket ending 8352", "ticket ending eight three five two", "no numbers"]) == {"8352"}
    assert issued_numbers([]) == frozenset()


def test_a_value_the_system_issued_is_kept_when_repeated_but_a_new_number_is_not() -> None:
    issued = issued_numbers([CODE_LINE])
    said = "I've opened a ticket for our team, ticket ending eight three five two."
    kept = scan_pins(said, AWAITING, issued=issued)
    assert kept.redacted == 0 and kept.text == said  # nothing redacted: the original text comes back
    echoed = scan_pins("Thanks, I heard 4821, and your ticket ends 8352.", AWAITING, issued=issued)
    assert "4821" not in echoed.text and "8352" in echoed.text and echoed.redacted == 1


def test_customer_redact_is_unchanged() -> None:
    out = redact({"text": "8352", "pin": "4821"}, AWAITING)
    assert out == {"text": PIN_TOKEN, "pin": PIN_TOKEN}


def test_langsmith_keeps_system_ids_and_hides_framed_customer_pins() -> None:
    payload = {"messages": [[{"type": "human", "content": "<customer_message>\nQS-10423 4821\n</customer_message>"},
                             {"type": "ai", "content": CODE_LINE}]],
               "outputs": {"short_id": "8352"}, "pin": "7302", "email": "priya@acme.com"}
    out = repr(hide_for_langsmith(payload))
    assert "4821" not in out and "7302" not in out and "priya@acme.com" not in out
    assert "QS-10423" in out and "ticket ending 8352" in out and "'short_id': '8352'" in out
