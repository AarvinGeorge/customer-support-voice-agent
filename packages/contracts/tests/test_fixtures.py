"""What: tests that every recorded SSE fixture line validates and each turn is a well-formed stream.
Use when: CI. Inputs: fixtures/*.jsonl. Output: pass or fail. Side effects: reads files.
Role: keeps lane C's replay fixtures and lane B's emitter on the same contract."""
import json
from collections import defaultdict

import pytest
from conftest import FIXTURES

from contracts.events import EVENT_TYPES, TurnCompletedEvent, parse_event
from contracts.fixtures_build import render_all

FILES = ("s1_chat.jsonl", "voice_turn.jsonl", "edge_turns.jsonl")


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
    assert evs[0].input_kind == "voice"
    say = evs[-1].final.say
    assert say and "*" not in say and "$" not in say


def test_all_event_types_covered_across_fixtures():
    """What: lane C can assert on every s6.2 type from the fixtures. Use when: CI.
    Inputs: none. Output: assertion. Side effects: reads files. Role: coverage for the reducer."""
    seen = {parse_event(ln).type for f in FILES for ln in _lines(f)}
    assert seen == set(EVENT_TYPES)
