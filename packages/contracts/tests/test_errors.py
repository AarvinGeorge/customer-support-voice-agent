"""What: tests for the error envelope and the error code tables (doc 07 s1.3).
Use when: CI. Inputs: none. Output: pass or fail. Side effects: none.
Role: guards the shape every non-2xx body uses."""
import pytest
from pydantic import ValidationError

from contracts.errors import CORE_ERROR_CODES, ERROR_CLASSES, ErrorBody, ErrorEnvelope


def _body(**over):
    """What: build a valid error body dict. Use when: tests. Inputs: overrides.
    Output: dict. Side effects: none. Role: test helper."""
    base = {"class": "core_unavailable", "code": "TOOL_ERROR", "message": "Try again.",
            "retryable": True, "changed": False, "fault_injected": False, "details": {}}
    base.update(over)
    return base


def test_class_alias_round_trip():
    """What: JSON key "class" maps to class_ and back. Use when: CI. Inputs: none.
    Output: assertion. Side effects: none. Role: alias guard."""
    env = ErrorEnvelope.model_validate({"error": _body()})
    assert env.error.class_ == "core_unavailable"
    dumped = env.model_dump(mode="json", by_alias=True)
    assert dumped["error"]["class"] == "core_unavailable"
    assert "class_" not in dumped["error"]


def test_unknown_class_and_extra_field_rejected():
    """What: class outside the list and extra keys fail. Use when: CI. Inputs: none.
    Output: assertion. Side effects: none. Role: extra=forbid guard."""
    with pytest.raises(ValidationError):
        ErrorBody.model_validate(_body(**{"class": "teapot"}))
    with pytest.raises(ValidationError):
        ErrorBody.model_validate(_body(extra_field=1))


def test_changed_may_be_null():
    """What: changed null means unknown. Use when: CI. Inputs: none. Output: assertion.
    Side effects: none. Role: tri-state guard."""
    assert ErrorBody.model_validate(_body(changed=None)).changed is None


def test_error_classes_count():
    """What: 17 classes as listed in s1.3. Use when: CI. Inputs: none. Output: assertion.
    Side effects: none. Role: list guard."""
    assert len(ERROR_CLASSES) == 17


def test_core_error_table_spot_checks():
    """What: core code table has 21 rows with the doc's HTTP, retryable, changed.
    Use when: CI. Inputs: none. Output: assertion. Side effects: none. Role: table guard."""
    assert len(CORE_ERROR_CODES) == 21
    assert CORE_ERROR_CODES["TOOL_ERROR"] == (503, True, False)
    assert CORE_ERROR_CODES["INTERNAL"] == (500, True, None)
    assert CORE_ERROR_CODES["PENDING_EXPIRED"] == (410, False, False)
    assert CORE_ERROR_CODES["VERIFY_LOCKED"] == (423, False, False)


def test_default_dump_uses_wire_name_class():
    """What: model_dump and model_dump_json without by_alias emit "class" (review PR4 I2).
    Use when: CI. Inputs: none. Output: assertion. Side effects: none. Role: wire-name guard."""
    import json

    b = ErrorBody.model_validate(_body())
    assert "class" in b.model_dump() and "class_" not in b.model_dump()
    assert json.loads(b.model_dump_json())["class"] == "core_unavailable"
    assert "class_" not in b.model_dump_json()


def test_wire_key_class_underscore_rejected():
    """What: a body with "class_" instead of "class" fails validation (no silent round trip).
    Use when: CI. Inputs: none. Output: assertion. Side effects: none. Role: wire-name guard."""
    bad = _body()
    bad["class_"] = bad.pop("class")
    with pytest.raises(ValidationError):
        ErrorBody.model_validate(bad)
