"""What: tests for scripts/check_smoke_summary.py (the PR body must carry a passing live smoke summary when a PR changes
agent behaviour) and for the RUN_REAL refusal of scripts/live_smoke.sh.
Use when: CI runs pytest in scripts/. Inputs: a throwaway git repository per test (real git, no network).
Output: pass or fail. Side effects: writes only inside pytest tmp dirs; runs git and bash there.
Role: proves the check can fail for each reason and passes a correct body (a zero must be shown possible to be non-zero)."""
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import check_smoke_summary as css  # noqa: E402

HERE = Path(__file__).resolve().parent


def _repo(tmp: Path) -> tuple[str, str, str]:
    """What: a repo with a base commit and a PR commit touching a gated path. Use when: check tests. Inputs: tmp dir.
    Output: (base sha, head sha, short head sha). Side effects: git init and commits under tmp. Role: fixture."""
    def git(*a: str) -> str:
        """What: run git in the repo. Use when: building it. Inputs: args. Output: stdout. Side effects: git.
        Role: helper."""
        return subprocess.run(["git", "-C", str(tmp), *a], check=True, capture_output=True, text=True).stdout.strip()
    git("init", "-q", "-b", "main")
    git("config", "user.email", "t@example.test")
    git("config", "user.name", "t")
    (tmp / "README.md").write_text("x\n")
    git("add", ".")
    git("commit", "-q", "-m", "base")
    base = git("rev-parse", "HEAD")
    gates = tmp / "services/agent/src/agent/gates.py"
    gates.parent.mkdir(parents=True)
    gates.write_text("x = 1\n")
    git("add", ".")
    git("commit", "-q", "-m", "pr")
    head = git("rev-parse", "HEAD")
    return base, head, head[:12]


def _git_in(tmp: Path) -> css.Git:
    """What: a git runner bound to the repo. Use when: calling check. Inputs: tmp dir. Output: runner.
    Side effects: none. Role: helper."""
    return lambda args: subprocess.run(["git", "-C", str(tmp), *args], capture_output=True, text=True)


def _body(commit: str, result: str = "PASS", dirty: bool = False) -> str:
    """What: a PR body with a pasted summary. Use when: check tests. Inputs: commit, result, dirty flag.
    Output: text. Side effects: none. Role: helper."""
    return (f"## Live smoke\n```\nLIVE SMOKE 2026-11-27T19:00Z commit {commit}{' (dirty tree)' if dirty else ''}\n"
            f"stack cs-smoke-1 via Caddy 127.0.0.1:3141\n...\nRESULT: {result}\n```\n")


def test_gated_paths_match_files_and_directory_prefixes() -> None:
    files = ["services/agent/src/agent/middleware/grounding.py", "data/models.yaml", "apps/web/x.ts",
             "services/agent/src/agent/gates.py", "services/agent/src/agent/gates_test.py", "services/agent/tests/t.py"]
    assert css.gated_paths(files) == ["data/models.yaml", "services/agent/src/agent/gates.py",
                                      "services/agent/src/agent/middleware/grounding.py"]


def test_a_passing_summary_on_a_pr_commit_passes(tmp_path: Path) -> None:
    base, head, short = _repo(tmp_path)
    ok, why = css.check(base, head, _body(short), _git_in(tmp_path))
    assert ok, why


def test_each_failure_reason(tmp_path: Path) -> None:
    base, head, short = _repo(tmp_path)
    g = _git_in(tmp_path)
    assert css.check(base, head, "no summary here", g) == (False, css.check(base, head, "", g)[1])
    assert "paste" in css.check(base, head, "", g)[1]
    assert "RESULT: FAIL" in css.check(base, head, _body(short, "FAIL"), g)[1]
    assert "RESULT: missing" in css.check(base, head, _body(short).replace("RESULT: PASS", ""), g)[1]
    assert "dirty" in css.check(base, head, _body(short, dirty=True), g)[1]
    assert "not a commit of this PR" in css.check(base, head, _body(base[:12]), g)[1]
    assert "not in this repository" in css.check(base, head, _body("deadbeefdead"), g)[1]
    assert not css.check("nope", head, _body(short), g)[0]  # a git error fails closed


def test_the_last_summary_block_wins(tmp_path: Path) -> None:
    base, head, short = _repo(tmp_path)
    assert not css.check(base, head, _body(short) + _body(short, "FAIL"), _git_in(tmp_path))[0]
    assert css.check(base, head, _body(short, "FAIL") + _body(short), _git_in(tmp_path))[0]


def test_no_gated_change_needs_no_summary(tmp_path: Path) -> None:
    base, _head, _ = _repo(tmp_path)
    ok, why = css.check(base, base, "", _git_in(tmp_path))
    assert ok and "not required" in why


def test_live_smoke_refuses_without_run_real_and_calls_no_docker(tmp_path: Path) -> None:
    fake = tmp_path / "bin"
    fake.mkdir()
    log = tmp_path / "docker-called"
    (fake / "docker").write_text(f"#!/bin/sh\necho called >> {log}\n")
    (fake / "docker").chmod(0o755)
    env = {k: v for k, v in os.environ.items() if k != "RUN_REAL"}
    env["PATH"] = f"{fake}:{env.get('PATH', '')}"
    for value in (None, "0", "true"):
        e = env if value is None else {**env, "RUN_REAL": value}
        r = subprocess.run(["bash", str(HERE / "live_smoke.sh")], env=e, capture_output=True, text=True, timeout=30)
        assert r.returncode == 2 and "set RUN_REAL=1" in r.stderr, (value, r.stderr)
    assert not log.exists()
