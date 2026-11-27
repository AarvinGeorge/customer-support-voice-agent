"""What: tests for the Gate 1 probe script's threshold arithmetic and probe set.
Use when: CI. Inputs: the script at services/knowledge/scripts/probe_retrieval.py, loaded by path (no network).
Output: pass or fail. Side effects: none. Role: doc 07 s4.1 steps 2 and 3 and D-052 (S1 to S5 questions in the set)."""
import importlib.util
from pathlib import Path

import pytest

PATH = Path(__file__).resolve().parents[1] / "scripts" / "probe_retrieval.py"


def load():
    """What: import the script as a module. Use when: each test. Inputs: none. Output: module. Side effects: none
    (main() is guarded). Role: helper."""
    spec = importlib.util.spec_from_file_location("probe_retrieval", PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def rows(ans, off, scenario="generic"):
    """What: build probe rows. Use when: threshold tests. Inputs: answerable and off_topic cosines, scenario tag.
    Output: list. Side effects: none. Role: helper."""
    return ([{"label": "answerable", "scenario": scenario, "top_cosine": c} for c in ans]
            + [{"label": "off_topic", "scenario": scenario, "top_cosine": c} for c in off])


def test_thresholds_midpoint_and_lower_quartile():
    """What: separated ranges give tau_low = midpoint and tau_high = inclusive lower quartile. Use when: CI.
    Inputs: ans 0.4 to 0.7, off up to 0.2. Output: assertion. Side effects: none. Role: 07 s4.1 steps 2 and 3."""
    t = load().thresholds(rows([0.4, 0.5, 0.6, 0.7], [0.1, 0.2]))
    assert t["overlap"] is False and t["tau_low"] == pytest.approx(0.3) and t["tau_high"] == pytest.approx(0.475)


def test_thresholds_overlap_uses_highest_off_topic_plus_point_01():
    """What: overlapping ranges give tau_low = highest off_topic + 0.01, and tau_high is never below tau_low.
    Use when: CI. Inputs: off 0.5 above lowest answerable 0.4. Output: assertion. Side effects: none."""
    t = load().thresholds(rows([0.4, 0.45, 0.46, 0.47], [0.5]))
    assert t["overlap"] is True and t["tau_low"] == pytest.approx(0.51) and t["tau_high"] >= t["tau_low"]


def test_probe_set_has_s1_to_s5_questions_and_all_labels():
    """What: the set carries scenario-tagged questions for S1 to S5 and every label, under 40 probes.
    Use when: CI. Inputs: PROBES. Output: assertion. Side effects: none. Role: D-052 item 1."""
    probes = load().PROBES
    assert {p[1] for p in probes} >= {"S1", "S2", "S3", "S4", "S5"}
    assert {p[0] for p in probes} == {"answerable", "not_in_docs", "off_topic"} and len(probes) <= 40


def test_thresholds_ignore_scenario_probes():
    """What: S1 to S5 probes are checks, not inputs: adding high or low scenario rows leaves both numbers unchanged.
    Use when: CI. Inputs: generic rows plus scenario rows. Output: assertion. Side effects: none. Role: D-053."""
    m = load()
    base = rows([0.4, 0.5, 0.6, 0.7], [0.1, 0.2])
    extra = rows([0.9, 0.95, 0.99], [0.01], scenario="S1")
    assert m.thresholds(base + extra) == m.thresholds(base)


def test_margins_record_each_scenario_probe_and_flag_under_point_02():
    """What: each scenario probe gets a margin to the bar it is tested against (answerable: top cosine minus tau_high;
    off_topic: tau_low minus top cosine; not_in_docs: tau_high minus top cosine) and a risk flag under 0.02; generic
    probes get none. Use when: CI. Inputs: four rows. Output: assertion. Side effects: none. Role: D-053."""
    m = load()
    rs = [{"label": "answerable", "scenario": "S1", "top_cosine": 0.70},
          {"label": "answerable", "scenario": "S2", "top_cosine": 0.62},
          {"label": "off_topic", "scenario": "S4", "top_cosine": 0.10},
          {"label": "not_in_docs", "scenario": "S4", "top_cosine": 0.40},
          {"label": "answerable", "scenario": "generic", "top_cosine": 0.5}]
    m.add_margins(rs, tau_low=0.3, tau_high=0.6)
    assert [r["margin"] for r in rs[:4]] == pytest.approx([0.10, 0.02, 0.20, 0.20])
    assert [r["risk"] for r in rs[:4]] == [False, False, False, False]
    m.add_margins(rs, tau_low=0.3, tau_high=0.61)
    assert rs[1]["risk"] is True and rs[1]["margin"] == pytest.approx(0.01)
    assert "margin" not in rs[4]
