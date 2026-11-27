"""What: the PR check for the live smoke gate: a pull request that changes agent behaviour (gates, middleware, prompts,
tool descriptions, model adapters, data/models.yaml or the agent wiring) must carry a passing live smoke summary
(scripts/live_smoke.sh output) in its body, run on a commit of this PR.
Use when: the smoke-gate workflow on every pull_request event (opened, edited, synchronize, reopened); locally with
`python3 scripts/check_smoke_summary.py --base <sha> --head <sha>` and PR_BODY in the env.
Inputs: --base and --head commits, env PR_BODY (the PR description, passed by env so it is never shell-interpolated).
Output: one line saying why it passed or failed; exit 0 pass (or no gated path changed), 1 fail, 2 usage.
Side effects: read-only git commands (diff, merge-base, cat-file).
Role: makes the PR template's smoke item enforceable. It proves a passing summary from this PR's history is present;
it cannot prove the summary was not edited by hand (the reviewer compares it with the PR's commits)."""
from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from collections.abc import Callable, Sequence

# One list, used by the check and named by the PR template. Prefix entries end with "/".
GATED = (
    "services/agent/src/agent/gates.py",
    "services/agent/src/agent/middleware/",
    "services/agent/src/agent/prompt.py",
    "services/agent/src/agent/prompts/",
    "services/agent/src/agent/untrusted.py",
    "services/agent/src/agent/tools/catalog.py",
    "services/agent/src/agent/models/",
    "services/agent/src/agent/assembly.py",
    "services/agent/src/agent/composition.py",
    "data/models.yaml",
)
HEADER = re.compile(r"^LIVE SMOKE \S+ commit ([0-9a-f]{7,40})(.*)$", re.M)
RESULT = re.compile(r"^RESULT: (PASS|FAIL)\s*$", re.M)
Git = Callable[[Sequence[str]], subprocess.CompletedProcess[str]]


def _git(args: Sequence[str]) -> subprocess.CompletedProcess[str]:
    """What: run one read-only git command. Use when: main. Inputs: args after `git`. Output: CompletedProcess.
    Side effects: git reads. Role: the default runner (tests pass a fake)."""
    return subprocess.run(["git", *args], capture_output=True, text=True, timeout=60)


def gated_paths(files: Sequence[str]) -> list[str]:
    """What: the changed files that change agent behaviour. Use when: deciding whether the smoke is required.
    Inputs: changed paths. Output: the gated ones, sorted. Side effects: none. Role: GATED applied."""
    return sorted(f for f in files if any(f == g or (g.endswith("/") and f.startswith(g)) for g in GATED))


def parse_summary(body: str) -> tuple[str, bool, str] | None:
    """What: the smoke summary in a PR body. Use when: a gated path changed. Inputs: body text.
    Output: (commit, dirty flag, result) of the LAST summary block, or None when there is none.
    Side effects: none. Role: the first and last lines evals.smoke.render_summary prints."""
    heads = list(HEADER.finditer(body))
    if not heads:
        return None
    last = heads[-1]
    result = RESULT.search(body, last.end())
    return last.group(1), "dirty" in last.group(2), result.group(1) if result else "missing"


def check(base: str, head: str, body: str, git: Git = _git) -> tuple[bool, str]:
    """What: the whole check. Use when: main. Inputs: base and head commits, PR body, git runner.
    Output: (ok, reason). Side effects: git reads. Role: fail closed on any git error."""
    diff = git(["diff", "--name-only", f"{base}...{head}"])
    if diff.returncode != 0:
        return False, f"git diff {base}...{head} failed: {diff.stderr.strip()[:200]}"
    gated = gated_paths(diff.stdout.split())
    if not gated:
        return True, "no agent-behaviour path changed; the live smoke is not required"
    found = parse_summary(body)
    names = ", ".join(gated[:5]) + (" ..." if len(gated) > 5 else "")
    if found is None:
        return False, f"changes {names}: paste the scripts/live_smoke.sh summary (LIVE SMOKE ... RESULT: PASS) into the PR body"
    commit, dirty, result = found
    if result != "PASS":
        return False, f"the live smoke summary says RESULT: {result}; rerun RUN_REAL=1 scripts/live_smoke.sh until it passes"
    if dirty:
        return False, "the live smoke ran on a dirty tree; commit, rerun and paste the new summary"
    full = git(["rev-parse", "--verify", "--quiet", f"{commit}^{{commit}}"])
    if full.returncode != 0:
        return False, f"smoke commit {commit} is not in this repository's history (push it, or rerun on a pushed commit)"
    sha = full.stdout.strip()
    in_head = git(["merge-base", "--is-ancestor", sha, head]).returncode == 0
    in_base = git(["merge-base", "--is-ancestor", sha, base]).returncode == 0
    if not in_head or in_base:
        return False, f"smoke commit {commit} is not a commit of this PR (it must be on the PR branch, not on the base)"
    return True, f"live smoke PASS on PR commit {commit} covers {names}"


def main(argv: Sequence[str] | None = None, git: Git = _git) -> int:
    """What: command line. Use when: the workflow or a person runs it. Inputs: --base, --head, env PR_BODY.
    Output: exit code. Side effects: prints one line. Role: entry point."""
    p = argparse.ArgumentParser(prog="check_smoke_summary")
    p.add_argument("--base", required=True)
    p.add_argument("--head", required=True)
    a = p.parse_args(argv)
    ok, why = check(a.base, a.head, os.environ.get("PR_BODY", ""), git)
    print(("ok: " if ok else "FAIL: ") + why)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
