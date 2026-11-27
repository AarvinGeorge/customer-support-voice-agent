"""What: offline tests for the shared reseed guard (evals.reseed), used by the golden CLI and the S5 e2e tests.
Use when: CI. Inputs: none. Output: pytest results. Side effects: tmp dirs only; docker and Postgres are faked.
Role: the reseed TRUNCATEs core, so it must only ever reach the stack under test (review I-1 of PR #60, re-review N-1, N-2).
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from evals import reseed

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
    with pytest.raises(reseed.ReseedRefused, match=why):
        reseed.reseed_command(env, URL, tmp_path, run)
    assert run.seen == []


def test_reseed_guard_refuses_on_the_vm_marker(tmp_path: Path) -> None:
    """What: a checkout with deploy.sh's state dir (.deploy) is the VM: refused. Use when: CI. Inputs: tmp root.
    Output: none. Side effects: creates tmp_path/.deploy. Role: never wipe the public demo."""
    (tmp_path / ".deploy").mkdir()
    with pytest.raises(reseed.ReseedRefused, match=".deploy"):
        reseed.reseed_command(GOOD_ENV, URL, tmp_path, _port_run("127.0.0.1:3095"))


@pytest.mark.parametrize("url", ["https://demo.example.com", "http://10.0.0.5:3095", "http://localhost"])
def test_reseed_guard_refuses_a_non_loopback_url(tmp_path: Path, url: str) -> None:
    """What: the base URL must be loopback with an explicit port (the local override's EDGE_PORT). Use when: CI.
    Inputs: tmp root, url. Output: none. Side effects: none. Role: the VM serves 80 and 443 on a domain."""
    with pytest.raises(reseed.ReseedRefused, match="EVALS_AGENT_URL"):
        reseed.reseed_command(GOOD_ENV, url, tmp_path, _port_run("127.0.0.1:3095"))


@pytest.mark.parametrize("published, code", [("127.0.0.1:3000", 0), ("0.0.0.0:80", 0), ("", 1)])
def test_reseed_guard_refuses_when_the_project_does_not_serve_the_url(tmp_path: Path, published: str, code: int) -> None:
    """What: the named project's Caddy must publish the URL's port, else the reseed would hit another stack.
    Use when: CI. Inputs: tmp root, published address, exit code. Output: none. Side effects: none. Role: review I-1."""
    with pytest.raises(reseed.ReseedRefused, match="cs-a5"):
        reseed.reseed_command(GOOD_ENV, URL, tmp_path, _port_run(published, code))


def test_reseed_guard_accepts_the_stack_under_test(tmp_path: Path) -> None:
    """What: a matching stack gives the reseed command pinned with -p to that project. Use when: CI. Inputs: tmp root.
    Output: none. Side effects: none. Role: the truncate can only reach the stack behind EVALS_AGENT_URL."""
    run = _port_run("127.0.0.1:3095")
    cmd = reseed.reseed_command(GOOD_ENV, URL, tmp_path, run)
    assert run.seen == [["docker", "compose", "-p", "cs-a5", "port", "caddy", "80"]]
    assert cmd[:4] == ["docker", "compose", "-p", "cs-a5"] and cmd[-1] == "--reseed"


def test_a_project_name_with_a_trailing_newline_is_refused(tmp_path: Path) -> None:
    """What: "cs-a5\\n" is not a lane name. Use when: CI. Inputs: tmp root. Output: none. Side effects: none.
    Role: re-review N-1 (re.match with $ accepted it)."""
    run = _port_run("127.0.0.1:3095")
    with pytest.raises(reseed.ReseedRefused, match="lane stack name"):
        reseed.reseed_command({**GOOD_ENV, "COMPOSE_PROJECT_NAME": "cs-a5\n"}, URL, tmp_path, run)
    assert run.seen == []


def test_a_bad_url_port_is_a_refusal_not_a_value_error(tmp_path: Path) -> None:
    """What: http://localhost:abc raises ReseedRefused. Use when: CI. Inputs: tmp root. Output: none. Side effects: none.
    Role: re-review N-2 (urlsplit().port raised ValueError)."""
    with pytest.raises(reseed.ReseedRefused, match="EVALS_AGENT_URL"):
        reseed.reseed_command(GOOD_ENV, "http://localhost:abc", tmp_path, _port_run("127.0.0.1:3095"))


def test_a_dangling_deploy_symlink_is_the_vm_marker_too(tmp_path: Path) -> None:
    """What: a .deploy symlink whose target is gone still marks a deploy target. Use when: CI. Inputs: tmp root.
    Output: none. Side effects: creates a dangling symlink in tmp. Role: re-review N-2 (exists() missed it)."""
    (tmp_path / ".deploy").symlink_to(tmp_path / "gone")
    with pytest.raises(reseed.ReseedRefused, match=".deploy"):
        reseed.reseed_command(GOOD_ENV, URL, tmp_path, _port_run("127.0.0.1:3095"))


def test_guarded_reseed_runs_the_pinned_command_and_checks_the_audit_row(tmp_path: Path) -> None:
    """What: guard, read ts, reseed with -p, read ts again; a ts that did not advance is a ReseedError.
    Use when: CI. Inputs: tmp root. Output: none. Side effects: none. Role: one guarded reseed for every caller."""
    run = _port_run("127.0.0.1:3095")
    stamps = iter([None, "2026-11-27 15:01"])
    reseed.guarded_reseed(GOOD_ENV, URL, "core-db", tmp_path, run=run, last_ts=lambda db: next(stamps))
    assert run.seen[1][:4] == ["docker", "compose", "-p", "cs-a5"] and run.seen[1][-1] == "--reseed"
    stamps = iter(["2026-11-27 15:01", "2026-11-27 15:01"])
    with pytest.raises(reseed.ReseedError, match="EVALS_CORE_DB_URL"):
        reseed.guarded_reseed(GOOD_ENV, URL, "core-db", tmp_path, run=run, last_ts=lambda db: next(stamps))


def test_guarded_reseed_refuses_before_reading_or_truncating(tmp_path: Path) -> None:
    """What: a refused target makes no SELECT and no reseed. Use when: CI. Inputs: tmp root. Output: none.
    Side effects: none. Role: refusal is the first step."""
    run = _port_run("127.0.0.1:3000")
    reads: list[str] = []
    with pytest.raises(reseed.ReseedRefused):
        reseed.guarded_reseed(GOOD_ENV, URL, "core-db", tmp_path, run=run, last_ts=lambda db: reads.append(db))
    assert reads == [] and len(run.seen) == 1


def test_a_failed_reseed_command_is_a_reseed_error(tmp_path: Path) -> None:
    """What: the reseed exiting non-zero raises ReseedError with its stderr. Use when: CI. Inputs: tmp root.
    Output: none. Side effects: none. Role: no silent pass."""
    calls: list[list[str]] = []

    def run(cmd: list[str], **_: Any) -> SimpleNamespace:
        """What: port ok, reseed fails. Use when: this test. Inputs: cmd. Output: result. Side effects: records.
        Role: fake."""
        calls.append(cmd)
        if cmd[-1] == "80":
            return SimpleNamespace(returncode=0, stdout="127.0.0.1:3095\n", stderr="")
        return SimpleNamespace(returncode=1, stdout="", stderr="DEMO_RESEED refused")
    with pytest.raises(reseed.ReseedError, match="DEMO_RESEED refused"):
        reseed.guarded_reseed(GOOD_ENV, URL, "core-db", tmp_path, run=run, last_ts=lambda db: None)


# --- PR #73 review I4: the owner's cs-demo stack and port 3000, and smoke stacks without this run's marker ---------

@pytest.mark.parametrize("project", ["cs-demo", "cs-demo-2", "cs-a-demo"])
def test_reseed_guard_refuses_any_demo_project_on_every_path(tmp_path: Path, project: str) -> None:
    """What: a project naming demo is refused before any docker call, whatever the port. Use when: CI.
    Inputs: tmp root, project. Output: none. Side effects: none. Role: cs-demo is the owner's live stack."""
    run = _port_run("127.0.0.1:3095")
    with pytest.raises(reseed.ReseedRefused, match="demo"):
        reseed.reseed_command({**GOOD_ENV, "COMPOSE_PROJECT_NAME": project}, URL, tmp_path, run)
    assert run.seen == []


def test_reseed_guard_refuses_port_3000_even_when_the_project_publishes_it(tmp_path: Path) -> None:
    """What: EVALS_AGENT_URL on port 3000 is refused before any docker call, even if the port read would match.
    Use when: CI. Inputs: tmp root. Output: none. Side effects: none. Role: 3000 is cs-demo's edge port."""
    run = _port_run("127.0.0.1:3000")
    with pytest.raises(reseed.ReseedRefused, match="3000"):
        reseed.reseed_command(GOOD_ENV, "http://localhost:3000", tmp_path, run)
    assert run.seen == []


def test_a_smoke_project_needs_the_marker_of_this_run(tmp_path: Path) -> None:
    """What: a cs-smoke-* project is refused unless live_smoke.sh's run marker for that project exists and matches
    SMOKE_RUN_NONCE; with both it passes. Use when: CI. Inputs: tmp root. Output: none. Side effects: tmp files.
    Role: the smoke may only reseed the stack its own script created in this run."""
    env = {**GOOD_ENV, "COMPOSE_PROJECT_NAME": "cs-smoke-183617-ab12"}
    run = _port_run("127.0.0.1:3095")
    with pytest.raises(reseed.ReseedRefused, match="marker"):
        reseed.reseed_command(env, URL, tmp_path, run)
    marker = reseed.smoke_marker(tmp_path, env["COMPOSE_PROJECT_NAME"])
    marker.parent.mkdir(parents=True)
    marker.write_text("nonce-1\n")
    with pytest.raises(reseed.ReseedRefused, match="marker"):
        reseed.reseed_command({**env, "SMOKE_RUN_NONCE": "nonce-other"}, URL, tmp_path, run)
    assert run.seen == []
    assert reseed.reseed_command({**env, "SMOKE_RUN_NONCE": "nonce-1"}, URL, tmp_path, run)[:4] == \
        ["docker", "compose", "-p", "cs-smoke-183617-ab12"]
