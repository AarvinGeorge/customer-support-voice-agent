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


def test_redact_walks_models_sets_tuples_bytes_dataclasses():
    """What: Pydantic models, dataclasses, sets, frozensets, tuples and bytes are walked, not passed
    through (review PR4 I1). Use when: CI. Inputs: none. Output: assertion. Side effects: none.
    Role: proves the LangSmith second net cannot leak through an unknown container."""
    from dataclasses import dataclass

    from contracts.base import StrictModel

    class Msg(StrictModel):
        """What: test model. Use when: this test. Inputs: note, pin. Output: model.
        Side effects: none. Role: fixture."""

        note: str
        pin: str

    @dataclass
    class Box:
        """What: test dataclass. Use when: this test. Inputs: pin, note. Output: dataclass.
        Side effects: none. Role: fixture."""

        pin: str
        note: str

    raw = {"msg": Msg(note="mail priya@acme.com", pin="4821"), "s": {"priya@acme.com"},
           "fs": frozenset({"priya@acme.com"}), "t": ("priya@acme.com",), "b": b"priya@acme.com",
           "box": Box(pin="7302", note="priya@acme.com")}
    before = repr(raw)
    assert "4821" in before and "7302" in before and "priya@acme.com" in before  # proven possible
    out = redact(raw, RedactContext())
    after = repr(out)
    assert "4821" not in after and "7302" not in after and "priya@acme.com" not in after
    assert out["msg"]["pin"] == "[PIN]" and out["box"]["pin"] == "[PIN]"
    assert out["b"] == "p***@acme.com" and out["t"] == ("p***@acme.com",)
    assert out["s"] == {"p***@acme.com"} and out["fs"] == frozenset({"p***@acme.com"})


def test_redact_refuses_unknown_types():
    """What: an unknown non-scalar raises TypeError instead of passing through unredacted.
    Use when: CI. Inputs: none. Output: assertion. Side effects: none. Role: fail loudly."""
    import pytest

    class Opaque:
        """What: an object redact cannot inspect. Use when: this test. Inputs: none.
        Output: object. Side effects: none. Role: fixture."""

    with pytest.raises(TypeError):
        redact({"x": Opaque()}, RedactContext())
    assert redact({"n": 3, "f": 1.5, "ok": True, "none": None}, RedactContext()) == {
        "n": 3, "f": 1.5, "ok": True, "none": None}
