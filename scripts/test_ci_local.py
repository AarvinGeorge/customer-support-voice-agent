"""What: tests that .github/workflows/ci.yml and scripts/ci_local.sh stay ONE source of truth (D-068), and that
ci_local.sh goes red on a failing step and refuses an unknown job.
Use when: CI job `python (scripts)` (pytest over scripts/), or after editing ci.yml or ci_local.sh.
Inputs: the repo's ci.yml and ci_local.sh. Output: pass or fail.
Side effects: runs ci_local.sh (descriptors job only, logs under pytest tmp). Role: drift guard for the local CI gate."""
import re
import subprocess
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
CI = ROOT / ".github" / "workflows" / "ci.yml"
LOCAL = ROOT / "scripts" / "ci_local.sh"


def _jobs_listed() -> list[str]:
    """What: ci_local.sh --list. Use when: comparing with ci.yml. Inputs: none. Output: job names.
    Side effects: runs the script. Role: helper."""
    return subprocess.run(["bash", str(LOCAL), "--list"], check=True, capture_output=True, text=True).stdout.split()


def _workflow() -> dict:
    """What: the parsed ci.yml. Use when: each structural test. Inputs: none. Output: dict. Side effects: none. Role: helper."""
    return yaml.safe_load(CI.read_text())


def test_every_ci_job_calls_ci_local_and_covers_every_job() -> None:
    """What: the jobs ci.yml runs equal the jobs ci_local.sh defines, and no run step holds its own check command.
    Use when: CI. Inputs: none. Output: assertions. Side effects: none. Role: one source of truth."""
    wf = _workflow()
    called: set[str] = set()
    for job_id, job in wf["jobs"].items():
        runs = [s["run"] for s in job["steps"] if "run" in s]
        assert runs, f"{job_id} has no run step"
        for run in runs:
            assert run.startswith("set -euo pipefail"), f"{job_id}: run step must start with set -euo pipefail"
            body = [ln.strip() for ln in run.splitlines()[1:] if ln.strip()]
            assert len(body) == 1 and body[0].startswith("scripts/ci_local.sh "), f"{job_id}: run step must only call ci_local.sh: {body}"
            arg = body[0].split(" ", 1)[1].strip('"')
            if "${{ matrix.project }}" in arg:
                called.update(f"python:{p}" for p in job["strategy"]["matrix"]["project"])
            else:
                called.add(arg)
    assert called == set(_jobs_listed())


def test_tool_versions_match_the_workflow_setup() -> None:
    """What: the uv and node versions ci.yml installs equal the ones ci_local.sh enforces. Use when: CI.
    Inputs: none. Output: assertions. Side effects: none. Role: version parity."""
    text = LOCAL.read_text()
    uv = re.search(r'^UV_VERSION="([^"]+)"', text, re.M).group(1)  # type: ignore[union-attr]
    node = re.search(r'^NODE_MAJOR="([^"]+)"', text, re.M).group(1)  # type: ignore[union-attr]
    for job in _workflow()["jobs"].values():
        for step in job["steps"]:
            if str(step.get("uses", "")).startswith("astral-sh/setup-uv"):
                assert step["with"]["version"] == uv
            if str(step.get("uses", "")).startswith("actions/setup-node"):
                assert step["with"]["node-version"] == node


def test_unknown_job_is_refused(tmp_path: Path) -> None:
    """What: an unknown job name exits 2 before running anything. Use when: CI. Inputs: tmp log dir.
    Output: assertions. Side effects: none. Role: no silent no-op run."""
    r = subprocess.run(["bash", str(LOCAL), "descriptors", "no-such-job"], capture_output=True, text=True,
                       env={"PATH": "/usr/bin:/bin:/usr/local/bin", "HOME": str(tmp_path), "CI_LOG_DIR": str(tmp_path)})
    assert r.returncode == 2 and "unknown job: no-such-job" in r.stderr
    assert "--- descriptors" not in r.stdout


def test_a_failing_step_turns_the_run_red(tmp_path: Path) -> None:
    """What: the descriptors step against a base ref that does not exist fails, and the run exits 1 with a FAIL row.
    Use when: CI. Inputs: tmp log dir. Output: assertions. Side effects: logs in tmp_path. Role: proves ci_local can go red."""
    import os

    env = {**os.environ, "CI_LOG_DIR": str(tmp_path), "CI_DESCRIPTORS_BASE": "refs/heads/no-such-base-ref"}
    env.pop("GITHUB_ACTIONS", None)
    r = subprocess.run(["bash", str(LOCAL), "descriptors"], capture_output=True, text=True, env=env)
    assert r.returncode == 1, r.stdout + r.stderr
    assert re.search(r"descriptors .* FAIL", r.stdout)
    assert re.match(r"ci_local: \d+ passed, 1 failed", r.stdout.strip().splitlines()[-1])


def test_no_job_or_step_can_be_softened_in_the_workflow() -> None:
    """What: no `continue-on-error` and no `if:` on any job or step (review M1), so a check cannot go quietly green.
    Use when: CI. Inputs: none. Output: assertions. Side effects: none. Role: drift guard."""
    for job_id, job in _workflow()["jobs"].items():
        assert "continue-on-error" not in job and "if" not in job, job_id
        for step in job["steps"]:
            assert "continue-on-error" not in step and "if" not in step, f"{job_id}: {step}"


def test_pg_project_outside_the_localci_namespace_is_refused(tmp_path: Path) -> None:
    """What: CI_PG_PROJECT must start with cs-localci (review M4). Runs with a recording fake docker and uv FIRST on PATH
    and fake project names only, so even a removed guard cannot reach a real stack (PR #74 N1 incident).
    Use when: CI. Inputs: tmp log dir. Output: assertions. Side effects: fakes only. Role: guard."""
    from test_compose_owner import docker_calls, fake_env

    for proj in ("qs-probe-notlocalci", "cs-guardprobe-x"):
        env = fake_env(tmp_path, CI_LOG_DIR=str(tmp_path / "logs"), CI_PG_PROJECT=proj)
        env.pop("GITHUB_ACTIONS", None)
        r = subprocess.run(["bash", str(LOCAL), "pg-tests"], capture_output=True, text=True, env=env, timeout=120)
        assert r.returncode == 2 and "cs-localci" in r.stderr, r.stdout + r.stderr
        assert "--- pg tests" not in r.stdout
    assert [c for c in docker_calls(tmp_path) if not c.startswith("uv ")] == []  # refused before any docker call
