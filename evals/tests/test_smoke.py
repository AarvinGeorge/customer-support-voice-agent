"""What: tests for the live smoke (evals.smoke and `python -m evals smoke`): the case subset, the call ledger and cap,
the live checks per turn, the log patterns pinned to the agent and knowledge source, the summary and the RUN_REAL refusal.
Use when: CI (no network, no provider: the agent is the FastAPI double, logs are faked).
Inputs: the shipped golden cases, built turns (support.turn). Output: pass or fail. Side effects: tmp files only.
Role: the smoke itself must be proven before its zero findings are trusted (measurement discipline)."""
from __future__ import annotations

import datetime as dt
import re
import subprocess
from pathlib import Path
from typing import Any

import pytest
from starlette.testclient import TestClient

from evals import cli, smoke
from evals.cases import load_cases
from evals.model import Check, TurnResult
from evals.tests.fakes import ACCESS_CODE, FakeStore, FakeWorld, Script, build_app
from evals.tests.support import ROOT, gate, step, turn

AGENT_SRC = ROOT / "services" / "agent" / "src" / "agent"
KNOWLEDGE_SRC = ROOT / "services" / "knowledge" / "src" / "knowledge"
CONF = {"retrieval_verdict": "pass", "top_cosine": 0.6, "tau_low": 0.3, "tau_high": 0.45, "grounding": "pass"}


def gate_step(name: str, verdict: str, decision: str, *, status: str = "ok", reason: str | None = None,
              kind: str = "grounding") -> list[dict[str, Any]]:
    """What: a gate step pair plus its gate event, as the agent emits them. Use when: scripting gates. Inputs: step name,
    verdict, decision, status, error reason, gate kind. Output: partial events. Side effects: none. Role: builder."""
    s0, s1 = step(name, kind="gate", status=status)
    s1["summary"] = {"verdict": verdict, "decision": decision, **({"reason": reason} if reason else {})}
    return [s0, {**gate(verdict, decision, kind=kind), "step_id": s0["step_id"]}, s1]


def model_step(truncated: int = 0) -> list[dict[str, Any]]:
    """What: a model step pair. Use when: scripting the agent model. Inputs: truncated count. Output: partial events.
    Side effects: none. Role: builder."""
    s0, s1 = step("agent", kind="model")
    s1.update(model="claude-sonnet-5-5", summary={"truncated": truncated} if truncated else {})
    return [s0, s1]


def answered(index: int = 0, parts: list[dict[str, Any]] | None = None, **kw: Any) -> TurnResult:
    """What: a passing answer turn: search, retrieval gate, model, grounding pass. Use when: live check tests.
    Inputs: index, parts override, turn() keywords. Output: TurnResult. Side effects: none. Role: builder."""
    if parts is None:
        parts = [*step("search_kb"), *gate_step("retrieval_gate", "pass", "answer", kind="retrieval"), *model_step(),
                 *gate_step("grounding_gate", "pass", "answer")]
    kw.setdefault("confidence", CONF)
    return turn(index, "text", parts, **kw)[0]


ONE_TURN = {"id": "X", "turns": [{"kind": "text", "text": "q", "expect": {"end_reason": "answered"}}]}


def failing(checks: list[Check]) -> list[str]:
    """What: names of non-passing checks. Use when: asserting. Inputs: checks. Output: names. Side effects: none.
    Role: helper."""
    return [c.name for c in checks if c.status != "pass"]


# --- the subset ---------------------------------------------------------------------------------------------------

def test_smoke_cases_are_the_five_named_scenarios_and_s1_chat_is_g03_in_chat() -> None:
    cases = load_cases()
    got = smoke.smoke_cases(cases)
    assert [lb for lb, _, _ in got] == ["S1 verify, commit, policy follow-up", "S3 member decline", "S3 owner approval",
                                        "S4 out of docs", "S5 llm_429 fallback"]
    assert [c["id"] for _, c, _ in got] == ["S1-CHAT", "G-04", "G-05", "G-07", "G-12"]
    s1 = got[0][1]
    g03 = next(c for c in cases if c["id"] == "G-03")
    assert [t["kind"] for t in s1["turns"]] == ["text", "verification", "confirm", "text"]
    assert s1["turns"][3] == {"kind": "text", "text": g03["turns"][3]["question"], "expect": g03["turns"][3]["expect"]}
    assert s1["expected"] == g03["expected"] and g03["turns"][3]["kind"] == "voice"  # the golden case is not mutated
    assert got[4][1]["turns"][0]["fault"] == "llm_429"


# --- the call ledger ----------------------------------------------------------------------------------------------

def test_turn_calls_splits_model_calls_into_agent_verifier_and_classifier() -> None:
    parts = [*step("search_kb"), *gate_step("scope_gate", "none", "escalate", kind="retrieval"), *model_step(),
             *gate_step("grounding_gate", "pass", "answer"), *gate_step("grounding_gate", "skipped", "skip"),
             *gate_step("grounding_gate", "error", "clarify", status="error", reason="no_time")]
    t = turn(0, "text", parts, model_calls=5)[0]
    assert smoke.turn_calls(t) == {"total": 5, "agent": 3, "verifier": 1, "classifier": 1, "search": 1}


def test_turn_calls_is_zero_for_a_rejected_request_and_unknown_without_turn_completed() -> None:
    assert smoke.turn_calls(TurnResult(0, "text", {}, 429)) == {"total": 0, "agent": 0, "verifier": 0, "classifier": 0,
                                                                "search": 0}
    t = turn(0, "text", [], model_calls=2)[0]
    t.events = [e for e in t.events if e["type"] != "turn.completed"]
    assert smoke.turn_calls(t) is None


def _agent(turns: list[TurnResult], cap: int) -> tuple[smoke.CappedAgent, str]:
    """What: a CappedAgent on the double with one scripted session. Use when: cap tests. Inputs: turns, cap.
    Output: (agent, session id). Side effects: none. Role: helper."""
    w = FakeWorld()
    w.next = Script([t.events for t in turns])
    a = smoke.CappedAgent(TestClient(build_app(w), base_url="http://agent.test"), cap)
    a.access(ACCESS_CODE)
    return a, a.create_session()


def test_capped_agent_refuses_the_turn_after_the_cap_is_reached() -> None:
    a, sid = _agent([turn(i, "text", [], model_calls=1)[0] for i in range(3)], cap=2)
    a.label = "S1"
    for i in range(2):
        a.turn(i, "text", sid, {"client_turn_id": f"00000000-0000-4000-8000-00000000000{i}", "text": "hi"}, {})
    with pytest.raises(smoke.BudgetExceeded, match="not sent"):
        a.turn(2, "text", sid, {"client_turn_id": "00000000-0000-4000-8000-000000000002", "text": "hi"}, {})
    assert a.spent == 2 and len(a.results) == 2


def test_capped_agent_aborts_when_a_turn_takes_the_total_past_the_cap_and_keeps_the_result() -> None:
    a, sid = _agent([turn(0, "text", [], model_calls=4)[0]], cap=3)
    with pytest.raises(smoke.BudgetExceeded, match="exceeded: 4"):
        a.turn(0, "text", sid, {"client_turn_id": "00000000-0000-4000-8000-000000000000", "text": "hi"}, {})
    assert len(a.results) == 1 and a.ledger[0][2]["total"] == 4


# --- live checks --------------------------------------------------------------------------------------------------

def test_a_clean_answer_turn_passes_every_live_check() -> None:
    checks = smoke.live_checks(ONE_TURN, [answered()], frozenset({"retrieval", "grounding_pass"}))
    assert failing(checks) == []
    assert {c.name for c in checks} >= {"live.stop.0", "live.truncation.0", "live.gate1.0", "live.gate2.0",
                                        "live.clarify.0", "live.gate1.case", "live.gate2.case"}


@pytest.mark.parametrize(("parts", "kw", "bad"), [
    ([*model_step(truncated=1), *gate_step("grounding_gate", "pass", "answer")], {}, "live.truncation.0"),
    ([*model_step(), *gate_step("grounding_gate", "error", "clarify", status="error", reason="truncated")],
     {"end_reason": "clarify"}, "live.truncation.0"),
    ([*step("search_kb"), *model_step(), *gate_step("grounding_gate", "pass", "answer")], {}, "live.gate1.0"),
    ([*model_step()], {}, "live.gate2.0"),
    ([*model_step(), *gate_step("grounding_gate", "error", "clarify", status="error", reason="timeout")], {}, "live.gate2.0"),
    ([*model_step(), *gate_step("grounding_gate", "fail", "clarify")], {"end_reason": "clarify"}, "live.clarify.0"),
    ([*model_step(), *gate_step("grounding_gate", "pass", "answer")],
     {"text": "I want to be sure I give you the right answer. Could you tell me a little more about what you need?"},
     "live.clarify.0"),
    ([*model_step(), *gate_step("grounding_gate", "pass", "answer")], {"end_reason": "deadline"}, "live.stop.0"),
    ([*model_step(), *gate_step("grounding_gate", "pass", "answer")], {"end_reason": "escalated"}, "live.stop.0"),
])
def test_each_live_failure_is_caught(parts: list[dict[str, Any]], kw: dict[str, Any], bad: str) -> None:
    checks = smoke.live_checks(ONE_TURN, [answered(parts=parts, **kw)], frozenset())
    assert bad in failing(checks)


def test_an_error_event_and_a_missing_turn_completed_fail_stop() -> None:
    t = answered()
    t.events.insert(-1, {**t.events[0], "type": "error", "code": "INTERNAL", "message": "x", "retryable": False})
    assert "live.stop.0" in failing(smoke.live_checks(ONE_TURN, [t], frozenset()))
    t2 = answered()
    t2.events = t2.events[:-1]
    assert "live.stop.0" in failing(smoke.live_checks(ONE_TURN, [t2], frozenset()))
    assert smoke.live_checks(ONE_TURN, [], frozenset())[0].status == "error"


def test_required_case_gates_fail_when_absent() -> None:
    t = answered(parts=[*model_step(), *gate_step("grounding_gate", "skipped", "skip")])
    checks = smoke.live_checks(ONE_TURN, [t], frozenset({"retrieval", "grounding_pass"}))
    assert {"live.gate1.case", "live.gate2.case"} <= set(failing(checks))


def test_a_gate1_clarify_decision_on_a_readback_turn_is_not_the_clarify_line() -> None:
    """What: G-05 live run 1: a weak search (Gate 1 decision clarify) then a readback is not a clarify-line failure.
    Use when: CI. Inputs: none. Output: none. Side effects: none. Role: false positive found by the first live run."""
    case = {"id": "X", "turns": [{"kind": "verification", "expect": {"end_reason": "readback"}}]}
    parts = [*step("search_kb"), *gate_step("retrieval_gate", "weak", "clarify", kind="retrieval"), *model_step(),
             *step("prepare_refund")]
    t = turn(0, "verification", parts, end_reason="readback", text="Shall I send this to our team for approval?")[0]
    assert failing(smoke.live_checks(case, [t], frozenset())) == []


def test_clarify_is_allowed_where_the_turn_expects_it() -> None:
    case = {"id": "X", "turns": [{"kind": "text", "text": "q", "expect": {"end_reason": "clarify"}}]}
    t = answered(parts=[*model_step(), *gate_step("grounding_gate", "fail", "clarify")], end_reason="clarify")
    assert failing(smoke.live_checks(case, [t], frozenset())) == []


# --- patterns pinned to the source (a zero must be able to be non-zero) --------------------------------------------

def test_the_clarify_line_copy_matches_the_agent_source() -> None:
    assert smoke.CLARIFY_LINE in (AGENT_SRC / "gates.py").read_text()


def test_every_agent_log_pattern_matches_a_warning_the_agent_source_logs() -> None:
    src = (AGENT_SRC / "gates.py").read_text() + (AGENT_SRC / "middleware" / "deadline_fault.py").read_text()
    for name, pattern in smoke.AGENT_LOG_PATTERNS.items():
        assert re.search(pattern, src), name


def test_the_knowledge_line_patterns_match_lines_formatted_from_the_source() -> None:
    main = (KNOWLEDGE_SRC / "main.py").read_text()
    fmt = re.search(r'log\.info\("(trace_id=%s request_id=%s %s %s %s %dms)"', main)
    assert fmt is not None
    assert re.search(smoke.SEARCH_LINE, fmt.group(1) % ("t", "r", "POST", "/v1/search", 200, 41))
    assert not re.search(smoke.SEARCH_LINE, fmt.group(1) % ("t", "r", "GET", "/readyz", 200, 3))
    search = (KNOWLEDGE_SRC / "search.py").read_text()
    retry = re.search(r'log\.warning\("(trace_id=%s embedding attempt %d failed: %s)"', search)
    assert retry is not None and re.search(smoke.EMBED_RETRY_LINE, retry.group(1) % ("t", 1, "EmbeddingError"))


def _logs(agent: str, knowledge: str, code: int = 0) -> Any:
    """What: a fake subprocess.run for docker compose logs. Use when: log tests. Inputs: texts, exit code.
    Output: callable. Side effects: none. Role: double."""
    def run(cmd: list[str], **_: Any) -> subprocess.CompletedProcess[str]:
        """What: answer one logs call. Use when: log_findings. Inputs: argv. Output: CompletedProcess.
        Side effects: none. Role: double."""
        assert cmd[:4] == ["docker", "compose", "-p", "cs-smoke-x"] and "logs" in cmd
        return subprocess.CompletedProcess(cmd, code, knowledge if "knowledge" in cmd else agent, "")
    return run


UP = "INFO:     Uvicorn running on http://0.0.0.0:8000\n"


def test_log_findings_counts_warnings_and_searches() -> None:
    agent = UP + "WARNING agent.gates grounding verifier reply truncated (stop_reason max_tokens, output_tokens=1536)\n"
    knowledge = UP + "INFO k trace_id=a request_id=b POST /v1/search 200 40ms\n" * 3
    got = smoke.log_findings("cs-smoke-x", run=_logs(agent, knowledge))
    assert got["verifier reply truncated"] == 1 and got["search requests"] == 3 and got["verifier failed"] == 0


def test_log_findings_refuses_an_unreadable_or_empty_log() -> None:
    with pytest.raises(RuntimeError):
        smoke.log_findings("cs-smoke-x", run=_logs(UP, UP, code=1))
    with pytest.raises(RuntimeError, match="cannot be trusted"):
        smoke.log_findings("cs-smoke-x", run=_logs("", ""))


# --- summary and CLI ----------------------------------------------------------------------------------------------

META = {"when": "2026-11-27T19:00Z", "project": "cs-smoke-x", "edge_port": "3141", "commit": "abc1234",
        "ingest_embedding_calls": "1", "cost": 0.01}
CLEAN_LOGS = {**{k: 0 for k in smoke.AGENT_LOG_PATTERNS}, "search requests": 2, "embedding retries (upper bound)": 0}


def _rows(verdict_ok: bool = True) -> list[smoke.SmokeRow]:
    """What: five summary rows. Use when: summary tests. Inputs: whether all pass. Output: rows. Side effects: none.
    Role: helper."""
    from evals.model import CaseGrade
    ok = [Check("outcome", "pass")]
    return [smoke.SmokeRow(lb, CaseGrade(lb, "train", ok if verdict_ok or i else [Check("outcome", "fail", "x")]),
                           [Check("live.stop.0", "pass")], ["answered"], 3) for i, (_, lb, _) in enumerate(smoke.SMOKE)]


def test_summary_passes_only_when_every_case_passes_the_logs_are_clean_and_the_tree_is_clean() -> None:
    ledger = [("S1", 0, {"total": 3, "agent": 2, "verifier": 1, "classifier": 0, "search": 2})]
    text, ok = smoke.render_summary(_rows(), ledger, CLEAN_LOGS, META, 25, None)
    assert ok and text.splitlines()[0].startswith("LIVE SMOKE ") and text.splitlines()[-1] == "RESULT: PASS"
    assert "agent 2, verifier 1, classifier 0 = 3 model calls (cap 25); embeddings 3 (ingest 1, search 2" in text
    assert len(text.splitlines()) <= 30 and "\u2014" not in text
    assert not smoke.render_summary(_rows(False), ledger, CLEAN_LOGS, META, 25, None)[1]
    assert not smoke.render_summary(_rows(), ledger, None, META, 25, None)[1]
    assert not smoke.render_summary(_rows(), ledger, {**CLEAN_LOGS, "verifier failed": 1}, META, 25, None)[1]
    assert not smoke.render_summary(_rows(), ledger, CLEAN_LOGS, {**META, "dirty": True}, 25, None)[1]
    assert not smoke.render_summary(_rows()[:4], ledger, CLEAN_LOGS, META, 25, None)[1]
    text, ok = smoke.render_summary(_rows(), ledger, CLEAN_LOGS, META, 25, "cap 25 exceeded")
    assert not ok and "ABORTED: cap 25 exceeded" in text and text.endswith("RESULT: FAIL")


def _deps(world: FakeWorld) -> dict[str, Any]:
    """What: CLI doubles. Use when: CLI tests. Inputs: world. Output: kwargs. Side effects: none. Role: helper."""
    return {"agent_http": TestClient(build_app(world), base_url="http://agent.test"), "store": FakeStore(world),
            "before_case": lambda c: None, "after_case": None, "now": dt.datetime(2026, 11, 27, 19, 0, tzinfo=dt.UTC),
            "log_run": _logs(UP, UP)}


ENV = {"DEMO_ACCESS_CODE": ACCESS_CODE, "RUN_REAL": "1", "COMPOSE_PROJECT_NAME": "cs-smoke-x", "SMOKE_COMMIT": "abc1234"}


def test_smoke_refuses_without_run_real_and_sends_nothing(capsys: pytest.CaptureFixture[str]) -> None:
    w = FakeWorld()
    assert cli.main(["smoke"], {k: v for k, v in ENV.items() if k != "RUN_REAL"}, **_deps(w)) == 2
    assert w.requests == [] and w.sessions_created == 0 and "RUN_REAL=1" in capsys.readouterr().err
    assert cli.main(["smoke"], {**ENV, "RUN_REAL": "true"}, **_deps(w)) == 2


def test_smoke_with_no_agent_behind_it_fails_every_case(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    w = FakeWorld()  # no script: every session is a 503, so every case grades error
    assert cli.main(["smoke", "--out-dir", str(tmp_path)], ENV, **_deps(w)) == 1
    out = capsys.readouterr().out
    assert out.count("ERROR") >= 5 and out.rstrip().endswith("RESULT: FAIL")
    assert sorted(p.suffix for p in tmp_path.iterdir()) == [".json", ".txt"]


def test_smoke_aborts_at_the_cap_with_exit_3(capsys: pytest.CaptureFixture[str]) -> None:
    w = FakeWorld()
    deps = _deps(w)
    big = Script([turn(i, "text", [], model_calls=10)[0].events for i in range(4)])
    deps["before_case"] = lambda c: setattr(w, "next", big if c["id"] == "S1-CHAT" else None)
    assert cli.main(["smoke", "--cap", "15"], ENV, **deps) == 3
    out = capsys.readouterr().out
    assert "ABORTED: cap 15 exceeded: 20 real model calls after S1 verify, commit, policy follow-up turn 1" in out
    assert len(w.requests) == 2  # no third turn, no later case
