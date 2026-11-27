"""What: offline tests for the S5 e2e module's helpers (reseed per case, the fallback badge check), with no stack.
Use when: CI and local runs; the e2e tests themselves are skipped unless EVALS_E2E=1.
Inputs: none. Output: pytest results. Side effects: none (subprocess, runner and graders are faked).
Role: keeps the e2e harness honest; both tests come from the live rerun after PRs #52 and #58.
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from evals.tests import test_s5_faults_e2e as s5


def test_play_reseeds_before_every_case(monkeypatch: pytest.MonkeyPatch) -> None:
    """What: _play reseeds core before each case, like the golden CLI. Use when: CI. Inputs: monkeypatch.
    Output: none. Side effects: none. Role: G-11 must not merge into G-10's open ticket (doc 07 s7 ticket merge)."""
    calls: list[str] = []
    monkeypatch.delenv("EVALS_E2E_OUT", raising=False)
    monkeypatch.setattr(s5, "_reseed", lambda stack: calls.append("reseed"))
    monkeypatch.setattr(s5, "run_case", lambda *a, **k: calls.append("run") or SimpleNamespace())
    monkeypatch.setattr(s5.graders, "grade_case", lambda *a, **k: SimpleNamespace(verdict="pass", checks=[]))
    stack: dict[str, Any] = {"cases": {"G-10": {"persona": "p"}, "G-11": {"persona": "p"}}, "personas": {"p": {}},
                             "agent": None, "store": None, "policy": None}
    s5._play(stack, "G-10")
    s5._play(stack, "G-11")
    assert calls == ["reseed", "run", "reseed", "run"]


def _model_step(model: str) -> dict[str, Any]:
    """What: a completed model step event. Use when: building fake turns. Inputs: model id. Output: event dict.
    Side effects: none. Role: helper."""
    return {"type": "step.completed", "kind": "model", "model": model, "status": "ok"}


def _badge() -> dict[str, Any]:
    """What: a model.fallback event for the llm_429 fault. Use when: building fake turns. Inputs: none.
    Output: event dict. Side effects: none. Role: helper."""
    return {"type": "model.fallback", "reason": "fault_llm_429", "from_model": "claude-sonnet-5-5", "to_model": "gpt-5.4-mini"}


def test_fallback_badges_one_per_model_call() -> None:
    """What: the fault fires on the primary for the whole turn (doc 07 s10), so each model call shows one badge.
    Use when: CI. Inputs: none. Output: none. Side effects: none. Role: a turn with 2 model calls has 2 badges."""
    two = [_badge(), _model_step("gpt-5.4-mini-2026-03-17"), _badge(), _model_step("gpt-5.4-mini-2026-03-17")]
    assert [b["reason"] for b in s5._fallback_badges(two)] == ["fault_llm_429"] * 2
    with pytest.raises(AssertionError):
        s5._fallback_badges([_badge(), _model_step("gpt-5.4-mini"), _model_step("gpt-5.4-mini")])
    with pytest.raises(AssertionError):
        s5._fallback_badges([_model_step("claude-sonnet-5-5")])


GOOD_ENV = {"COMPOSE_PROJECT_NAME": "cs-a5", "DEMO_FAULTS_ENABLED": "true"}
URL = "http://localhost:3095"


def _port_run(published: str, code: int = 0) -> Any:
    """What: a fake subprocess.run for `docker compose port caddy 80`. Use when: guard tests. Inputs: published
    address, exit code. Output: the fake and its call list. Side effects: none. Role: helper."""
    seen: list[list[str]] = []

    def run(cmd: list[str], **_: Any) -> SimpleNamespace:
        """What: record the command, answer the port. Use when: the guard runs. Inputs: cmd. Output: result.
        Side effects: appends to seen. Role: fake."""
        seen.append(cmd)
        return SimpleNamespace(returncode=code, stdout=published + "\n", stderr="")
    run.seen = seen  # type: ignore[attr-defined]
    return run


@pytest.mark.parametrize("env, why", [
    ({"DEMO_FAULTS_ENABLED": "true"}, "COMPOSE_PROJECT_NAME"),
    ({**GOOD_ENV, "COMPOSE_PROJECT_NAME": "development"}, "cs-"),
    ({**GOOD_ENV, "COMPOSE_PROJECT_NAME": "customer-support-voice-agent"}, "cs-"),
    ({**GOOD_ENV, "COMPOSE_PROJECT_NAME": "cs-prod"}, "production"),
    ({**GOOD_ENV, "COMPOSE_PROJECT_NAME": "cs-production-b"}, "production"),
    ({"COMPOSE_PROJECT_NAME": "cs-a5"}, "DEMO_FAULTS_ENABLED"),
    ({**GOOD_ENV, "DEMO_FAULTS_ENABLED": "false"}, "DEMO_FAULTS_ENABLED"),
])
def test_reseed_guard_refuses_bad_env(tmp_path: Path, env: dict[str, str], why: str) -> None:
    """What: no project name, a non-lane or production-like name, or the fault flag off: refused before any docker call.
    Use when: CI. Inputs: tmp root, env, expected words. Output: none. Side effects: none. Role: review I-1."""
    run = _port_run("127.0.0.1:3095")
    with pytest.raises(s5.ReseedRefused, match=why):
        s5.reseed_command(env, URL, tmp_path, run)
    assert run.seen == []


def test_reseed_guard_refuses_on_the_vm_marker(tmp_path: Path) -> None:
    """What: a checkout with deploy.sh's state dir (.deploy) is the VM: refused. Use when: CI. Inputs: tmp root.
    Output: none. Side effects: creates tmp_path/.deploy. Role: never wipe the public demo."""
    (tmp_path / ".deploy").mkdir()
    with pytest.raises(s5.ReseedRefused, match=".deploy"):
        s5.reseed_command(GOOD_ENV, URL, tmp_path, _port_run("127.0.0.1:3095"))


@pytest.mark.parametrize("url", ["https://demo.example.com", "http://10.0.0.5:3095", "http://localhost"])
def test_reseed_guard_refuses_a_non_loopback_url(tmp_path: Path, url: str) -> None:
    """What: the base URL must be loopback with an explicit port (the local override's EDGE_PORT). Use when: CI.
    Inputs: tmp root, url. Output: none. Side effects: none. Role: the VM serves 80 and 443 on a domain."""
    with pytest.raises(s5.ReseedRefused, match="EVALS_AGENT_URL"):
        s5.reseed_command(GOOD_ENV, url, tmp_path, _port_run("127.0.0.1:3095"))


@pytest.mark.parametrize("published, code", [("127.0.0.1:3000", 0), ("0.0.0.0:80", 0), ("", 1)])
def test_reseed_guard_refuses_when_the_project_does_not_serve_the_url(tmp_path: Path, published: str, code: int) -> None:
    """What: the named project's Caddy must publish the URL's port, else the reseed would hit another stack.
    Use when: CI. Inputs: tmp root, published address, exit code. Output: none. Side effects: none. Role: review I-1."""
    with pytest.raises(s5.ReseedRefused, match="cs-a5"):
        s5.reseed_command(GOOD_ENV, URL, tmp_path, _port_run(published, code))


def test_reseed_guard_accepts_the_stack_under_test(tmp_path: Path) -> None:
    """What: a matching stack gives the reseed command pinned with -p to that project. Use when: CI. Inputs: tmp root.
    Output: none. Side effects: none. Role: the truncate can only reach the stack behind EVALS_AGENT_URL."""
    run = _port_run("127.0.0.1:3095")
    cmd = s5.reseed_command(GOOD_ENV, URL, tmp_path, run)
    assert run.seen == [["docker", "compose", "-p", "cs-a5", "port", "caddy", "80"]]
    assert cmd[:4] == ["docker", "compose", "-p", "cs-a5"] and cmd[-1] == "--reseed"


def test_reseed_fails_when_the_graded_database_was_not_reseeded(monkeypatch: pytest.MonkeyPatch) -> None:
    """What: after the reseed, the reseed audit row in EVALS_CORE_DB_URL must be newer than before. Use when: CI.
    Inputs: monkeypatch. Output: none. Side effects: none. Role: proves the reseeded database is the graded one."""
    monkeypatch.delenv("EVALS_E2E_RESEED", raising=False)
    monkeypatch.setattr(s5, "reseed_command", lambda *a, **k: ["true"])
    monkeypatch.setattr(s5.subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=0, stdout="", stderr=""))
    stamps = iter(["2026-11-27 15:00", "2026-11-27 15:00"])
    monkeypatch.setattr(s5, "_last_reseed_ts", lambda core_db: next(stamps))
    with pytest.raises(AssertionError, match="EVALS_CORE_DB_URL"):
        s5._reseed({"agent_url": URL, "core_db": "x"})
    stamps = iter([None, "2026-11-27 15:01"])
    s5._reseed({"agent_url": URL, "core_db": "x"})


def test_ticket_ending_needs_exactly_four_digits() -> None:
    """What: "ticket ending 12345" is not a 4-digit short id. Use when: CI. Inputs: none. Output: none.
    Side effects: none. Role: review M-3."""
    assert s5.TICKET_ENDING.search("ticket ending 12345") is None
    m = s5.TICKET_ENDING.search("I've opened a ticket, ending 0993.")
    assert m is not None and m.group(1) == "0993"


def test_fallback_badges_reject_a_step_the_primary_answered() -> None:
    """What: every model step must be answered by the badge's fallback model. Use when: CI. Inputs: none.
    Output: none. Side effects: none. Role: review M-3."""
    with pytest.raises(AssertionError):
        s5._fallback_badges([_badge(), _model_step("claude-sonnet-5-5")])
