"""What: tests for the per-turn expectations added with the new-behaviour golden cases (verification_card,
verification_result, grounding) and for the verification turn's as_persona. Each check is shown failing as well as passing.
Use when: CI. Inputs: hand-built turns (tests/support.py) and a mutated copy of G-08. Output: pass or fail.
Side effects: none. Role: a grader that cannot fail is a green light that tests nothing (owner rule: prove red first)."""
from __future__ import annotations

import copy
from typing import Any

import pytest

from evals import cases, graders, runner
from evals.tests.fakes import ACCESS_CODE, Script
from evals.tests.support import PERSONA, case_by_id, gate, make_run, turn
from evals.tests.test_graders import POLICY, by
from evals.tests.test_runner import PERSONAS, world_and_client

CARD = {"type": "verification.required", "reason": "account_lookup", "prompt_text": "p", "attempts_left": 3, "fields": ["account_id", "pin"]}
GROUND_OK = {**gate("pass", "answer", kind="grounding"), "claims_total": 2, "claims_supported": 2}


def grade_turn(expect: dict[str, Any], *parts: dict[str, Any], end_reason: str = "answered") -> str:
    """What: status of check turn.0 for one built turn against a given expect. Use when: each test. Inputs: expect, events.
    Output: pass, fail or error. Side effects: none. Role: helper over G-08 (one text turn, no card needed)."""
    case = copy.deepcopy(case_by_id("G-08"))
    case["turns"][0]["expect"] = expect
    run = make_run("G-08", [turn(0, "text", list(parts), end_reason=end_reason)])
    return by(graders.grade_case(case, run, POLICY))["turn.0"]


def test_verification_card_offered_matches_the_reason_of_the_event() -> None:
    assert grade_turn({"verification_card": "account_lookup"}, CARD) == "pass"
    assert grade_turn({"verification_card": "action"}, CARD) == "fail"  # a card, but not this reason
    assert grade_turn({"verification_card": "account_lookup"}) == "fail"  # no card at all


def test_verification_card_none_means_no_event_in_the_turn() -> None:
    assert grade_turn({"verification_card": "none"}) == "pass"
    assert grade_turn({"verification_card": "none"}, CARD) == "fail"


def test_verification_result_status_is_read_from_the_turn() -> None:
    conflict = {"type": "verification.result", "status": "conflict"}
    assert grade_turn({"verification_result": "conflict"}, conflict) == "pass"
    assert grade_turn({"verification_result": "conflict"}, {"type": "verification.result", "status": "verified"}) == "fail"
    assert grade_turn({"verification_result": "conflict"}) == "fail"


def test_grounding_needs_a_passing_gate_with_enough_supported_claims() -> None:
    assert grade_turn({"grounding": {"min_claims": 1}}, GROUND_OK) == "pass"
    assert grade_turn({"grounding": {"min_claims": 3}}, GROUND_OK) == "fail"  # fewer claims than required
    unsupported = {**GROUND_OK, "claims_supported": 1}
    assert grade_turn({"grounding": {"min_claims": 1}}, unsupported) == "fail"  # one claim not supported
    failed = {**gate("fail", "clarify", kind="grounding"), "claims_total": 2, "claims_supported": 1}
    assert grade_turn({"grounding": {"min_claims": 1}}, failed, end_reason="clarify") == "fail"
    # no gate event at all (a fixed line skips Gate 2): the turn made no verified claim, which is a fail, not a pass
    assert grade_turn({"grounding": {"min_claims": 1}}) == "fail"


def test_new_expect_keys_are_in_the_schema_and_a_bad_value_is_rejected() -> None:
    case = copy.deepcopy(case_by_id("G-08"))
    case["turns"][0]["expect"] = {"verification_card": "sometimes"}
    import json

    import jsonschema
    schema = json.loads((cases.GOLDEN_DIR / "schema" / "case.schema.json").read_text())
    v = jsonschema.Draft202012Validator(schema)
    assert list(v.iter_errors(case))
    case["turns"][0]["expect"] = {"verification_card": "none", "verification_result": "conflict", "grounding": {"min_claims": 1}}
    assert not list(v.iter_errors(case))


def test_as_persona_sends_the_other_personas_credentials_and_unknown_names_error() -> None:
    case = copy.deepcopy(case_by_id("G-10"))
    case["turns"] = [{"kind": "verification", "credentials": "persona"},
                     {"kind": "verification", "credentials": "persona", "as_persona": "Marcus Lee"}]
    w, client, store = world_and_client()
    w.next = Script([turn(0, "verification", [], end_reason="verified")[0].events, turn(1, "verification", [], end_reason="answered")[0].events])
    client.access(ACCESS_CODE)
    run = runner.run_case(case, PERSONAS[case["persona"]], client, store, personas=PERSONAS)
    first, second = (r["body"]["verification"] for r in w.requests)
    assert first == {"account_id": "QS-10423", "pin": PERSONAS["Priya Raman"]["pin"]}
    assert second == {"account_id": "QS-20318", "pin": PERSONAS["Marcus Lee"]["pin"]} and run.error is None
    case["turns"][1]["as_persona"] = "Nobody"
    w2, client2, store2 = world_and_client()
    w2.next = Script([turn(0, "verification", [], end_reason="verified")[0].events])
    client2.access(ACCESS_CODE)
    run2 = runner.run_case(case, PERSONAS[case["persona"]], client2, store2, personas=PERSONAS)
    assert "Nobody" in (run2.error or "")


@pytest.mark.parametrize("name", ["Priya Raman", "Marcus Lee"])
def test_persona_fixture_is_the_seeded_one(name: str) -> None:
    assert PERSONAS[name]["account_id"].startswith("QS-") and PERSONA["pin"] == "7342"


def test_says_needs_one_phrase_in_that_turns_final_text() -> None:
    case = copy.deepcopy(case_by_id("G-08"))
    case["turns"][0]["expect"] = {"says": ["start a new conversation", "new chat"]}
    ok = make_run("G-08", [turn(0, "text", [], text="To help a different account, please Start A New Conversation.")])
    assert by(graders.grade_case(case, ok, POLICY))["turn.0"] == "pass"  # case-insensitive
    bad = make_run("G-08", [turn(0, "text", [], text="Sorry, something else.")])
    assert by(graders.grade_case(case, bad, POLICY))["turn.0"] == "fail"


def claim(text: str, supported: bool, source_id: str | None) -> dict[str, Any]:
    """What: one Gate 2 claim dict. Use when: a from_source test. Inputs: text, supported, source id. Output: dict.
    Side effects: none. Role: helper."""
    return {"text": text, "supported": supported, "source_id": source_id}


def test_grounding_from_source_needs_a_supported_claim_resting_on_that_source() -> None:
    exp = {"grounding": {"min_claims": 1, "from_source": "session_state"}}
    ok = {**GROUND_OK, "claims": [claim("No ticket was created.", True, "session_state"), claim("x", True, "doc")]}
    assert grade_turn(exp, ok) == "pass"
    other = {**GROUND_OK, "claims": [claim("A ticket was created.", True, "account_context"), claim("x", True, "doc")]}
    assert grade_turn(exp, other) == "fail"  # supported, but not by the session's own state (I2)
    unsupported = {**GROUND_OK, "claims_supported": 1, "claims": [claim("A ticket was created.", False, "session_state"), claim("x", True, "doc")]}
    assert grade_turn(exp, unsupported) == "fail"
    nosource = {**GROUND_OK, "claims": None}
    assert grade_turn(exp, nosource) == "fail"  # no claim list to read: fail, never a silent pass


def test_grounding_verdict_other_than_pass_fails_even_with_full_counts() -> None:
    """Review PR #84 M3: the verdict test must be load bearing on its own."""
    errored = {**gate("error", "clarify", kind="grounding"), "claims_total": 2, "claims_supported": 2}
    assert grade_turn({"grounding": {"min_claims": 1}}, errored) == "fail"


def test_every_as_persona_name_in_the_dataset_is_a_persona() -> None:
    """Review PR #84 M5: dataset validation, not a run-time surprise."""
    personas = cases.load_personas()
    for c in cases.load_cases():
        for t in c["turns"]:
            if t.get("as_persona"):
                assert t["as_persona"] in personas, f"{c['id']}: {t['as_persona']}"


def test_end_reason_any_accepts_each_listed_outcome_and_no_other() -> None:
    """D-082: a turn may end in any of a short list of end reasons; one outside the list fails."""
    exp = {"end_reason_any": ["verification_requested", "answered"]}
    assert grade_turn(exp, end_reason="verification_requested") == "pass"
    assert grade_turn(exp, end_reason="answered") == "pass"
    assert grade_turn(exp, end_reason="clarify") == "fail"
    assert grade_turn(exp, end_reason="escalated") == "fail"
