"""What: tests for the LangSmith hide function by provenance, default deny (CS-E-02.F14 and its fix round 1,
build-log/reports/rca-pin-overmask.md). Text our code writes carries an explicit "ours" label at its source (a
message id from ours_id, a state key in OURS_FIELDS) and keeps its numbers; model, tool and system messages are
system data (D-078); every unlabeled string, in or outside a customer frame, gets the full PIN scan (D-078).
Use when: pytest (no network).
Inputs: none. Output: pass or fail. Side effects: none.
Role: pins the fix for trace 01a22039-11d3 (the verifier input read "U-[PIN]" and "ticket ending [PIN]") and the
frame-invariant canary (review-pr91 I2): customer text without the label is masked on every path.
"""
from __future__ import annotations

import json
from typing import Any

import pytest

from contracts.core_api import Claims, MeContext, VerifyRequest
from contracts.redact import OURS_ID_PREFIX, PIN_TOKEN, RedactContext, hide_for_langsmith, ours_id, scan_text

# The shape of the real verifier input (trace 01a22039-11d3): code-written source blocks that mention the word PIN
# (an article heading) next to our own numbers.
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
CANARY = "my PIN is 4821"


def _shapes(content: str, msg_id: str | None) -> list[dict[str, Any]]:
    """What: one human message in the plain dump and the LangChain serialized shape. Use when: these tests.
    Inputs: content, message id. Output: two dicts. Side effects: none. Role: helper."""
    return [{"type": "human", "content": content, "id": msg_id},
            {"lc": 1, "type": "constructor", "id": ["langchain", "schema", "messages", "HumanMessage"],
             "kwargs": {"content": content, "type": "human", "id": msg_id}}]


def test_positive_control_the_scanner_masks_this_exact_evidence_as_customer_text() -> None:
    # Proves the "kept" assertions below can fail: read as customer text, every one of our numbers is masked.
    masked = scan_text(EVIDENCE, RedactContext())
    assert all(s not in masked for s in OURS)


def test_a_message_labelled_ours_keeps_our_numbers_and_an_unlabeled_one_is_scanned() -> None:
    assert ours_id("gate2_evidence").startswith(OURS_ID_PREFIX) and ours_id("turn") != ours_id("turn")
    with pytest.raises(ValueError):
        ours_id("spoof")  # only the kinds the code sites write
    assert not any(c.isdigit() for c in "".join(ours_id("turn") for _ in range(200)))  # never a PIN-like run
    for labelled, unlabeled in zip(_shapes(EVIDENCE, ours_id("gate2_evidence")), _shapes(EVIDENCE, "run-1")):
        kept = json.dumps(hide_for_langsmith({"messages": [[{"type": "system", "content": "rules"}, labelled]]}))
        assert not [s for s in OURS if s.replace('"', '\\"') not in kept] and PIN_TOKEN not in kept
        masked = json.dumps(hide_for_langsmith({"messages": [[unlabeled]]}))
        assert "U-2041" not in masked and "invoice ending 0412" not in masked  # default deny (D-078)


def test_session_state_under_ours_fields_keeps_our_numbers() -> None:
    state = {"context_snapshot": {"claims": {"user_id": "U-2041", "expires_at": "2026-11-28T00:35:24-05:00"},
                                  "open_tickets": [{"ticket_id": "TCK-26215124", "short_id": "5124"}],
                                  "recent_actions": [{"amount_cents": 9600}]},
             "claims_snapshot": {"user_id": "U-2041"}, "verified": True}
    assert hide_for_langsmith(state) == state
    assert hide_for_langsmith({"short_id": "5124"}) == {"short_id": PIN_TOKEN}  # unlabeled key: unknown provenance


def test_a_labelled_message_still_scans_its_customer_frame() -> None:
    framed = f"<customer_message>\n{CANARY}\n</customer_message>\n{EVIDENCE}"
    for msg in _shapes(framed, ours_id("turn")):
        out = json.dumps(hide_for_langsmith({"messages": [[msg]], "pin": "4821", "email": "priya@acme.example"}))
        assert "4821" not in out and "priya@acme.example" not in out
        assert "U-2041" in out and "ticket ending 5124" in out and "invoice ending 0412" in out


class _Runtime:
    """What: an object the walker cannot inspect, like the runtime inside a middleware ModelRequest. Use when: the
    canary and fallback tests. Inputs: text its str() returns. Output: object. Side effects: none. Role: fixture."""

    def __init__(self, text: str) -> None:
        """What: keep the text. Use when: fixture. Inputs: text. Output: none. Side effects: none. Role: constructor."""
        self.text = text

    def __str__(self) -> str:
        """What: the text. Use when: the JSON fallback. Inputs: none. Output: text. Side effects: none. Role: fixture."""
        return self.text


def test_an_unwalkable_payload_keeps_labelled_records_and_scans_the_rest() -> None:
    # Live trace 01a2206c-7896: a middleware run's request held an object the walker refuses; the fallback flattened
    # it and scanned every string, so the model's tool call read {"ending": "[PIN]"}.
    payload = {"request": {"runtime": _Runtime(CANARY),
                           "messages": [{"type": "ai", "content": "", "tool_calls": [
                               {"name": "list_charges", "args": {"ending": "0412"}}]}],
                           "state": {"context_snapshot": {"open_tickets": [{"short_id": "5124"}]}, "pin": "4821"}}}
    out = json.dumps(hide_for_langsmith(payload))
    assert '"ending": "0412"' in out and '"short_id": "5124"' in out
    assert "4821" not in out  # the unlabeled runtime string and the PIN field are masked


# review-pr91 I2: the frame-invariant canary. Customer text that reaches a trace on any path WITHOUT the ours label is
# masked, whether or not a frame is around it, intact or cut.
CANARY_PATHS: list[tuple[str, Any]] = [
    ("human message, unframed", {"messages": [[{"type": "human", "content": f"Customer said: {CANARY}"}]]}),
    ("human message, framed", {"messages": [[{"type": "human",
                                               "content": f"<customer_message>{CANARY}</customer_message>"}]]}),
    ("human message, cut frame", {"messages": [[{"type": "human", "content": f"<customer_message>{CANARY}"}]]}),
    ("serialized human message", {"messages": [[_shapes(f"hello {CANARY}", None)[1]]]}),
    ("non-message field", {"note": f"Customer's last message: {CANARY}"}),
    ("cut frame in a field", {"inputs": {"q": f"<customer_message>{CANARY}"}}),
    ("PIN key inside a JSON string", {"body": json.dumps({"pin": "4821"})}),
    ("state key not labelled", {"summary": f"{CANARY} please"}),
    ("list of strings", {"items": ["a", f"{CANARY}"]}),
    ("unwalkable object", {"obj": _Runtime(f"Customer: {CANARY}")}),
    ("a bare number", {"text": "4821"}),
    # review-pr91 r1 I3: each net proven alone on the framed path (the frame scan, then the full scan).
    ("bare framed number", {"messages": [[{"type": "human", "content": "<customer_message>4821</customer_message>"}]]}),
    ("bare framed number in a field", {"q": "<customer_message>\n4821\n</customer_message>"}),
    ("cue outside the frame", {"messages": [[{"type": "human",
                                              "content": "<customer_message>hi</customer_message> my PIN is 4821"}]]}),
    # review-pr91 r1 M5: the label counts only in the exact shape the code sites write.
    ("spoofed label on a user dict", {"messages": [[{"role": "user", "id": "ours:spoof", "text": CANARY}]]}),
    ("label with an unknown kind", {"messages": [[{"type": "human", "id": ours_id("turn").replace("turn", "x", 1),
                                                   "content": CANARY}]]}),
    ("label without content", {"messages": [[{"type": "human", "id": ours_id("turn"), "text": CANARY}]]}),
    ("ours field deep in a payload", {"a": {"b": {"context_snapshot": CANARY}}}),
]


@pytest.mark.parametrize("payload", [p for _, p in CANARY_PATHS], ids=[n for n, _ in CANARY_PATHS])
def test_canary_customer_text_without_the_ours_label_is_masked_on_every_path(payload: Any) -> None:
    assert "4821" in json.dumps(payload, default=str)  # the probe finds the canary in the input
    assert "4821" not in json.dumps(hide_for_langsmith(payload), default=str)


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
    # The PIN goes in VerifyRequest only (positive control); what core returns has no PIN field. This proves the
    # schema only: the agent stores core's JSON dict as sent, so the stored value is covered by the agent test
    # test_card_verification_never_puts_the_pin_in_our_records (review-pr91 M4).
    assert "pin" in _field_names(VerifyRequest)
    for record in (Claims, MeContext):
        assert not {n for n in _field_names(record) if "pin" in n}, record.__name__
