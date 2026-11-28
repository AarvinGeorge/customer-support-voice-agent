"""What: tests for the LangSmith hide function by provenance (CS-E-02.F14, build-log/reports/rca-pin-overmask.md).
Customer-authored text reaches a trace only inside the frames code writes at ingress (<customer_message>,
<voice_transcript>); everything else in a trace payload is ours (code-written evidence, tool results, session state)
and is never pattern-masked. PIN fields and emails are still replaced everywhere, and the frames still get the
scanner as the second net.
Use when: pytest (no network).
Inputs: none. Output: pass or fail. Side effects: none.
Role: pins the fix for trace 01a22039-11d3, where the verifier input read "U-[PIN]", "[PIN]-11-28" and "ticket
ending [PIN]" because a code-written HumanMessage and a state dict were scanned as customer text.
"""
from __future__ import annotations

import json

from contracts.core_api import Claims, MeContext, VerifyRequest
from contracts.redact import PIN_TOKEN, RedactContext, hide_for_langsmith, scan_text

# The shape of the real verifier input (trace 01a22039-11d3): code-written source blocks that mention the word PIN
# (an article heading and our capabilities text) next to our own numbers.
EVIDENCE = (
    '<evidence>\n<source sid="account_context" kind="verified_context">{"account": {"claims": {"user_id": "U-2041", '
    '"expires_at": "2026-11-28T00:35:24-05:00"}, "open_tickets": [{"display": "ticket ending 5124", '
    '"short_id": "5124"}], "recent_actions": [{"amount_cents": 9600, "label": "Refunded $96.00 for charge ending '
    '0412"}]}}</source>\n<source sid="doc@1" kind="doc">Verification settings > Your support PIN\nUpdated: '
    '2026-11-27\nEach person has their own PIN.</source>\n</evidence>\n<draft>\nYour invoice ending 0412 was '
    'refunded.\n</draft>'
)
OURS = ("U-2041", "2026-11-28", "ticket ending 5124", '"short_id": "5124"', "9600", "charge ending 0412",
        "Updated: 2026-11-27", "invoice ending 0412")


def test_positive_control_the_scanner_masks_this_exact_evidence_as_customer_text() -> None:
    # Proves the "kept" assertions below can fail: read as customer text, every one of our numbers is masked.
    masked = scan_text(EVIDENCE, RedactContext())
    assert all(s not in masked for s in OURS)


def test_a_code_written_human_message_keeps_our_numbers_in_the_trace() -> None:
    for message in ({"type": "human", "content": EVIDENCE},
                    {"lc": 1, "type": "constructor", "id": ["langchain", "schema", "messages", "HumanMessage"],
                     "kwargs": {"content": EVIDENCE, "type": "human"}}):
        out = json.dumps(hide_for_langsmith({"messages": [[{"type": "system", "content": "rules"}, message]]}))
        missing = [s for s in OURS if s.replace('"', '\\"') not in out]
        assert not missing and PIN_TOKEN not in out


def test_session_state_records_keep_our_numbers_in_the_trace() -> None:
    state = {"context_snapshot": {"claims": {"user_id": "U-2041", "expires_at": "2026-11-28T00:35:24-05:00"},
                                  "open_tickets": [{"ticket_id": "TCK-26215124", "short_id": "5124"}],
                                  "recent_actions": [{"amount_cents": 9600}]},
             "ending": "0412"}
    assert hide_for_langsmith(state) == state


def test_positive_control_a_typed_pin_in_a_customer_frame_is_still_masked_next_to_our_numbers() -> None:
    framed = f"<customer_message>\nmy PIN is 4821\n</customer_message>\n{EVIDENCE}"
    voice = ('<voice_transcript source="client" untrusted="true">\ncustomer: QS-10423 4821\n'
             'voice assistant: ticket ending 5124.\n</voice_transcript>')
    payload = {"messages": [[{"type": "human", "content": framed}, {"type": "human", "content": voice}]],
               "inputs": {"q": "<customer_message>4821</customer_message>"}, "pin": "4821",
               "email": "priya@acme-analytics.example"}
    out = json.dumps(hide_for_langsmith(payload))
    assert "4821" not in out and "priya@acme-analytics.example" not in out
    assert "U-2041" in out and "ticket ending 5124" in out and "invoice ending 0412" in out


class _Runtime:
    """What: an object the walker cannot inspect, like the runtime inside a middleware ModelRequest. Use when: the
    unwalkable payload tests. Inputs: text its str() returns. Output: object. Side effects: none. Role: fixture."""

    def __init__(self, text: str) -> None:
        """What: keep the text. Use when: fixture. Inputs: text. Output: none. Side effects: none. Role: constructor."""
        self.text = text

    def __str__(self) -> str:
        """What: the text. Use when: the JSON fallback. Inputs: none. Output: text. Side effects: none. Role: fixture."""
        return self.text


def test_an_unwalkable_payload_keeps_our_records_and_scans_customer_frames() -> None:
    # Live trace 01a2206c-7896: a middleware run's "request" held an object the walker refuses, and the fallback
    # scanned every string as customer text, so the model's tool call read {"ending": "[PIN]"}.
    payload = {"request": {"runtime": _Runtime("<customer_message>my PIN is 4821</customer_message>"),
                           "messages": [{"type": "ai", "tool_calls": [{"name": "list_charges",
                                                                         "args": {"ending": "0412"}}]}],
                           "state": {"open_tickets": [{"short_id": "5124"}], "pin": "4821"}}}
    out = json.dumps(hide_for_langsmith(payload))
    assert '"ending": "0412"' in out and '"short_id": "5124"' in out
    assert "4821" not in out  # positive control: the framed customer PIN and the PIN field are masked


def _field_names(model: type) -> set[str]:
    """What: every field name of a Pydantic model and of the models nested in it. Use when: the record tests.
    Inputs: model class. Output: names. Side effects: none. Role: helper."""
    names: set[str] = set()
    for name, field in model.model_fields.items():  # type: ignore[attr-defined]
        names.add(name.lower())
        for arg in (field.annotation, *getattr(field.annotation, "__args__", ())):
            for inner in (arg, *getattr(arg, "__args__", ())):
                if isinstance(inner, type) and hasattr(inner, "model_fields") and inner is not model:
                    names |= _field_names(inner)
    return names


def test_our_records_have_no_place_for_a_pin() -> None:
    # The PIN goes in VerifyRequest only (positive control); what core returns and the agent keeps has no PIN field.
    assert "pin" in _field_names(VerifyRequest)
    for record in (Claims, MeContext):
        assert not {n for n in _field_names(record) if "pin" in n}, record.__name__
