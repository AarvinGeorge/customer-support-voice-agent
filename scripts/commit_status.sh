# What: shared commit-status helper (sourced, not run) for the local gates: `live-smoke` (scripts/live_smoke.sh) and
#   `local-ci` (scripts/ci_gate.sh, D-068, PR #74). Posts GitHub commit statuses on one exact sha: pending at the start,
#   success only on a clean pass, failure from the caller's EXIT trap for every other ending.
# Use when: a local gate tests one commit and its result must be mechanical proof on that commit (PR #73 review I1).
#   Interface:
#     commit_status_begin CONTEXT SHA [DESCRIPTION]   post pending; arms the failure-on-exit
#     commit_status_success DESCRIPTION               post success (call only on a clean pass)
#     commit_status_finish RC [DESCRIPTION]           call first in the caller's EXIT trap: posts failure unless success
#                                                     was posted (RC is the exit code, named in the default description)
#     commit_status_post STATE DESCRIPTION            the raw post (pending, success, failure, error)
# Inputs: env COMMIT_STATUS_REPO (default AarvinGeorge/customer-support-voice-agent), COMMIT_STATUS_NO_POST=1 (print
#   instead of posting, for dry runs and tests); `gh` authenticated.
# Output: one stderr line per post. Side effects: `gh api repos/<repo>/statuses/<sha>` POSTs.
# Role: the only writer of gate statuses, so both gates post the same way.

COMMIT_STATUS_REPO="${COMMIT_STATUS_REPO:-AarvinGeorge/customer-support-voice-agent}"
_CS_CONTEXT=""
_CS_SHA=""
_CS_DONE=0

# What: post one status. Use when: the functions below. Inputs: state, description (cut to the API's 140 characters).
# Output: stderr line; returns non-zero when the post failed. Side effects: one GitHub API POST. Role: raw post.
commit_status_post() {
  local state="$1" desc="${2:0:140}"
  [ -n "$_CS_SHA" ] && [ -n "$_CS_CONTEXT" ] || { echo "commit_status: begin was not called" >&2; return 1; }
  echo "commit_status: $_CS_CONTEXT $state on $_CS_SHA: $desc" >&2
  if [ "${COMMIT_STATUS_NO_POST:-}" = 1 ]; then echo "commit_status: COMMIT_STATUS_NO_POST=1, not posted" >&2; return 0; fi
  gh api "repos/$COMMIT_STATUS_REPO/statuses/$_CS_SHA" -f state="$state" -f context="$_CS_CONTEXT" \
    -f description="$desc" >/dev/null
}

# What: start a gated run on one commit. Use when: after the preflight refusals, before testing. Inputs: context, full
# sha, optional description. Output: none. Side effects: posts pending. Role: arms commit_status_finish.
commit_status_begin() {
  _CS_CONTEXT="$1"; _CS_SHA="$2"; _CS_DONE=0
  commit_status_post pending "${3:-running}"
}

# What: record a clean pass. Use when: every check passed. Inputs: description. Output: none. Side effects: posts
# success. Role: the only path to success.
commit_status_success() {
  commit_status_post success "$1" && _CS_DONE=1
}

# What: close the run from the caller's EXIT trap. Use when: first thing in the trap. Inputs: exit code, optional
# description. Output: none. Side effects: posts failure unless success was posted or begin never ran.
# Role: a crash, a refusal after begin, Ctrl-C or a failed check can never leave pending or success behind.
commit_status_finish() {
  local rc="$1" desc="${2:-}"
  [ -n "$_CS_SHA" ] || return 0
  [ "$_CS_DONE" = 1 ] && return 0
  commit_status_post failure "${desc:-did not pass (exit $rc)}" || true
  _CS_DONE=1
}
