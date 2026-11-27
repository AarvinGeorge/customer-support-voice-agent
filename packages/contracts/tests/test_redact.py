"""What: tests for the redact interface field rules (doc 07 s11).
Use when: CI. Inputs: none. Output: pass or fail. Side effects: none.
Role: proves secrets present in the input are absent from the output."""
from contracts.redact import RedactContext, mask_email, redact


def test_card_pins_dropped_and_email_masked():
    """What: pin and step_up_pin values become "[PIN]"; emails become p***@domain.
    Use when: CI. Inputs: none. Output: assertion. Side effects: none. Role: s11 rows."""
    raw = {"verification": {"account_id": "QS-10423", "pin": "4821"},
           "confirm": {"step_up_pin": "7302"},
           "args": {"new_email": "priya@acme.com", "note": "mail priya@acme.com today"}}
    blob = repr(raw)
    assert "4821" in blob and "7302" in blob and "priya@acme.com" in blob  # proven possible
    out = redact(raw, RedactContext())
    text = repr(out)
    assert "4821" not in text and "7302" not in text and "priya@acme.com" not in text
    assert out["verification"]["account_id"] == "QS-10423"
    assert out["args"]["new_email"] == "p***@acme.com"
    assert raw["verification"]["pin"] == "4821"  # input not mutated


def test_mask_email():
    """What: mask keeps first letter and domain. Use when: CI. Inputs: none.
    Output: assertion. Side effects: none. Role: s11 email rule."""
    assert mask_email("priya@acme.com") == "p***@acme.com"
