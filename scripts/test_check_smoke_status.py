"""What: tests for the live smoke gate: scripts/check_smoke_status.py (commit statuses, not PR text), the one gated
path list (scripts/smoke_gated_paths.txt) against where each gate really lives, the shared helper
and scripts/live_smoke.sh (its refusals and its commit status lifecycle on the shared scripts/lib/commit_status.sh).
Use when: CI runs pytest in scripts/. Inputs: throwaway git repositories, a fake `gh`, the real source tree.
Output: pass or fail. Side effects: writes only inside pytest tmp dirs; runs git and bash there.
Role: proves each failure reason can fire (a zero must be shown possible to be non-zero) and a correct PR passes."""
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import check_smoke_status as css  # noqa: E402

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
REPO = "o/r"


class World:
    """What: a throwaway git repo plus fake GitHub statuses. Use when: check tests. Inputs: tmp dir.
    Output: commit helpers and a runner for css. Side effects: git in tmp. Role: fixture."""

    def __init__(self, tmp: Path) -> None:
        """What: init the repo with a base commit. Use when: each test. Inputs: tmp dir. Output: none.
        Side effects: git init and one commit. Role: init."""
        self.tmp, self.statuses, self.api_down, self.posts = tmp, {}, False, []
        self.git("init", "-q", "-b", "main")
        self.git("config", "user.email", "t@example.test")
        self.git("config", "user.name", "t")
        self.base = self.commit({"README.md": "x\n"})

    def git(self, *a: str) -> str:
        """What: run git in the repo. Use when: building history. Inputs: args. Output: stdout. Side effects: git.
        Role: helper."""
        return subprocess.run(["git", "-C", str(self.tmp), *a], check=True, capture_output=True, text=True).stdout.strip()

    def commit(self, files: dict[str, str], mv: tuple[str, str] | None = None) -> str:
        """What: write files (and optionally git mv one) then commit. Use when: tests. Inputs: files, rename.
        Output: sha. Side effects: git. Role: helper."""
        if mv:
            (self.tmp / mv[1]).parent.mkdir(parents=True, exist_ok=True)
            self.git("mv", *mv)
        for name, body in files.items():
            (self.tmp / name).parent.mkdir(parents=True, exist_ok=True)
            (self.tmp / name).write_text(body)
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "c")
        return self.git("rev-parse", "HEAD")

    def run(self, cmd: Any) -> subprocess.CompletedProcess[str]:
        """What: the css runner: git in the repo, gh answered from self.statuses. Use when: css calls. Inputs: argv.
        Output: CompletedProcess. Side effects: git reads. Role: double."""
        cmd = list(cmd)
        if cmd[0] == "git":
            return subprocess.run(["git", "-C", str(self.tmp), *cmd[1:]], capture_output=True, text=True)
        if self.api_down:
            return subprocess.CompletedProcess(cmd, 1, "", "HTTP 502")
        path = cmd[2]
        if "-f" in cmd:
            self.posts.append((path, [c for c in cmd if c.startswith(("state=", "context="))]))
            return subprocess.CompletedProcess(cmd, 0, "{}", "")
        sha = path.split("/")[-2]
        st = self.statuses.get(sha)
        body = {"statuses": [{"context": "live-smoke", **st}, {"context": "other", "state": "success"}] if st else []}
        return subprocess.CompletedProcess(cmd, 0, json.dumps(body), "")

    def check(self, head: str) -> tuple[bool, str]:
        """What: css.check from base to head. Use when: tests. Inputs: head. Output: result. Side effects: reads.
        Role: helper."""
        return css.check(REPO, self.base, head, self.run)


PASS = {"state": "success", "description": "PASS 5/5 cases, 19 calls, cap 25, 4 searches"}
GATES = "services/agent/src/agent/gates.py"


def test_the_config_gates_source_trees_and_skips_tests() -> None:
    files = [GATES, "services/agent/src/agent/sse.py", "services/knowledge/src/knowledge/gate.py", "data/models.yaml",
             "data/policy.yaml", "evals/smoke.py", "evals/golden/golden.yaml", "scripts/check_smoke_status.py",
             ".github/workflows/smoke-gate.yml", "evals/tests/test_smoke.py", "services/agent/tests/test_gate1.py",
             "apps/web/src/x.ts", "README.md", "infra/tests/test_x.py"]
    assert css.gated_paths(files) == sorted(files[:9])


# Where each gate, prompt, model setting and wiring point is defined: found by searching the source (so a move or a
# rename is found again), then every hit must be gated. At least one hit each (a zero must be able to be non-zero).
DEFINITIONS = {
    "Gate 1 in the agent": r"def apply_retrieval_gate\b",
    "Gate 1 thresholds in knowledge": r"\btau_high\b",
    "Gate 2 middleware": r"class GroundingMiddleware\b",
    "Gate 2 verifier and its prompt": r"class GroundingVerifier\b|VERIFIER_PROMPT\s*=",
    "scope classifier and prompt": r"class ScopeClassifier\b|SCOPE_PROMPT\s*=",
    "end reason (clarify) rule": r"def classify_final\b",
    "system prompt": r"SYSTEM_PROMPT\s*=",
    "middleware stack": r"create_agent\(",
    "model registry": r"models\.yaml",
    "refund rules": r"PRO_WITHIN_LIMITS",
    "tool descriptions": r"def search_kb\b|\"search_kb\"",
}


def _source_files() -> list[str]:
    """What: tracked code and config files outside tests; Markdown only where it is a prompt (docs that mention a
    file do not change behaviour). Use when: the coverage test. Inputs: none. Output: paths.
    Side effects: git ls-files. Role: helper."""
    out = subprocess.run(["git", "-C", str(ROOT), "ls-files"], check=True, capture_output=True, text=True).stdout.split()
    return [f for f in out if (f.endswith((".py", ".yaml", ".yml", ".json", ".sql")) or
                               (f.endswith(".md") and "/prompts/" in f))
            and "/tests/" not in f and not f.startswith(("tests/", "spikes/", "scripts/test_", "build-log/"))
            and f not in ("README.md", "PRODUCT.md", "DESIGN.md")]


def test_every_place_a_gate_or_prompt_lives_is_gated() -> None:
    files = _source_files()
    texts = {f: (ROOT / f).read_text(errors="ignore") for f in files}
    for name, pattern in DEFINITIONS.items():
        hits = [f for f, t in texts.items() if re.search(pattern, t)]
        assert hits, f"{name}: pattern found nothing, the test cannot see it"
        assert not set(hits) - set(css.gated_paths(hits)), f"{name} lives in ungated {set(hits) - set(css.gated_paths(hits))}"
    for p in ("data/models.yaml", "data/policy.yaml", "services/agent/src/agent/prompts/realtime.md"):
        assert (ROOT / p).exists() and css.gated_paths([p]) == [p]


def test_no_gated_change_needs_no_smoke(tmp_path: Path) -> None:
    w = World(tmp_path)
    head = w.commit({"apps/web/x.ts": "1"})
    assert w.check(head) == (True, "no agent-behaviour path changed; the live smoke is not required")


def test_a_gated_change_without_any_status_fails_even_with_a_pasted_body(tmp_path: Path) -> None:
    w = World(tmp_path)
    head = w.commit({GATES: "x = 1\n"})
    ok, why = w.check(head)
    assert not ok and "no live-smoke status" in why


def test_success_on_the_head_passes_and_failure_on_the_head_fails(tmp_path: Path) -> None:
    w = World(tmp_path)
    head = w.commit({GATES: "x = 1\n"})
    w.statuses[head] = PASS
    assert w.check(head)[0]
    w.statuses[head] = {"state": "failure", "description": "FAIL 3/5 cases, 19 calls, cap 25"}
    assert "is failure" in w.check(head)[1]
    w.statuses[head] = {"state": "pending", "description": "running"}
    assert "is pending" in w.check(head)[1]


def test_a_stale_smoke_fails_when_a_gated_path_changed_after_it(tmp_path: Path) -> None:
    """What: review I2: smoke on A, then another gated change in B: fail. A later non-gated commit is fine."""
    w = World(tmp_path)
    a = w.commit({GATES: "x = 1\n"})
    w.statuses[a] = PASS
    docs = w.commit({"apps/web/readme.md": "doc"})
    assert w.check(docs)[0]
    b = w.commit({GATES: "x = 2\n"})
    ok, why = w.check(b)
    assert not ok and "changed after it" in why


def test_a_smoke_on_a_commit_before_the_gated_change_fails(tmp_path: Path) -> None:
    w = World(tmp_path)
    early = w.commit({"apps/web/x.ts": "1"})
    w.statuses[early] = PASS
    head = w.commit({GATES: "x = 1\n"})
    assert not w.check(head)[0]


def test_a_rename_out_of_a_gated_tree_is_still_gated(tmp_path: Path) -> None:
    w = World(tmp_path)
    w.base = w.commit({GATES: "x = 1\n"})
    head = w.commit({"lib/gate_logic.py": "x = 2\n"}, mv=(GATES, "lib/gate_logic.py"))
    ok, why = w.check(head)
    assert not ok and "gates.py" in why


def test_a_cap_above_25_or_an_unreadable_api_fails_closed(tmp_path: Path) -> None:
    w = World(tmp_path)
    head = w.commit({GATES: "x = 1\n"})
    w.statuses[head] = {"state": "success", "description": "PASS 5/5 cases, 40 calls, cap 60"}
    assert "cap of at most 25" in w.check(head)[1]
    w.statuses[head] = PASS
    w.api_down = True
    ok, why = w.check(head)
    assert not ok and "cannot read statuses" in why
    assert not css.check(REPO, "nope", head, w.run)[0]


def test_event_mode_posts_smoke_gate_on_the_pr_head(tmp_path: Path) -> None:
    w = World(tmp_path)
    head = w.commit({GATES: "x = 1\n"})
    w.statuses[head] = PASS
    w.git("update-ref", "refs/pull/7/head", head)
    w.git("remote", "add", "origin", str(tmp_path))
    ev = tmp_path / "event.json"
    ev.write_text(json.dumps({"pull_request": {"number": 7, "base": {"sha": w.base}, "head": {"sha": head}}}))
    assert css.main(["--repo", REPO, "--event", str(ev), "--event-name", "pull_request_target"], w.run) == 0
    assert w.posts == [(f"repos/{REPO}/statuses/{head}", ["state=success", "context=smoke-gate"])]


def test_status_events_for_other_contexts_evaluate_nothing() -> None:
    assert css.targets("status", {"context": "local-ci", "sha": "a" * 40}, REPO) == []


# --- scripts/live_smoke.sh: refusals, and the status lifecycle on the shared helper ----------------------------------

def _fake_bin(tmp: Path) -> tuple[dict[str, str], Path]:
    """What: a PATH whose `gh` and `docker` only record their calls. Use when: refusal tests. Inputs: tmp dir.
    Output: env, log path. Side effects: writes two scripts. Role: double."""
    bin_ = tmp / "bin"
    bin_.mkdir()
    log = tmp / "calls.log"
    for name in ("gh", "docker"):
        (bin_ / name).write_text(f'#!/bin/sh\necho "{name} $*" >> {log}\n')
        (bin_ / name).chmod(0o755)
    env = {k: v for k, v in os.environ.items() if k not in ("RUN_REAL", "COMMIT_STATUS_NO_POST", "SMOKE_OUT")}
    env["PATH"] = f"{bin_}:{env.get('PATH', '')}"
    return env, log


def test_live_smoke_refuses_without_run_real_and_calls_no_docker_or_gh(tmp_path: Path) -> None:
    env, log = _fake_bin(tmp_path)
    for value in (None, "0", "true"):
        e = env if value is None else {**env, "RUN_REAL": value}
        r = subprocess.run(["bash", str(HERE / "live_smoke.sh")], env=e, capture_output=True, text=True, timeout=30)
        assert r.returncode == 2 and "set RUN_REAL=1" in r.stderr, (value, r.stderr)
    assert not log.exists()


STUB = """#!/bin/sh
echo "$(basename "$0") $*" >> "$CALLS"
case "$(basename "$0") $*" in
  "docker compose build"*) [ -n "$FAIL_UP" ] && exit 1 ;;
  *"run --rm ingest_knowledge"*) echo "INGEST ok run=1 articles=10 embedding_calls=1 corpus_version=x" ;;
  "docker ps"*) ;;
  "curl "*) echo '{"status":"ready","core":"ok","demo_faults":true}' ;;
  "uv run"*) out=""; prev=""; for a in "$@"; do [ "$prev" = "--status-out" ] && out="$a"; prev="$a"; done
             echo "$SMOKE_DESC" > "$out"; echo "LIVE SMOKE x"; exit "$SMOKE_CODE" ;;
esac
exit 0
"""


def _smoke_repo(tmp: Path) -> tuple[Path, dict[str, str], Path]:
    """What: a throwaway repo holding live_smoke.sh, the shared helper, a fake .env and migrate.sh, pushed to a fake
    origin, plus stub docker, uv, curl and gh. Use when: lifecycle tests. Inputs: tmp dir.
    Output: (repo, env, calls log). Side effects: files and git in tmp. Role: fixture (no docker, no model call)."""
    repo = tmp / "repo"
    (repo / "scripts" / "lib").mkdir(parents=True)
    for rel in ("scripts/live_smoke.sh", "scripts/lib/commit_status.sh"):
        (repo / rel).write_text((ROOT / rel).read_text())
    (repo / "scripts" / "migrate.sh").write_text("#!/bin/sh\nexit 0\n")
    for f in ("live_smoke.sh", "migrate.sh"):
        (repo / "scripts" / f).chmod(0o755)
    (repo / ".gitignore").write_text(".tmp/\n.env\n")
    (repo / ".env").write_text("CORE_DB_URL=postgresql://c:p@postgres:5432/q\nAGENT_DB_URL=postgresql://a:p@postgres:5432/q\n")
    def git(*a: str) -> None:
        """What: git in the repo. Use when: fixture. Inputs: args. Output: none. Side effects: git. Role: helper."""
        subprocess.run(["git", "-C", str(repo), *a], check=True, capture_output=True)
    git("init", "-q", "-b", "main")
    git("config", "user.email", "t@example.test")
    git("config", "user.name", "t")
    git("add", "-A")
    git("commit", "-q", "-m", "c")
    subprocess.run(["git", "init", "-q", "--bare", str(tmp / "origin.git")], check=True, capture_output=True)
    git("remote", "add", "origin", str(tmp / "origin.git"))
    git("push", "-q", "origin", "main")
    bin_ = tmp / "bin"
    bin_.mkdir()
    for name in ("docker", "uv", "curl", "gh"):
        (bin_ / name).write_text(STUB)
        (bin_ / name).chmod(0o755)
    calls = tmp / "calls.log"
    env = {k: v for k, v in os.environ.items() if k not in ("COMMIT_STATUS_NO_POST", "SMOKE_OUT", "KEEP_STACK")}
    env.update(PATH=f"{bin_}:{env.get('PATH', '')}", RUN_REAL="1", CALLS=str(calls), SMOKE_OUT=str(tmp / "out"),
               SMOKE_CODE="0", SMOKE_DESC="PASS 5/5 cases, 19 calls, cap 25, 4 searches")
    return repo, env, calls


def _statuses(calls: Path) -> list[str]:
    """What: the states posted, in order. Use when: lifecycle tests. Inputs: calls log. Output: states.
    Side effects: reads the log. Role: helper."""
    return [re.search(r"state=(\w+)", c).group(1) for c in calls.read_text().splitlines()  # type: ignore[union-attr]
            if c.startswith("gh api") and "context=live-smoke" in c]


def _run_smoke(repo: Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    """What: run the copied script. Use when: lifecycle tests. Inputs: repo, env. Output: result.
    Side effects: runs bash with stubs. Role: helper."""
    return subprocess.run(["bash", str(repo / "scripts" / "live_smoke.sh")], env=env, capture_output=True, text=True,
                          timeout=60)


def test_a_pass_posts_pending_then_success_and_tears_down(tmp_path: Path) -> None:
    repo, env, calls = _smoke_repo(tmp_path)
    r = _run_smoke(repo, env)
    assert r.returncode == 0, r.stderr
    assert _statuses(calls) == ["pending", "success"]
    log = calls.read_text()
    assert "description=PASS 5/5 cases, 19 calls, cap 25" in log
    assert "--profile seed --profile jobs down -v --remove-orphans --rmi local" in log
    assert "-p cs-demo" not in log and not list((repo / ".tmp" / "live-smoke").glob("*.run"))  # marker removed


def test_a_failed_smoke_or_stack_posts_failure_from_the_exit_trap(tmp_path: Path) -> None:
    repo, env, calls = _smoke_repo(tmp_path)
    r = _run_smoke(repo, {**env, "SMOKE_CODE": "1", "SMOKE_DESC": "FAIL 3/5 cases, 19 calls, cap 25"})
    assert r.returncode == 1 and _statuses(calls) == ["pending", "failure"]
    assert "description=FAIL 3/5 cases" in calls.read_text()
    calls.unlink()
    r = _run_smoke(repo, {**env, "FAIL_UP": "1"})
    assert r.returncode == 2 and _statuses(calls) == ["pending", "failure"]


def test_a_dirty_tree_runs_but_posts_nothing(tmp_path: Path) -> None:
    repo, env, calls = _smoke_repo(tmp_path)
    (repo / "scripts" / "migrate.sh").write_text("#!/bin/sh\nexit 0  # edited\n")
    r = _run_smoke(repo, env)
    assert r.returncode == 0 and _statuses(calls) == [] and "no live-smoke status" in r.stderr


def test_an_unpushed_commit_is_refused_before_any_docker_call(tmp_path: Path) -> None:
    repo, env, calls = _smoke_repo(tmp_path)
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "--allow-empty", "-m", "local only"], check=True)
    r = _run_smoke(repo, env)
    assert r.returncode == 2 and "not on origin" in r.stderr
    assert "docker compose" not in (calls.read_text() if calls.exists() else "")


def test_live_smoke_uses_the_shared_helper_and_never_names_the_owner_stack() -> None:
    text = (HERE / "live_smoke.sh").read_text()
    assert '. "$ROOT/scripts/lib/commit_status.sh"' in text and not (HERE / "commit_status.sh").exists()
    assert 'COMPOSE_PROJECT_NAME="cs-smoke-$STAMP"' in text and "free_port 3100 3199" in text
    assert "*demo*|*prod*) die" in text and "trap 'exit 130' INT" in text and "LANGSMITH_PROJECT=cs-smoke" in text
