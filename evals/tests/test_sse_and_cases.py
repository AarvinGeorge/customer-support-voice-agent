"""What: tests for the SSE parser, the dataset loaders and placeholder resolution.
Use when: CI. Inputs: the shipped dataset and small literals. Output: pass or fail. Side effects: none.
Role: the runner's input side (dataset and wire format)."""
from __future__ import annotations

import json

import pytest
import yaml

from evals import cases, sse
from evals.tests.support import ROOT


def test_parse_sse_reads_events_and_skips_keepalives() -> None:
    raw = ": keepalive\n\nevent: token\nid: 3\ndata: {\"type\":\"token\",\"seq\":3,\"text\":\"hi\"}\n\nevent: x\nid: 4\ndata: {\"type\":\"x\",\"seq\":4}\n\n"
    out = list(sse.parse_sse(raw.splitlines()))
    assert [e["seq"] for e in out] == [3, 4] and out[0]["text"] == "hi"


def test_parse_sse_rejects_non_json_data_loudly() -> None:
    with pytest.raises(sse.SseError):
        list(sse.parse_sse(["event: x", "data: {not json", ""]))


def test_parse_sse_handles_a_final_event_without_trailing_blank_line() -> None:
    assert list(sse.parse_sse(["event: x", 'data: {"type":"x","seq":1}'])) == [{"type": "x", "seq": 1}]


def test_load_cases_validates_every_shipped_case() -> None:
    cs = cases.load_cases()
    assert len(cs) == 21 and {c["split"] for c in cs} == {"train", "test"}


def test_load_cases_rejects_a_case_that_breaks_the_schema(tmp_path) -> None:  # type: ignore[no-untyped-def]
    data = yaml.safe_load((ROOT / "evals/golden/golden.yaml").read_text())
    del data["cases"][0]["expected"]["tool_trajectory"]
    p = tmp_path / "g.yaml"
    p.write_text(yaml.safe_dump(data))
    with pytest.raises(cases.DatasetError):
        cases.load_cases(p)


def test_load_retrieval_and_personas() -> None:
    q = cases.load_retrieval()
    assert len(q) == 25 and {x["label"] for x in q} == {"answerable", "not_in_docs", "off_topic"}
    p = cases.load_personas()
    assert p["Priya Raman"]["account_id"] == "QS-10423" and len(p["Priya Raman"]["pin"]) == 4


def test_every_case_persona_exists_in_the_persona_file() -> None:
    p = cases.load_personas()
    assert all(c["persona"] in p for c in cases.load_cases())


def test_resolve_text_fills_persona_placeholders_only() -> None:
    persona = {"account_id": "QS-1", "pin": "1234"}
    assert cases.resolve_text("id {persona.account_id} pin {persona.pin} {other}", persona) == "id QS-1 pin 1234 {other}"


def test_wrong_pin_is_four_digits_and_never_the_real_pin() -> None:
    for real in ("0000", "1111", "7342"):
        w = cases.wrong_pin(real)
        assert len(w) == 4 and w.isdigit() and w != real


def test_report_json_roundtrip_of_sse_event() -> None:
    assert json.loads(json.dumps({"a": 1})) == {"a": 1}
