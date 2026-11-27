"""What: tests for model tool schemas, visibility sets and ask_support_agent (doc 07 s5, s6.6).
Use when: CI. Inputs: none. Output: pass or fail. Side effects: none.
Role: guards the "rules in code" tool surface."""
import pytest
from pydantic import ValidationError

from contracts.tools import (
    ASK_SUPPORT_AGENT_TOOL,
    TOOL_ARGS,
    VISIBILITY,
    FunctionCallOutput,
    PrepareRefundArgs,
    SearchKbArgs,
    strict_json_schema,
    tool_schemas,
)

FORBIDDEN_ARGS = {"account_id", "user_id", "role", "tier", "pin", "amount", "amount_cents"}


def _walk(schema):
    """What: yield every object node in a JSON schema. Use when: tests. Inputs: schema.
    Output: iterator of dicts. Side effects: none. Role: test helper."""
    if isinstance(schema, dict):
        if schema.get("type") == "object":
            yield schema
        for v in schema.values():
            yield from _walk(v)
    elif isinstance(schema, list):
        for v in schema:
            yield from _walk(v)


def test_no_commit_like_tool_and_no_identity_args():
    """What: no commit, submit, cancel, verify or escalate tool; no identity or money args.
    Use when: CI. Inputs: none. Output: assertion. Side effects: none. Role: s5.1 rule."""
    for name in TOOL_ARGS:
        assert not any(w in name for w in ("commit", "submit", "cancel", "verify_", "escalate"))
    for name, model in TOOL_ARGS.items():
        assert not (set(model.model_fields) & FORBIDDEN_ARGS), name


def test_strict_schema_shape():
    """What: every object node has additionalProperties false and all properties required.
    Use when: CI. Inputs: none. Output: assertion. Side effects: none. Role: strict mode."""
    for spec in tool_schemas():
        params = spec["parameters"]
        for node in _walk(params):
            assert node.get("additionalProperties") is False, spec["name"]
            assert sorted(node.get("required", [])) == sorted(node.get("properties", {})), spec["name"]


def test_optional_args_are_nullable_in_strict_schema():
    """What: an optional arg becomes required but nullable. Use when: CI. Inputs: none.
    Output: assertion. Side effects: none. Role: strict-mode encoding."""
    s = strict_json_schema(SearchKbArgs)
    ec = s["properties"]["error_code"]
    assert {"type": "null"} in ec["anyOf"]


def test_arg_validation_rules():
    """What: patterns and bounds from s5.1. Use when: CI. Inputs: none. Output: assertion.
    Side effects: none. Role: argument guard."""
    with pytest.raises(ValidationError):
        SearchKbArgs.model_validate({"query": "ab"})
    with pytest.raises(ValidationError):
        PrepareRefundArgs.model_validate({"charge_id": "CH-1", "reason": "duplicate", "note": ""})
    PrepareRefundArgs.model_validate({"ending": "0412", "reason": "duplicate", "note": "twice"})


def test_visibility_sets_sorted_and_scoped():
    """What: V0 is unverified tools only; V2 = V1 plus list_charges. Use when: CI.
    Inputs: none. Output: assertion. Side effects: none. Role: s5.2."""
    assert VISIBILITY["V0"] == ("create_ticket", "request_verification", "search_kb")
    assert set(VISIBILITY["V2"]) - set(VISIBILITY["V1"]) == {"list_charges"}
    for names in VISIBILITY.values():
        assert list(names) == sorted(names)
        assert set(names) <= set(TOOL_ARGS)


def test_ask_support_agent_matches_doc():
    """What: Realtime function schema exactly as s6.6. Use when: CI. Inputs: none.
    Output: assertion. Side effects: none. Role: voice tool guard."""
    assert ASK_SUPPORT_AGENT_TOOL["type"] == "function"
    assert ASK_SUPPORT_AGENT_TOOL["name"] == "ask_support_agent"
    assert ASK_SUPPORT_AGENT_TOOL["parameters"] == {
        "type": "object", "additionalProperties": False, "required": ["question"],
        "properties": {"question": {"type": "string", "maxLength": 2000,
                                    "description": "What the customer said or asked, in their words"}}}
    FunctionCallOutput.model_validate({"say": "Done.", "escalated": False})
    with pytest.raises(ValidationError):
        FunctionCallOutput.model_validate({"say": "x", "escalated": False, "awaiting": "pin"})


def test_parameters_carry_no_descriptor_docstring():
    """What: the root of each parameters schema has no description (the D-015 docstring must not
    reach the model; review PR4 Minor 2). Use when: CI. Inputs: none. Output: assertion.
    Side effects: none. Role: prompt hygiene."""
    for spec in tool_schemas():
        assert "description" not in spec["parameters"], spec["name"]
        assert "Side effects" not in str(spec["parameters"]), spec["name"]
