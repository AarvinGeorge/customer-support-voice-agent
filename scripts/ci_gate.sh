#!/usr/bin/env bash
# What: the local merge gate (D-068). Fetches PR <n>, merges its head onto the latest origin/main in a throwaway worktree
#   under this checkout's ignored .tmp/, runs scripts/ci_local.sh there, and posts the commit status `local-ci` on the PR
#   head sha: success only when every check passed, failure otherwise. The description names the main sha tested.
# Use when: BACKUP gate (D-068): GitHub Actions is the gate; run this when Actions cannot run (for example a spending
#   limit) or to test a PR merged onto the latest main locally. `scripts/ci_gate.sh 74`.
#   Run it from a lane worktree (a linked git worktree), never from the main checkout `development/`.
# Inputs: the PR number. Env: CI_GATE_REPO (default AarvinGeorge/customer-support-voice-agent), CI_GATE_REMOTE (default
#   origin), CI_GATE_NO_POST=1 (run without posting a status; for dry runs).
# Output: ci_local.sh output and its summary; the full log kept at .tmp/ci-gate-logs/; exit 0 only when the merge was
#   clean and every check passed; 1 on a failed check or a merge conflict (both post state=failure); 2 on a refusal
#   before testing (wrong cwd, bad PR, PR not open or not based on main, another gate running): nothing is posted.
# Side effects: git fetch; a temporary worktree and a ci-gate lock in the git common dir (both removed on exit); one
#   `gh api .../statuses/<sha>` POST. Never touches the main checkout or branch protection.
# Role: the mechanical merge gate while Actions is unavailable: tests the merge result (class C), not memory (class H).
set -euo pipefail

REPO="${CI_GATE_REPO:-AarvinGeorge/customer-support-voice-agent}"
REMOTE="${CI_GATE_REMOTE:-origin}"

# What: print a refusal and exit 2. Use when: a precondition fails before any test ran. Inputs: message.
# Output: stderr line. Side effects: exits. Role: refusals never post a status.
refuse() { echo "ci_gate: REFUSED: $*" >&2; exit 2; }

[ "$#" -eq 1 ] || refuse "usage: scripts/ci_gate.sh <pr-number>"
PR="$1"
case "$PR" in ''|*[!0-9]*) refuse "PR number must be digits, got '$PR'" ;; esac

TOP="$(git rev-parse --show-toplevel 2>/dev/null)" || refuse "not inside a git checkout (cd into a lane worktree)"
cd "$TOP"
GIT_DIR_ABS="$(cd "$(git rev-parse --git-dir)" && pwd -P)"
COMMON_ABS="$(cd "$(git rev-parse --git-common-dir)" && pwd -P)"
[ "$GIT_DIR_ABS" != "$COMMON_ABS" ] || refuse "this is the main checkout ($TOP); run from a lane worktree (git worktree), never development/"
URL="$(git remote get-url "$REMOTE" 2>/dev/null)" || refuse "no remote '$REMOTE'"
case "$URL" in *"$REPO"*) ;; *) refuse "remote '$REMOTE' ($URL) is not $REPO" ;; esac
git check-ignore -q "$TOP/.tmp/ci-gate-probe" || refuse ".tmp/ is not git-ignored in this checkout (needs the D-068 .gitignore line)"

LOCK="$COMMON_ABS/ci-gate.lock"
mkdir "$LOCK" 2>/dev/null || refuse "another ci_gate run holds $LOCK (pid $(cat "$LOCK/pid" 2>/dev/null || echo unknown)); remove it only if that run is gone"
echo $$ > "$LOCK/pid"
WT=""
# What: remove the throwaway worktree and the lock. Use when: EXIT trap. Inputs: none. Output: none.
# Side effects: git worktree remove, rm of the lock. Role: leaves nothing behind, pass or fail.
cleanup() {
  if [ -n "$WT" ] && [ -d "$WT" ]; then git -C "$TOP" worktree remove --force "$WT" >/dev/null 2>&1 || rm -rf "$WT"; git -C "$TOP" worktree prune; fi
  rm -rf "$LOCK"
}
trap cleanup EXIT

INFO="$(gh pr view "$PR" --repo "$REPO" --json state,baseRefName,headRefOid --jq '[.state,.baseRefName,.headRefOid]|join(" ")')" || refuse "cannot read PR #$PR"
read -r STATE BASE GH_HEAD <<<"$INFO"
[ "$STATE" = OPEN ] || refuse "PR #$PR is $STATE, not OPEN"
[ "$BASE" = main ] || refuse "PR #$PR targets '$BASE', not main"

git fetch -q "$REMOTE" "+refs/heads/main:refs/remotes/$REMOTE/main"
MAIN_SHA="$(git rev-parse "refs/remotes/$REMOTE/main")"
git fetch -q "$REMOTE" "refs/pull/$PR/head"
HEAD_SHA="$(git rev-parse FETCH_HEAD)"
[ "$HEAD_SHA" = "$GH_HEAD" ] || refuse "fetched head $HEAD_SHA differs from GitHub's $GH_HEAD (a push in flight?); rerun"

# What: post the local-ci commit status on the PR head. Use when: a test outcome exists (pass, fail, conflict).
# Inputs: state, description (cut to the API's 140 characters). Output: none. Side effects: one GitHub API POST,
#   skipped when CI_GATE_NO_POST=1. Role: the only write to GitHub.
post() {
  local desc="${2:0:140}"
  echo "ci_gate: status $1 on $HEAD_SHA: $desc"
  if [ "${CI_GATE_NO_POST:-}" = 1 ]; then echo "ci_gate: CI_GATE_NO_POST=1, not posted"; return 0; fi
  gh api "repos/$REPO/statuses/$HEAD_SHA" -f state="$1" -f context=local-ci -f description="$desc" >/dev/null
}

mkdir -p "$TOP/.tmp/ci-gate-logs"
STAMP="$(date +%Y%m%dT%H%M%S)"
WT="$TOP/.tmp/ci-gate-pr$PR-$STAMP-$$"
LOG="$TOP/.tmp/ci-gate-logs/pr$PR-$STAMP.log"
git worktree add -q --detach "$WT" "$MAIN_SHA"
if ! git -C "$WT" -c user.name=ci-gate -c user.email=ci-gate@localhost merge -q --no-ff --no-verify --no-edit "$HEAD_SHA" >"$LOG" 2>&1; then
  cat "$LOG" >&2
  git -C "$WT" merge --abort >/dev/null 2>&1 || true
  post failure "merge conflict: PR head ${HEAD_SHA:0:7} does not merge cleanly onto main $MAIN_SHA"
  echo "ci_gate: FAIL: PR #$PR does not merge cleanly onto main $MAIN_SHA" >&2
  exit 1
fi
[ -x "$WT/scripts/ci_local.sh" ] || { post failure "no executable scripts/ci_local.sh in merge onto main $MAIN_SHA"; exit 1; }

echo "ci_gate: PR #$PR head $HEAD_SHA merged onto main $MAIN_SHA in $WT; running ci_local.sh (log $LOG)"
rc=0
(cd "$WT" && CI_DESCRIPTORS_BASE="$MAIN_SHA" CI_LOG_DIR="$TOP/.tmp/ci-gate-logs/pr$PR-$STAMP" scripts/ci_local.sh) 2>&1 | tee -a "$LOG" || rc=$?
SUMMARY="$(grep -E '^ci_local: ' "$LOG" | tail -1 | sed -E 's/^ci_local: //; s/ passed,/ pass/; s/ failed,/ fail/; s/ warned,/ warn/; s/ skipped/ skip/')"
[ -n "$SUMMARY" ] || SUMMARY="no summary line (ci_local.sh aborted, exit $rc)"
if [ "$rc" -eq 0 ]; then
  post success "$SUMMARY; merged onto main $MAIN_SHA"
  echo "ci_gate: PASS"
else
  post failure "$SUMMARY; merged onto main $MAIN_SHA"
  echo "ci_gate: FAIL (ci_local.sh exit $rc)" >&2
  exit 1
fi
