"""What: tests for the agent client and the runner against the FastAPI double and the fake store (no network, no providers).
Use when: CI. Inputs: scripted turns built with tests/support.py. Output: pass or fail. Side effects: none.
Role: proves the runner sends what the browser sends (07 s2.3, s6.3), reseeds per case, and grades missing data error."""
from __future__ import annotations

from typing import Any

import pytest
from contracts.policy import load_policy
from starlette.testclient import TestClient

from evals import cases, runner
from evals.agent_client import AgentClient
from evals.hooks import HookError, make_hooks
from evals.tests.fakes import ACCESS_CODE, FakeStore, FakeWorld, Script, build_app
from evals.tests.support import ROOT, case_by_id, gate, pending, step, turn
from evals.tests.test_graders import ASK, PA, PRO_RULE, VERIFY_REQ

POLICY = load_policy(ROOT / "data/policy.yaml")
PERSONAS = cases.load_personas()


def world_and_client() -> tuple[FakeWorld, AgentClient, FakeStore]:
    """What: a wired double, client and store. Use when: each test. Inputs: none. Output: the three. Side effects: none. Role: helper."""
    w = FakeWorld()
    http = TestClient(build_app(w), base_url="http://agent.test")
    return w, AgentClient(http), FakeStore(w)


def g01_script() -> Script:
    """What: the three turns of G-01 plus the rows core would hold. Use when: a happy-path test. Inputs: none. Output: Script.
    Side effects: none. Role: scripted agent."""
    t0 = turn(0, "text", [*step("search_kb"), *step("request_verification"), VERIFY_REQ], end_reason="verification_requested", text="Please verify.")
    t1 = turn(1, "verification", [*step("list_charges"), *step("prepare_refund", rule=PRO_RULE), ASK], end_reason="readback",
              text="Refund $96.00 for the charge ending 0412?")
    t2 = turn(2, "confirm", [*step("commit", kind="commit", args={"pending_action_id": PA}),
                              {"type": "action.result", "pending_action_id": PA, "action_type": "refund", "outcome": "committed", "changed": True,
                               "reason_codes": ["PRO_WITHIN_LIMITS"], "policy_refs": [{"article": "refund-policy", "section": "pro-plan-refunds", "title": "T"}],
                               "result_text": "Refunded.", "ticket": None}],
              end_reason="committed", text="Refunded $96.00 for the charge ending 0412.", say="Refunded ninety six dollars for the charge ending 0412.")
    row = {"id": PA, "action_type": "refund", "target_key": "refund:CH-00000412", "params": {"charge_id": "CH-00000412", "amount_cents": 9600},
           "state": "committed", "reason_codes": ["PRO_WITHIN_LIMITS"]}
    return Script([t0[0].events, t1[0].events, t2[0].events], pending=[row], refunds=[{"target_key": "refund:CH-00000412", "amount_cents": 9600}])


def run_one(case_id: str, script: Script | None, *, store_down: bool = False) -> tuple[FakeWorld, Any, Any]:
    """What: run one case against the double. Use when: a runner test. Inputs: case id, script, store flag.
    Output: world, the CaseRun and its grade. Side effects: none. Role: helper."""
    w, client, store = world_and_client()
    w.next, w.store_down = script, store_down
    client.access(ACCESS_CODE)
    case = case_by_id(case_id)
    run = runner.run_case(case, PERSONAS[case["persona"]], client, store)
    from evals import graders
    return w, run, graders.grade_case(case, run, POLICY)


def test_g01_runs_end_to_end_and_passes() -> None:
    w, run, grade = run_one("G-01", g01_script())
    assert grade.verdict == "pass", [(c.name, c.detail) for c in grade.checks if c.status != "pass"]
    assert w.sessions_created == 1 and len(run.turns) == 3


def test_requests_match_the_browser_contract() -> None:
    w, _run, _g = run_one("G-01", g01_script())
    t0, t1, t2 = (r["body"] for r in w.requests)
    assert set(t0) == {"client_turn_id", "text"} and "0412" in t0["text"]
    assert t1["verification"] == {"account_id": "QS-10423", "pin": PERSONAS["Natasha Romanoff"]["pin"]} and "text" not in t1
    assert t2["confirm"] == {"pending_action_id": PA, "decision": "confirm"}  # no step_up_pin unless the card requires step-up
    assert len({r["body"]["client_turn_id"] for r in w.requests}) == 3


def test_step_up_card_gets_the_persona_pin_on_confirm() -> None:
    s = g01_script()
    t1 = turn(1, "verification", [*step("prepare_refund", rule=PRO_RULE),
                                  {"type": "confirmation.required", "pending": {**pending(PA), "requires_step_up": True}, "confirm_label": "Confirm"}],
              end_reason="readback")
    s.turns[1] = t1[0].events
    w, *_ = run_one("G-01", s)
    assert w.requests[2]["body"]["confirm"]["step_up_pin"] == PERSONAS["Natasha Romanoff"]["pin"]


def test_fault_header_is_sent_on_that_turn_only() -> None:
    t0 = turn(0, "verification", [], end_reason="verified")
    t1 = turn(1, "text", [*step("prepare_refund", status="error"), *step("prepare_refund", status="error")], end_reason="escalated")
    w, *_ = run_one("G-10", Script([t0[0].events, t1[0].events]))
    assert [r["fault"] for r in w.requests] == [None, "tool_error"]


def test_typed_pin_is_substituted_into_the_request_but_never_into_the_grade() -> None:
    t0 = turn(0, "text", [VERIFY_REQ], end_reason="verification_requested")
    t1 = turn(1, "verification", [*step("get_account_summary")], end_reason="answered", text="You are on Pro.")
    w, _run, grade = run_one("G-02", Script([t0[0].events, t1[0].events]))
    assert PERSONAS["Natasha Romanoff"]["pin"] in w.requests[0]["body"]["text"]
    assert all(PERSONAS["Natasha Romanoff"]["pin"] not in c.detail for c in grade.checks)


def test_voice_turns_use_voice_ask_with_a_registered_voice_session() -> None:
    first = g01_script()
    voice1 = turn(3, "voice", [*step("search_kb"), gate("pass", "answer")], end_reason="answered", text="It went through.",
                  citations=["refund-policy#how-refunds-work"])
    voice2 = turn(4, "voice", [*step("search_kb"), gate("pass", "answer")], end_reason="answered", text="Invoice ending 0412.")
    first.turns += [voice1[0].events, voice2[0].events]
    w, run, _g = run_one("G-03", first)
    v = [r for r in w.requests if r["path"] == "voice"]
    assert len(v) == 2 and v[0]["body"]["voice_session_id"] == "vs_fake_1"
    assert v[0]["body"]["transcript_final"] is True and v[0]["body"]["user_transcript"].startswith("did my refund go through")
    assert v[0]["body"]["last_assistant_interrupted"] is False and v[1]["body"]["last_assistant_interrupted"] is True
    assert v[0]["body"]["realtime_call_id"] != v[1]["body"]["realtime_call_id"] and v[0]["body"]["user_item_id"]
    assert len(run.turns) == 5


def test_no_script_means_the_case_is_error_not_pass() -> None:
    _w, run, grade = run_one("G-01", None)
    assert run.error and grade.verdict == "error"


def test_http_error_mid_case_stops_the_case_and_is_not_pass() -> None:
    w, client, store = world_and_client()
    w.next, w.fail_turn_with = g01_script(), 1
    client.access(ACCESS_CODE)
    run = runner.run_case(case_by_id("G-01"), PERSONAS["Natasha Romanoff"], client, store)
    from evals import graders
    g = graders.grade_case(case_by_id("G-01"), run, POLICY)
    assert len(run.turns) == 2 and run.turns[1].status_code == 500 and "HTTP 500" in (run.error or "")
    assert g.verdict != "pass"


def test_store_down_grades_row_and_trajectory_checks_error() -> None:
    _w, run, grade = run_one("G-01", g01_script(), store_down=True)
    st = {c.name: c.status for c in grade.checks}
    assert run.data is None and st["final_state.refunds"] == "error" and st["trajectory"] == "error"
    assert grade.verdict == "error"


def test_confirm_with_no_card_shown_is_an_error() -> None:
    t0 = turn(0, "text", [VERIFY_REQ], end_reason="verification_requested")
    t1 = turn(1, "verification", [], end_reason="readback")  # no confirmation.required
    _w, run, grade = run_one("G-01", Script([t0[0].events, t1[0].events]))
    assert "no confirmation.required" in (run.error or "") and grade.verdict != "pass"


def test_invalid_event_from_the_agent_is_a_contract_error() -> None:
    t0 = turn(0, "text", [VERIFY_REQ], end_reason="verification_requested")
    bad = [dict(e) for e in t0[0].events]
    bad[-1]["end_reason"] = "not_a_real_reason"
    _w, run, _g = run_one("G-01", Script([bad]))
    assert run.turns[0].error and "contract" in run.turns[0].error


def test_run_cases_grades_every_case_and_unscripted_ones_are_error() -> None:
    w, client, store = world_and_client()
    client.access(ACCESS_CODE)
    all_cases = cases.load_cases()

    def before(case: dict[str, Any]) -> None:
        w.next = g01_script() if case["id"] == "G-01" else None

    result = runner.run_cases(all_cases, PERSONAS, client, store, POLICY, before_case=before)
    verdicts = {g.case_id: g.verdict for g in result.grades}
    assert len(verdicts) == 21 and verdicts["G-01"] == "pass"
    assert all(v == "error" for k, v in verdicts.items() if k != "G-01")


def test_run_cases_filters_by_id_and_split() -> None:
    w, client, store = world_and_client()
    client.access(ACCESS_CODE)
    result = runner.run_cases(cases.load_cases(), PERSONAS, client, store, POLICY, only=["G-01"], before_case=lambda c: setattr(w, "next", g01_script()))
    assert [g.case_id for g in result.grades] == ["G-01"]
    test_only = runner.select(cases.load_cases(), None, "test")
    assert {c["split"] for c in test_only} == {"test"} and len(test_only) == 7


def test_a_failing_hook_makes_that_case_error_and_later_cases_still_run() -> None:
    w, client, store = world_and_client()
    client.access(ACCESS_CODE)

    def before(case: dict[str, Any]) -> None:
        if case["id"] == "G-01":
            raise HookError("reseed exited 3")
        w.next = None

    result = runner.run_cases(cases.load_cases(), PERSONAS, client, store, POLICY, only=["G-01", "G-06"], before_case=before)
    assert [g.verdict for g in result.grades] == ["error", "error"]
    assert "reseed exited 3" in (result.runs[0].error or "")


def test_hooks_run_reseed_then_fixture_ingest_and_fail_loudly() -> None:
    calls: list[list[str]] = []

    class Done:
        returncode = 0
        stderr = ""

    def ok(cmd: list[str], **kw: Any) -> Done:
        calls.append(cmd)
        return Done()

    before, after = make_hooks("docker compose exec core reseed", "ingest {path}", "remove {path}", run=ok)
    before(case_by_id("G-14"))
    assert calls[0][-1] == "reseed" and calls[1][0] == "ingest" and calls[1][1].endswith("bulk-export-tips-fixture.md")
    after(case_by_id("G-14"))
    assert calls[2][0] == "remove"
    calls.clear()
    before(case_by_id("G-01"))
    assert len(calls) == 1  # no fixtures, only the reseed

    class Bad(Done):
        returncode = 3
        stderr = "refusing to reseed"

    before_bad, _ = make_hooks("reseed", None, None, run=lambda cmd, **kw: Bad())
    with pytest.raises(HookError, match="refusing"):
        before_bad(case_by_id("G-01"))


def test_a_case_with_fixtures_and_no_ingest_command_is_an_error_not_a_skip() -> None:
    before, _ = make_hooks("reseed", None, None, run=lambda cmd, **kw: type("D", (), {"returncode": 0, "stderr": ""})())
    with pytest.raises(HookError, match="fixture"):
        before(case_by_id("G-14"))


def test_grade_details_never_contain_a_persona_pin() -> None:
    _w, _run, grade = run_one("G-02", Script([turn(0, "text", [VERIFY_REQ], end_reason="verification_requested")[0].events]))
    assert PERSONAS["Natasha Romanoff"]["pin"] not in repr(grade)


def test_hook_commands_that_cannot_start_or_time_out_are_hook_errors() -> None:
    import subprocess

    def missing(cmd: list[str], **kw: Any) -> None:
        raise FileNotFoundError("docker")

    def slow(cmd: list[str], **kw: Any) -> None:
        raise subprocess.TimeoutExpired(cmd, 300)

    for fn in (missing, slow):
        before, _ = make_hooks("reseed", None, None, run=fn)
        with pytest.raises(HookError):
            before(case_by_id("G-01"))


def test_a_callable_reseed_runs_and_its_errors_are_hook_errors() -> None:
    """What: make_hooks accepts the guarded reseed as a callable; a ReseedError becomes a HookError (the case errors).
    Use when: CI. Inputs: none. Output: none. Side effects: none. Role: the golden CLI's guarded reseed."""
    from evals.reseed import ReseedRefused
    calls: list[str] = []
    before, _ = make_hooks(lambda: calls.append("reseed"), None, None)
    before(case_by_id("G-01"))
    assert calls == ["reseed"]

    def refuse() -> None:
        """What: a refused reseed. Use when: this test. Inputs: none. Output: none. Side effects: raises. Role: fake."""
        raise ReseedRefused("COMPOSE_PROJECT_NAME must name the stack under test")
    before_bad, _ = make_hooks(refuse, None, None)
    with pytest.raises(HookError, match="COMPOSE_PROJECT_NAME"):
        before_bad(case_by_id("G-01"))
