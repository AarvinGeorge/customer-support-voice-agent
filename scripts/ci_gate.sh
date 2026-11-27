#!/usr/bin/env bash
# What: the local merge gate (D-068). Fetches PR <n>, merges its head onto the latest origin/main in a throwaway worktree
#   under this checkout's ignored .tmp/, runs scripts/ci_local.sh there, and posts the commit status `local-ci` on the PR
#   head sha: `pending` when the run starts, then `success` only on a clean pass and `failure` on any other exit
#   (conflict, failed check, abort, signal), so the status always reflects the latest run. Descriptions name the main sha.
# Use when: BACKUP gate (D-068): GitHub Actions is the gate; run this when Actions cannot run (for example a spending
#   limit) or to test a PR merged onto the latest main locally. `scripts/ci_gate.sh 74`.
#   Run it from a lane worktree (a linked git worktree), never from the main checkout `development/`.
# Inputs: the PR number. Env: CI_GATE_REPO (default AarvinGeorge/customer-support-voice-agent), CI_GATE_REMOTE (default
#   origin), CI_GATE_NO_POST=1 (run without posting a status; for dry runs).
# Output: ci_local.sh output and its summary; the full log kept at .tmp/ci-gate-logs/; exit 0 only when the merge was
#   clean and every check passed; 1 on a conflict, a failed check or an abort (failure posted); 2 on a refusal before
#   testing (wrong cwd, wrong remote, bad PR, fork PR, PR not open or not based on main, head race, another live gate
#   run): nothing is posted.
# Side effects: git fetch; a temporary worktree and a ci-gate lock in the git common dir (both removed on exit; a lock
#   left by a dead run is reclaimed); `gh api .../statuses/<sha>` POSTs via scripts/lib/commit_status.sh. Never touches
#   the main checkout's working tree or branch protection. Runs the PR's code locally, so fork PRs are refused.
# Role: the mechanical merge gate while Actions is unavailable: tests the merge result (class C), not memory (class H).
set -euo pipefail

REPO="${CI_GATE_REPO:-AarvinGeorge/customer-support-voice-agent}"
REMOTE="${CI_GATE_REMOTE:-origin}"
# shellcheck source=lib/commit_status.sh
source "$(cd "$(dirname "$0")" && pwd)/lib/commit_status.sh"
export COMMIT_STATUS_REPO="$REPO"
[ "${CI_GATE_NO_POST:-}" = 1 ] && export COMMIT_STATUS_NO_POST=1

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
if ! mkdir "$LOCK" 2>/dev/null; then
  OLD="$(cat "$LOCK/pid" 2>/dev/null || true)"
  if [ -n "$OLD" ] && ! kill -0 "$OLD" 2>/dev/null; then
    # The holder is gone (for example SIGKILL): reclaim the lock and sweep the worktrees such a run leaves.
    echo "ci_gate: reclaiming the lock of dead run pid $OLD"
    for d in "$TOP"/.tmp/ci-gate-pr*; do [ -d "$d" ] && { git worktree remove --force "$d" 2>/dev/null || rm -rf "$d"; }; done
    git worktree prune
    rm -rf "$LOCK"; mkdir "$LOCK"
  else
    refuse "another ci_gate run holds $LOCK (pid ${OLD:-unknown}, alive)"
  fi
fi
echo $$ > "$LOCK/pid"
WT="" HEAD_SHA="" MAIN_SHA="" PENDING=0 FINAL=0

# What: post a final status once (success or failure). Use when: every outcome after `pending`. Inputs: state, text.
# Output: the helper's line. Side effects: one POST. Role: marks the run reported so the EXIT trap does not repost.
final() { FINAL=1; commit_status "$HEAD_SHA" "$1" local-ci "$2"; }

# What: on every exit: post failure if a pending run ends without a final status, then remove the worktree and lock.
# Use when: EXIT trap (signals are routed to exit). Inputs: the exit code. Output: none.
# Side effects: maybe one POST; git worktree remove; rm of the lock. Role: no stale success, nothing left behind.
cleanup() {
  local rc=$?
  if [ "$PENDING" = 1 ] && [ "$FINAL" = 0 ]; then
    final failure "aborted (exit $rc) before a result; merge onto main $MAIN_SHA" || true
  fi
  if [ -n "$WT" ] && [ -d "$WT" ]; then git -C "$TOP" worktree remove --force "$WT" >/dev/null 2>&1 || rm -rf "$WT"; git -C "$TOP" worktree prune; fi
  rm -rf "$LOCK"
  exit "$rc"
}
trap cleanup EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM

INFO="$(gh pr view "$PR" --repo "$REPO" --json state,baseRefName,headRefOid,isCrossRepository \
  --jq '[.state,.baseRefName,.headRefOid,(.isCrossRepository|tostring)]|join(" ")')" || refuse "cannot read PR #$PR"
read -r STATE BASE GH_HEAD CROSS <<<"$INFO"
[ "$CROSS" = false ] || refuse "PR #$PR comes from a fork (isCrossRepository=$CROSS); the gate only runs branches of $REPO"
[ "$STATE" = OPEN ] || refuse "PR #$PR is $STATE, not OPEN"
[ "$BASE" = main ] || refuse "PR #$PR targets '$BASE', not main"

git fetch -q "$REMOTE" "+refs/heads/main:refs/remotes/$REMOTE/main"
MAIN_SHA="$(git rev-parse "refs/remotes/$REMOTE/main")"
git fetch -q "$REMOTE" "refs/pull/$PR/head"
HEAD_SHA="$(git rev-parse FETCH_HEAD)"
[ "$HEAD_SHA" = "$GH_HEAD" ] || refuse "fetched head $HEAD_SHA differs from GitHub's $GH_HEAD (a push in flight?); rerun"

commit_status "$HEAD_SHA" pending local-ci "running: merge of ${HEAD_SHA:0:7} onto main $MAIN_SHA"
PENDING=1

mkdir -p "$TOP/.tmp/ci-gate-logs"
STAMP="$(date +%Y%m%dT%H%M%S)"
WT="$TOP/.tmp/ci-gate-pr$PR-$STAMP-$$"
LOG="$TOP/.tmp/ci-gate-logs/pr$PR-$STAMP.log"
git worktree add -q --detach "$WT" "$MAIN_SHA"
if ! git -C "$WT" -c user.name=ci-gate -c user.email=ci-gate@localhost merge -q --no-ff --no-verify --no-edit "$HEAD_SHA" >"$LOG" 2>&1; then
  cat "$LOG" >&2
  git -C "$WT" merge --abort >/dev/null 2>&1 || true
  final failure "merge conflict: PR head ${HEAD_SHA:0:7} does not merge cleanly onto main $MAIN_SHA"
  echo "ci_gate: FAIL: PR #$PR does not merge cleanly onto main $MAIN_SHA" >&2
  exit 1
fi
[ -x "$WT/scripts/ci_local.sh" ] || { final failure "no executable scripts/ci_local.sh in merge onto main $MAIN_SHA"; exit 1; }

echo "ci_gate: PR #$PR head $HEAD_SHA merged onto main $MAIN_SHA in $WT; running ci_local.sh (log $LOG)"
rc=0
(cd "$WT" && CI_DESCRIPTORS_BASE="$MAIN_SHA" CI_LOG_DIR="$TOP/.tmp/ci-gate-logs/pr$PR-$STAMP" scripts/ci_local.sh) 2>&1 | tee -a "$LOG" || rc=$?
SUMMARY="$(grep -E '^ci_local: ' "$LOG" | tail -1 | sed -E 's/^ci_local: //; s/ passed,/ pass/; s/ failed,/ fail/; s/ skipped/ skip/' || true)"
if [ "$rc" -eq 0 ] && [ -n "$SUMMARY" ]; then
  final success "$SUMMARY; merged onto main $MAIN_SHA"
  echo "ci_gate: PASS"
elif [ -n "$SUMMARY" ]; then
  final failure "$SUMMARY; merged onto main $MAIN_SHA"
  echo "ci_gate: FAIL (ci_local.sh exit $rc)" >&2
  exit 1
else
  final failure "aborted: ci_local.sh exit $rc with no summary; merge onto main $MAIN_SHA"
  echo "ci_gate: FAIL (ci_local.sh aborted, exit $rc, no summary line)" >&2
  exit 1
fi
