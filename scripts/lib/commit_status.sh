#!/usr/bin/env bash
# What: the one writer of GitHub commit statuses for local gates (D-068): `commit_status <sha> <state> <context> <description>`.
# Use when: a local gate reports a result on a commit: scripts/ci_gate.sh (context local-ci) and the live-smoke gate
#   (PR #73, context of its choice). Source it: `source "$(dirname "$0")/lib/commit_status.sh"`.
# Inputs: sha (40 hex), state (pending, success, failure or error), context (non-empty), description (cut to the API's
#   140 characters). Env: COMMIT_STATUS_REPO (default AarvinGeorge/customer-support-voice-agent), COMMIT_STATUS_NO_POST=1
#   (print only, for dry runs).
# Output: one `status <state> <context> on <sha>: <description>` line on stdout. Returns 0 when posted, 2 on a bad
#   argument (nothing posted), or gh's exit code when the POST fails.
# Side effects: one `gh api repos/<repo>/statuses/<sha>` POST. Role: shared status helper; callers own when to post.

# What: validate and post one commit status. Use when: see the file header. Inputs: sha, state, context, description.
# Output: one line; return code as in the header. Side effects: one GitHub API POST. Role: the only status POST.
commit_status() {
  local sha="${1:-}" state="${2:-}" context="${3:-}" desc="${4:-}"
  local repo="${COMMIT_STATUS_REPO:-AarvinGeorge/customer-support-voice-agent}"
  case "$sha" in *[!0-9a-f]*|'') echo "commit_status: sha must be 40 lowercase hex, got '$sha'" >&2; return 2 ;; esac
  [ "${#sha}" -eq 40 ] || { echo "commit_status: sha must be 40 hex, got '$sha'" >&2; return 2; }
  case "$state" in pending|success|failure|error) ;; *) echo "commit_status: bad state '$state'" >&2; return 2 ;; esac
  [ -n "$context" ] || { echo "commit_status: empty context" >&2; return 2; }
  desc="${desc:0:140}"
  echo "status $state $context on $sha: $desc"
  if [ "${COMMIT_STATUS_NO_POST:-}" = 1 ]; then echo "commit_status: COMMIT_STATUS_NO_POST=1, not posted"; return 0; fi
  gh api "repos/$repo/statuses/$sha" -f state="$state" -f context="$context" -f description="$desc" >/dev/null
}
