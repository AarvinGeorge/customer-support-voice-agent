"""What: tests for the Gate 1 confusion counts, the dated report (markdown and JSON, raw counts) and the CLI.
Use when: CI. Inputs: stub search responses, the fake agent and store. Output: pass or fail. Side effects: writes to tmp dirs only.
Role: the runner's output side and its single entry point `python -m evals run`."""
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from starlette.testclient import TestClient

from evals import cli, gate1, report, reseed
from evals.agent_client import AgentClient
from evals.cases import load_cases, load_personas, load_retrieval
from evals.model import CaseGrade, Check
from evals.tests.fakes import ACCESS_CODE, FakeStore, FakeWorld, build_app
from evals.tests.test_runner import g01_script

Q = [
    {"id": "R-01", "split": "train", "label": "answerable", "query": "refund?", "tier": "public", "verified": False, "gold_chunk_ids": ["a#x", "a#y"]},
    {"id": "R-02", "split": "test", "label": "answerable", "query": "sso?", "tier": "pro", "verified": True, "gold_chunk_ids": ["b#z"], "error_code": "E-SSO-401"},
    {"id": "R-03", "split": "train", "label": "not_in_docs", "query": "android?", "tier": "public", "verified": False},
    {"id": "R-04", "split": "test", "label": "off_topic", "query": "weather?", "tier": "public", "verified": False},
    {"id": "R-05", "split": "train", "label": "not_in_docs", "query": "okta?", "tier": "free", "verified": True, "forbidden_chunk_ids": ["sso#okta"]},
]


def resp(verdict: str, ids: list[str]) -> dict[str, Any]:
    """What: a minimal SearchResponse. Use when: stubbing knowledge. Inputs: verdict, chunk ids. Output: dict. Side effects: none. Role: helper."""
    return {"chunks": [{"chunk_id": i} for i in ids], "confidence": {"verdict": verdict, "top_cosine": 0.5}}


ANSWERS = {"refund?": resp("pass", ["z#q", "m#m", "n#n", "a#y~2"]), "sso?": resp("weak", ["q#q"] * 3 + ["b#z"]), "android?": resp("pass", ["m#n"]),
           "weather?": resp("none", []), "okta?": resp("pass", ["sso#okta", "n#n"])}


def search(q: dict[str, Any]) -> dict[str, Any]:
    """What: stub search function. Use when: run_gate1 tests. Inputs: query dict. Output: response. Side effects: none. Role: stub."""
    return ANSWERS[q["query"]]


def test_confusion_counts_per_split_and_label_are_raw() -> None:
    g = gate1.run_gate1(Q, search)
    assert g["confusion"]["train"]["answerable"] == {"pass": 1}
    assert g["confusion"]["train"]["not_in_docs"] == {"pass": 2}
    assert g["confusion"]["test"]["answerable"] == {"weak": 1} and g["confusion"]["test"]["off_topic"] == {"none": 1}
    assert g["n"] == 5


def test_recall_at_k_counts_hits_by_prefix_and_ignores_the_split_suffix() -> None:
    g = gate1.run_gate1(Q, search)
    assert g["recall_at_k"]["train"]["3"] == {"hits": 0, "n": 1}  # gold a#y shows as a#y~2 at rank 4: the suffix is ignored
    assert g["recall_at_k"]["train"]["4"] == {"hits": 1, "n": 1}
    assert g["recall_at_k"]["test"]["3"] == {"hits": 0, "n": 1}  # b#z is rank 4
    assert g["recall_at_k"]["test"]["4"] == {"hits": 1, "n": 1}


def test_forbidden_chunk_returned_is_counted_as_a_tier_leak() -> None:
    g = gate1.run_gate1(Q, search)
    assert g["tier_leaks"] == [{"id": "R-05", "chunk_ids": ["sso#okta"]}]


def test_failed_search_is_counted_request_failed_never_pass() -> None:
    def flaky(q: dict[str, Any]) -> dict[str, Any]:
        if q["id"] == "R-01":
            raise gate1.SearchFailed("HTTP 503")
        return ANSWERS[q["query"]]
    g = gate1.run_gate1(Q, flaky)
    assert g["confusion"]["train"]["answerable"] == {"request_failed": 1}
    assert g["failures"] == [{"id": "R-01", "error": "HTTP 503"}]


def test_knowledge_client_sends_tier_verified_k_8_and_the_bearer_token() -> None:
    seen: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        return httpx.Response(200, json=resp("pass", ["a#x"]))

    kc = gate1.KnowledgeClient(httpx.Client(transport=httpx.MockTransport(handler), base_url="http://k.test"), "tok-123")
    kc.search(Q[1])
    body = json.loads(seen[0].content)
    assert seen[0].url.path == "/v1/search" and seen[0].headers["authorization"] == "Bearer tok-123"
    assert body == {"query": "sso?", "tier": "pro", "verified": True, "k": 8, "error_code": "E-SSO-401", "calibration": False}


def test_knowledge_client_raises_search_failed_on_http_error() -> None:
    kc = gate1.KnowledgeClient(httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(503, json={})), base_url="http://k.test"), "t")
    with pytest.raises(gate1.SearchFailed):
        kc.search(Q[0])


def test_the_shipped_queries_run_through_run_gate1() -> None:
    qs = load_retrieval()
    g = gate1.run_gate1(qs, lambda q: resp("none", []))
    assert g["n"] == 25 and sum(sum(v.values()) for s in ("train", "test") for v in g["confusion"][s].values()) == 25


GRADES = [CaseGrade("G-01", "train", [Check("outcome", "pass"), Check("trajectory", "pass")]),
          CaseGrade("G-02", "train", [Check("outcome", "fail", "x"), Check("trajectory", "error", "missing")]),
          CaseGrade("G-03", "test", [Check("outcome", "pass")]),
          CaseGrade("G-04", "test", [Check("outcome", "error", "no turn")])]


def test_report_counts_verdicts_per_split_and_per_check() -> None:
    r = report.build_report(GRADES, gate1.run_gate1(Q, search), meta={"agent_url": "http://x"}, now=dt.datetime(2026, 11, 27, 14, 5, tzinfo=dt.UTC))
    assert r["cases"]["total"] == 4 and r["cases"]["verdicts"] == {"pass": 2, "fail": 1, "error": 1}
    assert r["cases"]["by_split"]["train"] == {"pass": 1, "fail": 1, "error": 0}
    assert r["cases"]["by_split"]["test"] == {"pass": 1, "fail": 0, "error": 1}
    assert r["checks"]["outcome"] == {"pass": 2, "fail": 1, "error": 1} and r["checks"]["trajectory"] == {"pass": 1, "fail": 0, "error": 1}
    assert r["generated_at"] == "2026-11-27T14:05:00+00:00" and r["meta"]["agent_url"] == "http://x"
    assert r["cases"]["detail"][1]["checks"][0] == {"name": "outcome", "status": "fail", "detail": "x"}


def test_report_without_gate1_says_so_instead_of_inventing_counts() -> None:
    r = report.build_report(GRADES, None, meta={}, now=dt.datetime(2026, 11, 27, tzinfo=dt.UTC))
    assert r["gate1"] is None and "not run" in report.render_markdown(r)


def test_write_report_makes_dated_markdown_and_json(tmp_path: Path) -> None:
    now = dt.datetime(2026, 11, 27, 14, 5, 9, tzinfo=dt.UTC)
    r = report.build_report(GRADES, gate1.run_gate1(Q, search), meta={}, now=now)
    md, js = report.write_report(r, tmp_path, now)
    assert md.name == "golden-2026-11-27-140509.md" and js.name == "golden-2026-11-27-140509.json"
    assert json.loads(js.read_text())["cases"]["total"] == 4
    text = md.read_text()
    assert "G-02" in text and "Gate 1" in text and "train" in text and chr(0x2014) not in text


def test_markdown_has_no_em_dash_and_lists_failing_checks() -> None:
    md = report.render_markdown(report.build_report(GRADES, None, meta={}, now=dt.datetime(2026, 11, 27, tzinfo=dt.UTC)))
    assert "outcome" in md and "missing" in md


def make_deps() -> tuple[FakeWorld, dict[str, Any]]:
    """What: injectable dependencies for the CLI wired to the double. Use when: CLI tests. Inputs: none. Output: world and kwargs.
    Side effects: none. Role: helper."""
    w = FakeWorld()
    http = TestClient(build_app(w), base_url="http://agent.test")
    return w, {"agent_http": http, "store": FakeStore(w), "search": lambda q: ANSWERS.get(q["query"], resp("none", [])),
               "before_case": lambda c: setattr(w, "next", g01_script() if c["id"] == "G-01" else None), "after_case": None,
               "now": dt.datetime(2026, 11, 27, 15, 0, 0, tzinfo=dt.UTC)}


ENV = {"DEMO_ACCESS_CODE": ACCESS_CODE}


def test_python_m_evals_run_grades_all_cases_against_the_fake_and_writes_a_report(tmp_path: Path) -> None:
    _w, deps = make_deps()
    code = cli.main(["run", "--out-dir", str(tmp_path)], ENV, **deps)
    files = sorted(p.name for p in tmp_path.iterdir())
    assert files == ["golden-2026-11-27-150000.json", "golden-2026-11-27-150000.md"]
    rep = json.loads((tmp_path / files[0]).read_text())
    assert rep["cases"]["total"] == 21
    # only G-01 is scripted; the rest are error, never pass. The two D-083 cases are counted apart (known limitations).
    assert rep["cases"]["verdicts"] == {"pass": 1, "fail": 0, "error": 18}
    assert rep["cases"]["known_limitation"]["verdicts"] == {"pass": 0, "fail": 0, "error": 2}
    assert rep["gate1"]["n"] == 25
    assert code == 1  # not every case passed


def test_exit_code_is_zero_only_when_every_graded_case_passes(tmp_path: Path) -> None:
    _w, deps = make_deps()
    assert cli.main(["run", "--cases", "G-01", "--skip-gate1", "--out-dir", str(tmp_path)], ENV, **deps) == 0
    rep = json.loads(next(tmp_path.glob("*.json")).read_text())
    assert rep["cases"]["total"] == 1 and rep["gate1"] is None


def test_cli_without_an_access_code_fails_loudly(tmp_path: Path) -> None:
    _w, deps = make_deps()
    assert cli.main(["run", "--out-dir", str(tmp_path)], {}, **deps) == 2
    assert list(tmp_path.iterdir()) == []


def test_cli_rejects_an_unknown_case_id(tmp_path: Path) -> None:
    _w, deps = make_deps()
    assert cli.main(["run", "--cases", "G-99", "--out-dir", str(tmp_path)], ENV, **deps) == 2


def test_agent_client_access_failure_is_exit_2(tmp_path: Path) -> None:
    _w, deps = make_deps()
    assert cli.main(["run", "--out-dir", str(tmp_path)], {"DEMO_ACCESS_CODE": "wrong"}, **deps) == 2


def test_all_golden_cases_and_personas_still_load() -> None:
    assert len(load_cases()) == 21 and "Natasha Romanoff" in load_personas() and AgentClient and ACCESS_CODE


def test_a_malformed_search_body_is_request_failed_and_does_not_lose_the_run() -> None:
    g = gate1.run_gate1(Q, lambda q: {} if q["id"] == "R-01" else ANSWERS[q["query"]])
    assert g["confusion"]["train"]["answerable"] == {"request_failed": 1} and g["n"] == 5


def test_knowledge_client_non_json_200_is_search_failed() -> None:
    kc = gate1.KnowledgeClient(httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, text="oops")), base_url="http://k.test"), "t")
    with pytest.raises(gate1.SearchFailed):
        kc.search(Q[0])


def test_default_reseed_uses_the_seed_core_job_that_holds_the_owner_url() -> None:
    args = " ".join(reseed.RESEED_ARGS)
    assert "seed_core" in args and "--profile seed" in args and " exec " not in args and "DEMO_RESEED=true" in args


LIVE_ENV = {**ENV, "CORE_DB_URL": "postgresql://core", "AGENT_DB_URL": "postgresql://agent"}


def test_cli_refuses_an_unguarded_reseed_target_before_running_anything(tmp_path: Path) -> None:
    """What: with the real hooks and no COMPOSE_PROJECT_NAME, the CLI exits 2 and writes nothing.
    Use when: CI. Inputs: tmp dir. Output: none. Side effects: none. Role: PR #60 follow-up (DEFAULT_RESEED had no guard)."""
    _w, deps = make_deps()
    del deps["before_case"], deps["after_case"]
    assert cli.main(["run", "--skip-gate1", "--out-dir", str(tmp_path)], LIVE_ENV, **deps) == 2
    assert list(tmp_path.iterdir()) == []


def test_cli_reseeds_each_case_through_the_shared_guard(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """What: the default before-case hook calls reseed.guarded_reseed with the env, agent URL and core DB, once per case,
    after one up-front reseed_command check. Use when: CI. Inputs: tmp dir, monkeypatch. Output: none. Side effects: none.
    Role: one guard, no second copy."""
    _w, deps = make_deps()
    del deps["before_case"], deps["after_case"]
    checks: list[str] = []
    seen: list[tuple[str, str]] = []
    monkeypatch.setattr(reseed, "reseed_command", lambda env, url, root, run=None: checks.append(url) or ["x"])
    monkeypatch.setattr(reseed, "guarded_reseed", lambda env, url, core_db, root, **k: seen.append((url, core_db)))
    env = {**LIVE_ENV, "EVALS_AGENT_URL": "http://localhost:3095"}
    cli.main(["run", "--cases", "G-01,G-02", "--skip-gate1", "--out-dir", str(tmp_path)], env, **deps)
    assert checks == ["http://localhost:3095"]
    assert seen == [("http://localhost:3095", "postgresql://core")] * 2


def test_report_prints_which_decision_path_each_run_took() -> None:
    """Review PR #84 I3: a pass on the trivial path must be visible in the report, not silent."""
    g = CaseGrade("G-19", "train", [Check("outcome", "pass"), Check("path:retry_after_rejected_prepare", "pass", "not exercised: no rejected prepare", exercised=False)])
    g2 = CaseGrade("G-18", "train", [Check("path:answer_from_session_evidence", "pass", "exercised", exercised=True)])
    r = report.build_report([g, g2], None, meta={}, now=dt.datetime(2026, 11, 27, tzinfo=dt.UTC))
    assert r["paths"] == [{"case": "G-19", "probe": "retry_after_rejected_prepare", "exercised": False, "detail": "not exercised: no rejected prepare"},
                          {"case": "G-18", "probe": "answer_from_session_evidence", "exercised": True, "detail": "exercised"}]
    md = report.render_markdown(r)
    assert "Decision paths" in md and "NOT exercised" in md and "G-19" in md
    plain = report.build_report(GRADES, None, meta={}, now=dt.datetime(2026, 11, 27, tzinfo=dt.UTC))
    assert plain["paths"] == [] and "Decision paths" in report.render_markdown(plain)
