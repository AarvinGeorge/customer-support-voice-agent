"""What: tests for the SSE envelope and event union (doc 07 s6.1, s6.2).
Use when: CI. Inputs: none. Output: pass or fail. Side effects: none.
Role: guards every event type the browser and the event log read."""
import pytest
from pydantic import ValidationError

from contracts.events import EVENT_TYPES, format_sse, parse_event

ENV = {"v": 1, "seq": 1, "session_id": "s", "turn_id": "t", "trace_id": "t",
       "ts": "2026-11-27T13:00:00Z", "mode": "chat"}


def _ev(type_, **fields):
    """What: event dict with envelope. Use when: tests. Inputs: type and fields.
    Output: dict. Side effects: none. Role: test helper."""
    return {**ENV, "type": type_, **fields}


def test_fourteen_event_types():
    """What: the s6.2 table has 14 types. Use when: CI. Inputs: none. Output: assertion.
    Side effects: none. Role: list guard."""
    assert len(EVENT_TYPES) == 14
    assert "turn.completed" in EVENT_TYPES and "message.reset" not in EVENT_TYPES


def test_discriminated_parse_and_unknown_type():
    """What: parse picks the class by type; unknown type fails. Use when: CI.
    Inputs: none. Output: assertion. Side effects: none. Role: discriminator guard."""
    ev = parse_event(_ev("token", text="Hello"))
    assert type(ev).__name__ == "TokenEvent"
    with pytest.raises(ValidationError):
        parse_event(_ev("message.reset"))


def test_envelope_rules():
    """What: v must be 1, seq from 1, extra keys rejected. Use when: CI. Inputs: none.
    Output: assertion. Side effects: none. Role: envelope guard."""
    with pytest.raises(ValidationError):
        parse_event({**_ev("token", text="x"), "v": 2})
    with pytest.raises(ValidationError):
        parse_event({**_ev("token", text="x"), "seq": 0})
    with pytest.raises(ValidationError):
        parse_event({**_ev("token", text="x"), "surprise": 1})


def test_step_completed_model_and_rule_fields():
    """What: r4 additions on step.completed (title, detail, model tokens, rule).
    Use when: CI. Inputs: none. Output: assertion. Side effects: none. Role: r4 guard."""
    ev = parse_event(_ev("step.completed", step_id="t:1", status="ok", ms=900,
                         title="Prepared refund", detail="$96.00 on 21 Nov", summary={},
                         attempt=1, rule={"decision": "allowed", "reason_code": "PRO_WITHIN_LIMITS",
                                          "policy_ref": {"article": "refund-policy",
                                                         "section": "2", "title": "Pro refunds"}}))
    assert ev.rule.decision == "allowed"
    ev = parse_event(_ev("step.completed", step_id="t:2", status="ok", ms=1200,
                         title="Drafted answer", summary={}, attempt=1, model="m",
                         input_tokens=10, output_tokens=5, cache_read_tokens=0))
    assert ev.input_tokens == 10
    with pytest.raises(ValidationError):
        parse_event(_ev("step.completed", step_id="t:3", status="ok", ms=1, title="x",
                        detail="d" * 81, summary={}, attempt=1))


def test_error_event_carries_class_alias():
    """What: error event uses ErrorBody fields with the "class" key. Use when: CI.
    Inputs: none. Output: assertion. Side effects: none. Role: error event guard."""
    ev = parse_event(_ev("error", **{"class": "turn_deadline_exceeded", "code": "DEADLINE",
                                     "message": "m", "retryable": False, "changed": None,
                                     "fault_injected": False, "details": {}}))
    assert ev.model_dump(by_alias=True, mode="json")["class"] == "turn_deadline_exceeded"


def test_format_sse_wire():
    """What: wire format "event, id, data, blank line". Use when: CI. Inputs: none.
    Output: assertion. Side effects: none. Role: s6.1 wire guard."""
    ev = parse_event(_ev("token", text="Hi"))
    wire = format_sse(ev)
    lines = wire.split("\n")
    assert lines[0] == "event: token" and lines[1] == "id: 1"
    assert lines[2].startswith("data: {") and wire.endswith("\n\n")
