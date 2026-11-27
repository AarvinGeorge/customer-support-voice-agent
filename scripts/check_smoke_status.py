"""What: the live smoke gate check. A pull request that changes a path in scripts/smoke_gated_paths.txt passes only
when a PR commit carries a `live-smoke` commit status of success (posted by scripts/live_smoke.sh on the exact commit
it tested, cap at most 25) and no gated path changed between that commit and the head. Mechanical: it reads commit
statuses, never the PR body.
Use when: (1) the smoke-gate workflow (pull_request_target and status events), which posts the result as the
`smoke-gate` status on the PR head; (2) locally or from the D-068 local gate: `python3 scripts/check_smoke_status.py
--base <sha> --head <sha>` (prints, posts nothing).
Inputs: --base and --head, or --event FILE (the GitHub event payload) with --repo; `gh` authenticated (GH_TOKEN in
Actions). Output: one line per PR; exit 0 pass (or nothing gated), 1 fail, 2 usage.
Side effects: read-only git (diff, rev-list, fetch of refs/pull/N/head in event mode); GitHub API reads; in event mode
one `smoke-gate` status POST per PR head.
Role: PR #73 review I1 to I3: a hand-typed body, a stale smoke and a rename can no longer pass."""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
CONFIG = HERE / "smoke_gated_paths.txt"
CONTEXT, GATE_CONTEXT = "live-smoke", "smoke-gate"
CAP_LIMIT = 25
MAX_COMMITS = 100
Run = Callable[[Sequence[str]], subprocess.CompletedProcess[str]]


def _run(cmd: Sequence[str]) -> subprocess.CompletedProcess[str]:
    """What: run one command. Use when: git and gh calls. Inputs: argv. Output: CompletedProcess. Side effects: the
    command. Role: the default runner (tests pass fakes)."""
    return subprocess.run(list(cmd), capture_output=True, text=True, timeout=120)


def load_config(path: Path = CONFIG) -> tuple[list[str], list[str]]:
    """What: (includes, excludes) from the config file. Use when: every check. Inputs: path. Output: two lists.
    Side effects: reads the file. Role: the one list."""
    inc: list[str] = []
    exc: list[str] = []
    for line in path.read_text().splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            (exc if line.startswith("!") else inc).append(line.lstrip("!"))
    return inc, exc


def _hit(path: str, pattern: str) -> bool:
    """What: does one path match one entry (exact, or under a trailing-slash prefix). Use when: gated_paths.
    Inputs: path, entry. Output: bool. Side effects: none. Role: helper."""
    return path == pattern or (pattern.endswith("/") and path.startswith(pattern))


def gated_paths(files: Sequence[str], config: tuple[list[str], list[str]] | None = None) -> list[str]:
    """What: the changed files that change agent behaviour. Use when: deciding whether the smoke is required.
    Inputs: paths, optional config. Output: the gated ones, sorted. Side effects: none. Role: the config applied
    (an exact include beats an exclude prefix)."""
    inc, exc = config or load_config()
    out = []
    for f in files:
        exact = f in inc
        if exact or (any(_hit(f, p) for p in inc) and not any(_hit(f, p) for p in exc)):
            out.append(f)
    return sorted(set(out))


def changed(a: str, b: str, run: Run = _run, three_dot: bool = True) -> list[str]:
    """What: files changed between two commits, renames listed as delete plus add. Use when: the check.
    Inputs: commits, runner, merge-base (three dots) or direct diff. Output: paths; raises RuntimeError on a git error.
    Side effects: git diff. Role: review I3 (a rename must not hide the old path)."""
    r = run(["git", "diff", "--name-only", "--no-renames", f"{a}...{b}" if three_dot else f"{a}..{b}"])
    if r.returncode != 0:
        raise RuntimeError(f"git diff {a} {b} failed: {r.stderr.strip()[:200]}")
    return r.stdout.split()


def smoke_status(repo: str, sha: str, run: Run = _run) -> dict[str, Any] | None:
    """What: the latest `live-smoke` status on one commit. Use when: the check walks PR commits.
    Inputs: repo, sha, runner. Output: {state, description} or None. Side effects: one GitHub API read.
    Role: raises RuntimeError when the API cannot be read (fail closed, never "no status")."""
    r = run(["gh", "api", f"repos/{repo}/commits/{sha}/status"])
    if r.returncode != 0:
        raise RuntimeError(f"cannot read statuses of {sha[:12]}: {r.stderr.strip()[:200]}")
    for s in json.loads(r.stdout).get("statuses") or []:
        if s.get("context") == CONTEXT:
            return {"state": s.get("state"), "description": s.get("description") or ""}
    return None


def check(repo: str, base: str, head: str, run: Run = _run) -> tuple[bool, str]:
    """What: the whole check (see module). Use when: main. Inputs: repo, base and head commits, runner.
    Output: (ok, reason). Side effects: git and API reads. Role: fail closed on any read error."""
    try:
        gated = gated_paths(changed(base, head, run))
        if not gated:
            return True, "no agent-behaviour path changed; the live smoke is not required"
        names = ", ".join(gated[:4]) + (f" and {len(gated) - 4} more" if len(gated) > 4 else "")
        r = run(["git", "rev-list", f"--max-count={MAX_COMMITS}", f"{base}..{head}"])
        if r.returncode != 0:
            return False, f"git rev-list failed: {r.stderr.strip()[:200]}"
        for sha in r.stdout.split():  # newest first: the latest smoke decides
            st = smoke_status(repo, sha, run)
            if st is None:
                continue
            if st["state"] != "success":
                return False, f"latest live-smoke on {sha[:12]} is {st['state']}: {st['description']}"
            cap = re.search(r"\bcap (\d+)\b", st["description"])
            if cap is None or int(cap.group(1)) > CAP_LIMIT:
                return False, f"live-smoke on {sha[:12]} does not show a cap of at most {CAP_LIMIT}: {st['description']}"
            since = gated_paths(changed(sha, head, run, three_dot=False))
            if since:
                return False, (f"live-smoke passed on {sha[:12]}, but gated paths changed after it: "
                               f"{', '.join(since[:4])}; rerun RUN_REAL=1 scripts/live_smoke.sh on the head")
            return True, f"live-smoke success on {sha[:12]} ({st['description']}) covers {names}"
        return False, f"changes {names}: no live-smoke status on any PR commit; run RUN_REAL=1 scripts/live_smoke.sh"
    except RuntimeError as e:
        return False, str(e)


def targets(event_name: str, payload: dict[str, Any], repo: str, run: Run = _run) -> list[tuple[int, str, str]]:
    """What: the (PR number, base sha, head sha) to evaluate for one event. Use when: event mode.
    Inputs: event name, payload, repo, runner. Output: list. Side effects: one API read for a status event.
    Role: a push re-evaluates its PR; a live-smoke status re-evaluates every open PR to main containing that sha."""
    if event_name == "pull_request_target":
        pr = payload["pull_request"]
        return [(int(pr["number"]), pr["base"]["sha"], pr["head"]["sha"])]
    if event_name == "status":
        if payload.get("context") != CONTEXT:
            return []
        r = run(["gh", "api", f"repos/{repo}/commits/{payload['sha']}/pulls"])
        if r.returncode != 0:
            raise RuntimeError(f"cannot list PRs of {payload['sha'][:12]}: {r.stderr.strip()[:200]}")
        return [(int(p["number"]), p["base"]["sha"], p["head"]["sha"]) for p in json.loads(r.stdout)
                if p.get("state") == "open" and p["base"]["ref"] == "main"]
    raise RuntimeError(f"unsupported event {event_name}")


def post(repo: str, sha: str, ok: bool, why: str, run: Run = _run) -> None:
    """What: post the `smoke-gate` status on a PR head. Use when: event mode. Inputs: repo, sha, result, reason,
    runner. Output: none; raises RuntimeError when the post failed. Side effects: one API POST. Role: the result
    branch protection can require."""
    r = run(["gh", "api", f"repos/{repo}/statuses/{sha}", "-f", f"state={'success' if ok else 'failure'}",
             "-f", f"context={GATE_CONTEXT}", "-f", f"description={why[:140]}"])
    if r.returncode != 0:
        raise RuntimeError(f"cannot post {GATE_CONTEXT} on {sha[:12]}: {r.stderr.strip()[:200]}")


def main(argv: Sequence[str] | None = None, run: Run = _run) -> int:
    """What: command line (see module). Use when: the workflow or a person runs it. Inputs: argv. Output: exit code.
    Side effects: see module. Role: entry point."""
    p = argparse.ArgumentParser(prog="check_smoke_status")
    p.add_argument("--repo", default="AarvinGeorge/customer-support-voice-agent")
    p.add_argument("--base")
    p.add_argument("--head")
    p.add_argument("--event", help="GitHub event payload file (event mode)")
    p.add_argument("--event-name")
    a = p.parse_args(argv)
    if a.event:
        try:
            todo = targets(a.event_name or "", json.loads(Path(a.event).read_text()), a.repo, run)
            worst = 0
            for num, base, head in todo:
                f = run(["git", "fetch", "-q", "origin", f"+refs/pull/{num}/head:refs/remotes/pr/{num}"])
                ok, why = (False, f"git fetch of PR #{num} failed") if f.returncode != 0 else check(a.repo, base, head, run)
                print(f"PR #{num} {head[:12]}: {'ok' if ok else 'FAIL'}: {why}")
                post(a.repo, head, ok, why, run)
                worst = worst or (0 if ok else 1)
            if not todo:
                print("nothing to evaluate for this event")
            return worst
        except (RuntimeError, KeyError, ValueError) as e:
            print(f"FAIL: {e}")
            return 1
    if not (a.base and a.head):
        p.error("--base and --head, or --event")
    ok, why = check(a.repo, a.base, a.head, run)
    print(("ok: " if ok else "FAIL: ") + why)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
