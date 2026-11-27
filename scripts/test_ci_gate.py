"""What: tests for scripts/ci_gate.sh (D-068): a clean pass posts success naming the main sha, and the gate goes red or
refuses on a dirty merge, a failing check, the main checkout, a non-git directory and a bad PR number.
Use when: CI job `python (scripts)` (pytest over scripts/), or after editing ci_gate.sh.
Inputs: a throwaway bare "origin" with refs/pull/<n>/head, a clone plus a linked worktree, a stub ci_local.sh, and a
fake `gh` on PATH that answers `pr view` from the bare repo and records every `api` call.
Output: pass or fail. Side effects: only inside pytest tmp dirs; never calls GitHub.
Role: proves the gate can go red (a green gate must be shown able to fail)."""
import os
import subprocess
from pathlib import Path

GATE = Path(__file__).resolve().parent / "ci_gate.sh"
SLUG = "AarvinGeorge/customer-support-voice-agent"
STUB = """#!/usr/bin/env bash
# What: stub ci_local.sh. Use when: gate tests. Inputs: file ci_result. Output: summary line. Side effects: none. Role: test double.
rc="$(cat ci_result)"
echo "ci_local: 1 passed, $rc failed, 0 skipped (0s)"
exit "$rc"
"""
FAKE_GH = """#!/usr/bin/env bash
# What: fake gh. Use when: gate tests. Inputs: argv. Output: PR info or nothing. Side effects: appends to $GH_LOG. Role: test double.
if [ "$1" = pr ]; then echo "OPEN main $(git --git-dir="$FAKE_BARE" rev-parse "refs/pull/$3/head")"; exit 0; fi
if [ "$1" = api ]; then printf '%s\\n' "$*" >> "$GH_LOG"; exit 0; fi
exit 9
"""


def _git(cwd: Path, *args: str, env: dict) -> str:
    """What: run git and return stdout. Use when: building fixtures. Inputs: cwd, args, env. Output: stdout.
    Side effects: git writes in cwd. Role: test helper."""
    return subprocess.run(["git", *args], cwd=cwd, env=env, check=True, capture_output=True, text=True).stdout.strip()


def _world(tmp_path: Path) -> dict:
    """What: build origin, a clone (main checkout) and a linked lane worktree with PRs 1 (clean pass), 2 (conflict),
    3 (failing check). Use when: every gate test. Inputs: tmp_path. Output: dict of paths and env.
    Side effects: files under tmp_path. Role: fixture."""
    gcfg = tmp_path / "gitconfig"
    gcfg.write_text("[user]\n\tname = t\n\temail = t@example.com\n[init]\n\tdefaultBranch = main\n")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "gh").write_text(FAKE_GH)
    (bin_dir / "gh").chmod(0o755)
    bare = tmp_path / "remote" / f"{SLUG}.git"
    env = {**os.environ, "GIT_CONFIG_GLOBAL": str(gcfg), "GIT_CONFIG_NOSYSTEM": "1",
           "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}", "FAKE_BARE": str(bare), "GH_LOG": str(tmp_path / "gh.log")}
    env.pop("CI_GATE_NO_POST", None)
    bare.mkdir(parents=True)
    _git(bare, "init", "-q", "--bare", env=env)
    seed = tmp_path / "seed"
    _git(tmp_path, "clone", "-q", str(bare), str(seed), env=env)
    (seed / ".gitignore").write_text(".tmp/\n")
    (seed / "scripts").mkdir()
    (seed / "scripts" / "ci_local.sh").write_text(STUB)
    (seed / "scripts" / "ci_local.sh").chmod(0o755)
    (seed / "ci_result").write_text("0\n")
    (seed / "a.txt").write_text("base\n")
    _git(seed, "add", "-A", env=env)
    _git(seed, "commit", "-qm", "base", env=env)
    _git(seed, "push", "-q", "origin", "HEAD:refs/heads/main", env=env)

    def pr(n: int, path: str, text: str) -> None:
        """What: push a one-file change from base as refs/pull/n/head. Use when: fixture. Inputs: n, path, text.
        Output: none. Side effects: git push. Role: helper."""
        _git(seed, "switch", "-q", "-c", f"pr{n}", "main", env=env)
        (seed / path).write_text(text)
        _git(seed, "commit", "-qam", f"pr{n}", env=env)
        _git(seed, "push", "-q", "origin", f"HEAD:refs/pull/{n}/head", env=env)
        _git(seed, "switch", "-q", "main", env=env)

    pr(1, "a.txt", "base\nfrom pr1\n")
    pr(2, "a.txt", "conflicting pr2 line\n")
    pr(3, "ci_result", "1\n")
    (seed / "a.txt").write_text("main moved on\n")
    _git(seed, "commit", "-qam", "main moves", env=env)
    _git(seed, "push", "-q", "origin", "HEAD:refs/heads/main", env=env)
    # pr1 must still merge cleanly after main moved: rebuild it on top of the new main with a separate file.
    _git(seed, "switch", "-q", "-c", "pr1b", "main", env=env)
    (seed / "b.txt").write_text("pr1\n")
    _git(seed, "add", "b.txt", env=env)
    _git(seed, "commit", "-qm", "pr1b", env=env)
    _git(seed, "push", "-qf", "origin", "HEAD:refs/pull/1/head", env=env)
    main_clone = tmp_path / "development"
    _git(tmp_path, "clone", "-q", str(bare), str(main_clone), env=env)
    lane = tmp_path / "wt-lane"
    _git(main_clone, "worktree", "add", "-q", "--detach", str(lane), "origin/main", env=env)
    main_sha = _git(bare, "rev-parse", "refs/heads/main", env=env)
    return {"env": env, "lane": lane, "main": main_clone, "bare": bare, "main_sha": main_sha, "gh_log": tmp_path / "gh.log", "tmp": tmp_path}


def _gate(cwd: Path, env: dict, *args: str) -> subprocess.CompletedProcess:
    """What: run ci_gate.sh. Use when: each test. Inputs: cwd, env, args. Output: the completed process.
    Side effects: those of the gate inside the fixture. Role: helper."""
    return subprocess.run(["bash", str(GATE), *args], cwd=cwd, env=env, capture_output=True, text=True, timeout=120)


def _posts(w: dict) -> list[str]:
    """What: the recorded `gh api` calls. Use when: asserting what was posted. Inputs: world. Output: lines.
    Side effects: none. Role: helper."""
    return w["gh_log"].read_text().splitlines() if w["gh_log"].exists() else []


def _assert_clean(w: dict) -> None:
    """What: assert no throwaway worktree or lock is left. Use when: after every gate run. Inputs: world. Output: none.
    Side effects: none. Role: helper."""
    tmpdir = w["lane"] / ".tmp"
    assert not [p for p in tmpdir.glob("ci-gate-pr*")] if tmpdir.exists() else True
    assert "ci-gate-pr" not in _git(w["main"], "worktree", "list", env=w["env"])
    assert not (w["main"] / ".git" / "ci-gate.lock").exists()


def test_clean_pass_posts_success_with_main_sha(tmp_path: Path) -> None:
    """What: PR 1 merges cleanly and the stub passes. Use when: CI. Inputs: fixture. Output: assertions.
    Side effects: none outside tmp. Role: the green path."""
    world = _world(tmp_path)
    r = _gate(world["lane"], world["env"], "1")
    assert r.returncode == 0, r.stdout + r.stderr
    head = _git(world["bare"], "rev-parse", "refs/pull/1/head", env=world["env"])
    [call] = _posts(world)
    assert f"repos/{SLUG}/statuses/{head}" in call
    assert "state=success" in call and "context=local-ci" in call and world["main_sha"] in call
    _assert_clean(world)


def test_dirty_merge_posts_failure(tmp_path: Path) -> None:
    """What: PR 2 conflicts with main. Use when: CI. Inputs: fixture. Output: exit 1, failure posted.
    Side effects: none outside tmp. Role: refusal path (dirty merge)."""
    world = _world(tmp_path)
    r = _gate(world["lane"], world["env"], "2")
    assert r.returncode == 1, r.stdout + r.stderr
    [call] = _posts(world)
    assert "state=failure" in call and "merge conflict" in call and world["main_sha"] in call
    _assert_clean(world)


def test_failing_check_posts_failure(tmp_path: Path) -> None:
    """What: PR 3 makes ci_local.sh exit 1. Use when: CI. Inputs: fixture. Output: exit 1, failure posted.
    Side effects: none outside tmp. Role: proves the gate can go red on a check."""
    world = _world(tmp_path)
    r = _gate(world["lane"], world["env"], "3")
    assert r.returncode == 1, r.stdout + r.stderr
    [call] = _posts(world)
    assert "state=failure" in call and "1 fail" in call and world["main_sha"] in call
    _assert_clean(world)


def test_main_checkout_is_refused(tmp_path: Path) -> None:
    """What: running from the main checkout (not a linked worktree) refuses and posts nothing. Use when: CI.
    Inputs: fixture. Output: exit 2. Side effects: none. Role: wrong cwd (never development/)."""
    world = _world(tmp_path)
    r = _gate(world["main"], world["env"], "1")
    assert r.returncode == 2 and "main checkout" in r.stderr
    assert _posts(world) == []


def test_outside_git_is_refused(tmp_path: Path) -> None:
    """What: running outside any git checkout refuses. Use when: CI. Inputs: fixture. Output: exit 2.
    Side effects: none. Role: wrong cwd."""
    world = _world(tmp_path)
    outside = world["tmp"] / "nowhere"
    outside.mkdir()
    r = _gate(outside, world["env"], "1")
    assert r.returncode == 2 and "not inside a git checkout" in r.stderr
    assert _posts(world) == []


def test_bad_pr_number_is_refused(tmp_path: Path) -> None:
    """What: a non-numeric PR argument refuses. Use when: CI. Inputs: fixture. Output: exit 2.
    Side effects: none. Role: usage guard."""
    world = _world(tmp_path)
    r = _gate(world["lane"], world["env"], "1;rm")
    assert r.returncode == 2 and _posts(world) == []


def test_held_lock_is_refused(tmp_path: Path) -> None:
    """What: a second concurrent run (lock held) refuses and leaves the lock alone. Use when: CI. Inputs: fixture.
    Output: exit 2. Side effects: none. Role: one gate run at a time (shared pg test ports)."""
    world = _world(tmp_path)
    lock = world["main"] / ".git" / "ci-gate.lock"
    lock.mkdir()
    r = _gate(world["lane"], world["env"], "1")
    assert r.returncode == 2 and "another ci_gate run" in r.stderr
    assert lock.exists() and _posts(world) == []
