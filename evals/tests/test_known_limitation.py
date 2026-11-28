"""What: D-083 known limitations: the tagged cases (G-07, G-15, smoke S4) expect the safe weak path (clarify, then escalate,
Gate 2 not bypassed), and the golden report and the smoke summary show them apart from plain passes, never counted as fixed.
Use when: CI (python (evals)). Inputs: the golden dataset, grader and report builders, fake turns from tests.support.
Output: pass or fail. Side effects: none (no stack, no real calls). Role: CS-E-02.F11 fix round 2 (D-083)."""
from __future__ import annotations

import datetime as dt

from contracts.policy import load_policy

from evals import report, smoke
from evals.cases import ROOT, load_cases
from evals.graders import Ctx, g_turns, grade_case
from evals.model import CaseGrade, Check

from .support import case_by_id, gate, make_run, turn

NOW = dt.datetime(2026, 11, 28, tzinfo=dt.UTC)
POLICY = load_policy(ROOT / "data" / "policy.yaml")
TAGGED = {"G-07", "G-15"}


def test_only_the_d083_cases_carry_the_tag_and_they_expect_the_weak_path() -> None:
    cases = {c["id"]: c for c in load_cases()}
    assert {cid for cid, c in cases.items() if c.get("known_limitation")} == TAGGED
    for cid in TAGGED:
        c = cases[cid]
        assert c["known_limitation"] == "D-083"
        exp = c["expected"]
        assert exp["outcome"] == ["escalate"] and exp["escalation_reason"] == "not_in_docs"
        assert set(exp["gate"]["retrieval_verdict_any_of"]) == {"weak", "none"} and exp["gate"]["decision"] == "escalate"
        assert "policy_invented" in exp["forbidden"]
        asks = [t for t in c["turns"] if t["kind"] == "text" and "Android" in t["text"]]
        # one clarifying question, checked by Gate 2; D-066 may show it as answered (CS-E-02.F13), never escalated
        assert asks and asks[0]["expect"] == {"end_reason_any": ["clarify", "answered"], "gate2_ran": True}
        assert c["turns"][-1]["expect"]["end_reason"] == "escalated"  # then the ticket or human offer


def _clarify_turn(index: int, g2: str | None) -> tuple:
    """What: a clarify turn with a weak retrieval gate and optionally a Gate 2 event. Use when: gate2_ran tests.
    Inputs: index, Gate 2 verdict or None. Output: built turn. Side effects: none. Role: helper."""
    parts = [gate("weak", "clarify")]
    if g2 is not None:
        g = gate(g2, "answer" if g2 == "pass" else "skip", kind="grounding")
        g.update(claims_total=0, claims_supported=0, claims=[])
        parts.append(g)
    return turn(index, "text", parts, end_reason="clarify", text="Do you mean a native Android app?")


def test_gate2_ran_fails_when_gate_2_was_bypassed() -> None:
    case = {"id": "G-07", "split": "test", "turns": [{"kind": "text", "text": "x", "expect": {"end_reason": "clarify",
                                                                                         "gate2_ran": True}}],
            "expected": {}}
    policy = POLICY
    ran = g_turns(Ctx(case, make_run("G-07", [_clarify_turn(0, "pass")]), policy))
    skipped = g_turns(Ctx(case, make_run("G-07", [_clarify_turn(0, "skipped")]), policy))
    missing = g_turns(Ctx(case, make_run("G-07", [_clarify_turn(0, None)]), policy))
    assert ran[0].status == "pass"
    assert skipped[0].status == "fail" and "Gate 2" in skipped[0].detail
    assert missing[0].status == "fail" and "Gate 2" in missing[0].detail


def test_grade_case_carries_the_tag() -> None:
    g = grade_case(case_by_id("G-07"), make_run("G-07", [_clarify_turn(0, "pass")]), POLICY)
    assert g.known_limitation == "D-083"
    assert grade_case(case_by_id("G-08"), make_run("G-08", [_clarify_turn(0, "pass")]), POLICY).known_limitation is None


def test_report_counts_a_tagged_pass_apart_from_plain_passes() -> None:
    ok = [Check("outcome", "pass")]
    grades = [CaseGrade("G-01", "train", ok), CaseGrade("G-07", "test", ok, known_limitation="D-083"),
              CaseGrade("G-15", "train", [Check("outcome", "fail", "x")], known_limitation="D-083")]
    r = report.build_report(grades, None, meta={}, now=NOW)
    c = r["cases"]
    assert c["total"] == 3
    assert c["verdicts"] == {"pass": 1, "fail": 0, "error": 0}  # G-07's pass is not a plain pass
    assert c["by_split"]["test"] == {"pass": 0, "fail": 0, "error": 0}
    assert c["known_limitation"]["verdicts"] == {"pass": 1, "fail": 1, "error": 0}
    assert [(k["id"], k["tag"], k["verdict"]) for k in c["known_limitation"]["cases"]] == [("G-07", "D-083", "pass"),
                                                                                         ("G-15", "D-083", "fail")]
    assert not report.all_green(r)  # a tagged case that left the safe path is still a failure
    md = report.render_markdown(r)
    assert "## Known limitations (not counted as fixed)" in md
    assert "| G-07 | test | pass (known limitation D-083) |" in md and "| all (1) | 1 | 0 | 0 |" in md
    assert report.all_green(report.build_report(grades[:2], None, meta={}, now=NOW))


def test_smoke_summary_shows_a_tagged_pass_apart_and_still_passes() -> None:
    ok = [Check("outcome", "pass")]
    rows = [smoke.SmokeRow(lb, CaseGrade(cid, "train", ok, known_limitation="D-083" if cid == "G-07" else None),
                           [Check("live.stop.0", "pass")], ["answered"], 3) for cid, lb, _ in smoke.SMOKE]
    ledger = [("S1", 0, {"total": 3, "agent": 2, "verifier": 1, "classifier": 0, "search": 2})]
    logs = {**{k: 0 for k in smoke.AGENT_LOG_PATTERNS}, "search requests": 2, "embedding retries (upper bound)": 0}
    meta = {"when": "w", "project": "cs-smoke-x", "edge_port": "3141", "commit": "abc", "ingest_embedding_calls": "1"}
    text, passed = smoke.render_summary(rows, ledger, logs, meta, 25, None)
    assert passed
    s4 = [ln for ln in text.splitlines() if ln.startswith("S4 out of docs")]
    assert s4 and "PASS*" in s4[0]
    assert "known limitations (not counted as fixed): S4 D-083 PASS" in text
    desc = smoke.status_description(rows, ledger, 25, passed, None)
    assert desc.startswith("PASS 4/5 cases + 1 known limitation (D-083)") and "cap 25" in desc and len(desc) <= 140
