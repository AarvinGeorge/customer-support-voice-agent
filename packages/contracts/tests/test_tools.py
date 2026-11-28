"""What: tests for model tool schemas, visibility sets and ask_support_agent (doc 07 s5, s6.6).
Use when: CI. Inputs: none. Output: pass or fail. Side effects: none.
Role: guards the "rules in code" tool surface."""
import re
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from contracts.tools import (
    ASK_SUPPORT_AGENT_TOOL,
    TOOL_ARGS,
    TOOL_DESCRIPTIONS,
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
    assert VISIBILITY["V0"] == ("request_verification", "search_kb")  # D-070: no ticket while unverified
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


# CS-E-03.F8 and F7: tool descriptions are model-facing instructions, so they must not carry a business rule
# (core and the retrieval gates own those) and must send refunds and questions to the tools that decide.
_POLICY = yaml.safe_load((Path(__file__).resolve().parents[3] / "data" / "policy.yaml").read_text())


def _policy_terms(text):
    """What: policy.yaml role, tier and blocked-status names (and spaced forms) found in text.
    Use when: description rule checks. Inputs: text. Output: sorted matches. Side effects: none. Role: test helper."""
    words = [*_POLICY["roles"]["all_roles"], *_POLICY["tiers"], *_POLICY["actions"]["refund"]["blocked_statuses"]]
    forms = {f for w in words for f in (w, w.replace("_", " "))}
    return sorted(f for f in forms if re.search(rf"\b{re.escape(f)}s?\b", text, re.IGNORECASE))


def test_policy_terms_helper_can_fail():
    """What: positive control for the helper. Use when: CI. Inputs: none. Output: assertion. Side effects: none.
    Role: an empty match below is meaningful only if a planted rule is found."""
    assert _policy_terms("members cannot refund an enterprise invoice") == ["enterprise", "member"]


@pytest.mark.parametrize("name", sorted(TOOL_DESCRIPTIONS))
def test_tool_description_holds_no_business_rule(name):
    """What: no description names a role, tier or blocked status, or a number. Use when: CI. Inputs: tool name.
    Output: assertion. Side effects: none. Role: eligibility lives in core's rule engine, not in tool text."""
    desc = TOOL_DESCRIPTIONS[name]
    assert _policy_terms(desc) == [], name
    assert not re.search(r"\d", desc), name


def test_prepare_refund_description_says_it_decides_and_returns_the_policy():
    """What: the description sends every refund request here and says the result carries the decision and policy.
    Use when: CI. Inputs: none. Output: assertion. Side effects: none. Role: F8 (model declined without calling it)."""
    low = TOOL_DESCRIPTIONS["prepare_refund"].lower()
    assert "every refund request" in low and "eligib" in low and "policy" in low


def test_search_kb_description_says_search_first():
    """What: the description sends every question that is not small talk or an account lookup to search first.
    Use when: CI. Inputs: none. Output: assertion. Side effects: none. Role: F7 (off-topic answered unsearched)."""
    low = TOOL_DESCRIPTIONS["search_kb"].lower()
    assert "small talk" in low and "account" in low and "first" in low


def test_ask_support_agent_description_relays_everything_and_rules_nothing():
    """What: the voice tool description sends every customer question or request to the agent and carries no
    scope, role, tier or number rule. Use when: CI. Inputs: none. Output: assertion. Side effects: none.
    Role: CS-E-05.F2 (the Realtime model judged scope itself, so Gate 1 and the classifier never ran)."""
    desc = ASK_SUPPORT_AGENT_TOOL["description"]
    low = desc.lower()
    assert "every customer question or request" in low and "whatever the topic" in low
    assert "the agent decides scope, policy and identity" in low
    assert "anything about" not in low and "about quillstack" not in low  # the old scope gate
    assert _policy_terms(desc) == []
    assert not re.search(r"\d", desc)
