"""What: tests that every recorded SSE fixture line validates and each turn is a well-formed stream.
Use when: CI. Inputs: fixtures/*.jsonl. Output: pass or fail. Side effects: reads files.
Role: keeps lane C's replay fixtures and lane B's emitter on the same contract."""
import json
from collections import defaultdict

import pytest
from conftest import FIXTURES

from contracts.events import EVENT_TYPES, TurnCompletedEvent, TurnStartedEvent, VerificationResultEvent, parse_event
from contracts.fixtures_build import render_all

FILES = ("s1_chat.jsonl", "voice_turn.jsonl", "edge_turns.jsonl", "s4_unsure.jsonl", "s3_decline.jsonl",
         "s5_failures.jsonl")


def _lines(name):
    """What: non-empty lines of one fixture file. Use when: tests. Inputs: file name.
    Output: list of str. Side effects: reads a file. Role: test helper."""
    return [ln for ln in (FIXTURES / name).read_text().splitlines() if ln.strip()]


def test_fixture_files_exist_and_are_current():
    """What: committed fixtures equal a fresh render. Use when: CI. Inputs: none.
    Output: assertion. Side effects: reads files. Role: sync gate."""
    rendered = render_all()
    assert set(rendered) == set(FILES)
    for name, text in rendered.items():
        assert (FIXTURES / name).read_text() == text, (
            f"{name} is stale: run `uv run python scripts/build_fixtures.py`")


@pytest.mark.parametrize("name", FILES)
def test_every_line_validates(name):
    """What: each line parses into its event class and round-trips unchanged.
    Use when: CI. Inputs: file name. Output: assertion. Side effects: reads a file. Role: validity."""
    lines = _lines(name)
    assert lines, "a zero-line fixture would pass vacuously"
    for ln in lines:
        ev = parse_event(ln)
        assert ev.model_dump(mode="json", by_alias=True) == json.loads(ln)


@pytest.mark.parametrize("name", FILES)
def test_turn_streams_are_well_formed(name):
    """What: per turn, seq runs 1..n, turn.started first, exactly one turn.completed last,
    trace_id equals turn_id, one session per file. Use when: CI. Inputs: file name.
    Output: assertion. Side effects: reads a file. Role: s6.1 stream rules."""
    turns = defaultdict(list)
    sessions = set()
    for ln in _lines(name):
        ev = parse_event(ln)
        turns[ev.turn_id].append(ev)
        sessions.add(ev.session_id)
        assert ev.trace_id == ev.turn_id
    assert len(sessions) == 1
    for evs in turns.values():
        assert [e.seq for e in evs] == list(range(1, len(evs) + 1))
        assert evs[0].type == "turn.started"
        assert evs[-1].type == "turn.completed"
        assert sum(isinstance(e, TurnCompletedEvent) for e in evs) == 1


def test_s1_chat_covers_the_scenario():
    """What: the S1 file has verification, steps, both gates, confirmation, commit, tokens and a
    final answer with citations and usage. Use when: CI. Inputs: none. Output: assertion.
    Side effects: reads a file. Role: S1 coverage."""
    evs = [parse_event(ln) for ln in _lines("s1_chat.jsonl")]
    types = {e.type for e in evs}
    for t in ("verification.required", "verification.result", "step.started", "step.completed",
              "gate", "confirmation.required", "action.result", "status", "token", "turn.completed"):
        assert t in types, t
    assert {e.gate for e in evs if e.type == "gate"} == {"retrieval", "grounding"}
    assert any(e.type == "step.completed" and e.rule is not None for e in evs)
    assert any(e.type == "step.completed" and e.input_tokens is not None for e in evs)
    finals = [e for e in evs if e.type == "turn.completed"]
    assert any(f.final.citations for f in finals)
    assert all(f.usage.latency_ms > 0 for f in finals)
    assert all(e.mode == "chat" for e in evs)


def test_voice_turn_is_voice():
    """What: the voice file is one voice turn ending with a speakable say. Use when: CI.
    Inputs: none. Output: assertion. Side effects: reads a file. Role: voice coverage."""
    evs = [parse_event(ln) for ln in _lines("voice_turn.jsonl")]
    assert {e.turn_id for e in evs} and len({e.turn_id for e in evs}) == 1
    assert all(e.mode == "voice" for e in evs)
    first, last = evs[0], evs[-1]
    assert isinstance(first, TurnStartedEvent) and first.input_kind == "voice"
    assert isinstance(last, TurnCompletedEvent)
    say = last.final.say
    assert say and "*" not in say and "$" not in say


def test_all_event_types_covered_across_fixtures():
    """What: lane C can assert on every s6.2 type from the fixtures. Use when: CI.
    Inputs: none. Output: assertion. Side effects: reads files. Role: coverage for the reducer."""
    seen = {parse_event(ln).type for f in FILES for ln in _lines(f)}
    assert seen == set(EVENT_TYPES)


@pytest.mark.parametrize("name", FILES)
def test_tokens_follow_a_grounding_gate(name):
    """What: in every turn, token events come only after a grounding gate (pass or skipped),
    doc 07 s6.4 (review PR4 Minor 6a). Use when: CI. Inputs: file name. Output: assertion.
    Side effects: reads a file. Role: Gate 2 ordering."""
    seen_gate = {}
    for ln in _lines(name):
        ev = parse_event(ln)
        if ev.type == "gate" and ev.gate == "grounding" and ev.verdict in ("pass", "skipped"):
            seen_gate[ev.turn_id] = True
        if ev.type == "token":
            assert seen_gate.get(ev.turn_id), f"token before grounding gate in turn {ev.turn_id}"


def test_deadline_turn_follows_deadline_path():
    """What: the deadline turn escalates with a deadline ticket and ends with end_reason deadline,
    doc 07 s8.2 (review PR4 Minor 6b). Use when: CI. Inputs: none. Output: assertion.
    Side effects: reads a file. Role: deadline path shape."""
    evs = [parse_event(ln) for ln in _lines("edge_turns.jsonl")]
    esc = [e for e in evs if e.type == "escalated" and e.reason == "deadline"]
    assert len(esc) == 1 and esc[0].ticket is not None
    last = [e for e in evs if e.type == "turn.completed" and e.turn_id == esc[0].turn_id][0]
    assert last.end_reason == "deadline" and last.final.escalated and last.final.ticket is not None


def test_s4_unsure_covers_the_scenario():
    """What: the S4 file has the three unsure cases: an off-topic decline with no ticket, a weak
    first try that clarifies, and a second weak turn (0.41 against tau_high 0.45) that escalates
    not_in_docs with a ticket (doc 07 s6.5). Use when: CI. Inputs: none. Output: assertion.
    Side effects: reads a file. Role: S4 coverage for the step trail gate rows (CS-E-04.S5.T1)."""
    evs = [parse_event(ln) for ln in _lines("s4_unsure.jsonl")]
    gates = [e for e in evs if e.type == "gate" and e.gate == "retrieval"]
    assert [(g.verdict, g.decision) for g in gates] == [("none", "decline"), ("weak", "clarify"),
                                                        ("weak", "escalate")]
    last = gates[-1]
    assert (last.top_cosine, last.tau_low, last.tau_high) == (0.41, 0.30, 0.45)
    ends = {e.turn_id: e.end_reason for e in evs if e.type == "turn.completed"}
    assert list(ends.values()) == ["declined_off_topic", "clarify", "escalated"]
    esc = [e for e in evs if e.type == "escalated"]
    assert len(esc) == 1 and esc[0].reason == "not_in_docs" and esc[0].ticket is not None
    assert esc[0].turn_id == last.turn_id
    assert not any(e.type == "token" for e in evs)


def test_s3_decline_covers_the_scenario():
    """What: the S3 member file verifies Peter (member, Business), then the refund prepare is
    declined ROLE_NOT_PERMITTED with its policy ref, reported as action.result declined with
    changed false, and the grounded answer cites the public roles article (L1 S3, doc 07 s6.2).
    Use when: CI. Inputs: none. Output: assertion. Side effects: reads a file.
    Role: S3 coverage for the decline note (CS-E-04.S5.T3)."""
    evs = [parse_event(ln) for ln in _lines("s3_decline.jsonl")]
    verified = [e for e in evs if isinstance(e, VerificationResultEvent)][0]
    person = verified.person
    assert person is not None
    assert (person.role, person.tier) == ("member", "business")
    rules = [e.rule for e in evs if e.type == "step.completed" and e.rule is not None]
    assert [(r.decision, r.reason_code) for r in rules] == [("declined", "ROLE_NOT_PERMITTED")]
    res = [e for e in evs if e.type == "action.result"]
    assert len(res) == 1 and res[0].outcome == "declined" and res[0].changed is False
    assert res[0].reason_codes == ["ROLE_NOT_PERMITTED"] and res[0].pending_action_id is None
    assert [(p.article, p.section) for p in res[0].policy_refs] == [("roles-and-permissions",
                                                                     "billing-permissions")]
    final = [e for e in evs if e.type == "turn.completed"][-1].final
    assert final.citations and all(c.url.startswith("https://") for c in final.citations)
    assert not any(e.type == "confirmation.required" for e in evs)


def test_s5_failures_cover_the_scenario():
    """What: the S5 file has a turn where the primary model is rate limited and the fallback
    model prepares the refund (model.fallback, usage.fallback_used, models_used), then the yes
    turn where the billing system fails twice: retrying status, action.result failed with changed
    false and a ticket, escalated tool_failure (L1 s8, doc 07 s6.2). Use when: CI. Inputs: none.
    Output: assertion. Side effects: reads a file. Role: S5 coverage (CS-E-04.S5.T3)."""
    evs = [parse_event(ln) for ln in _lines("s5_failures.jsonl")]
    fb = [e for e in evs if e.type == "model.fallback"]
    assert len(fb) == 1 and fb[0].reason == "fault_llm_429"
    ends = [e for e in evs if e.type == "turn.completed"]
    assert [e.end_reason for e in ends] == ["readback", "escalated"]
    assert ends[0].usage.fallback_used and fb[0].to_model in ends[0].usage.models_used
    assert ends[0].turn_id == fb[0].turn_id
    assert any(e.type == "status" and e.kind == "retrying" for e in evs)
    res = [e for e in evs if e.type == "action.result"]
    assert [(r.outcome, r.changed) for r in res] == [("failed", False)] and res[0].ticket is not None
    esc = [e for e in evs if e.type == "escalated"]
    assert len(esc) == 1 and esc[0].reason == "tool_failure" and esc[0].ticket is not None
