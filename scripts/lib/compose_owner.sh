#!/usr/bin/env bash
# What: Compose ownership guard: a run may tear down ONLY a project it created. `compose_claim <project>` before the
#   first `up`; `compose_teardown <project> <command...>` at every teardown site (`down -v` and friends).
# Use when: any script that creates and removes a throwaway Compose project (scripts/pg_tests.sh,
#   scripts/deploy/restore_pg_test.sh, scripts/live_smoke.sh). Source it: `source "$ROOT/scripts/lib/compose_owner.sh"`.
# Inputs: project name ([a-z0-9][a-z0-9_-]*). Env: COMPOSE_OWNER_DIR (marker root, default ~/.cache/quillstack-ci/
#   compose-owners, shared by every checkout on the machine), COMPOSE_TEARDOWN_LOG (where the teardown command's output
#   goes, default /dev/null). compose_claim exports COMPOSE_OWNER_RUN_ID.
# Output: return 0 on success; 2 on a bad name; 3 on a refusal (the project already has containers, volumes or
#   networks; docker cannot list them; another live run holds the claim; or, at teardown, the marker is missing or
#   belongs to another run). A refusal prints one stderr line and leaves the project alone.
# Side effects: docker ps, volume ls and network ls (read-only) at claim; a marker dir <root>/<project> holding run_id and
#   pid; at teardown the given command, run with COMPOSE_PROJECT_NAME=<project>, then the marker is removed.
# Role: the root fix for the PR #74 N1 incident (a guard regression let `down -v` remove cs-demo): teardown is safe by
#   construction, not by name filters.

# What: the marker dir for a project. Use when: internal. Inputs: project. Output: path on stdout. Side effects: none.
# Role: one place for the marker layout.
_compose_owner_dir() { echo "${COMPOSE_OWNER_DIR:-$HOME/.cache/quillstack-ci/compose-owners}/$1"; }

# What: validate a project name. Use when: both entry points. Inputs: name. Output: return 0 or 2 with a message.
# Side effects: none. Role: no path tricks in the marker path, no empty name.
_compose_owner_name_ok() {
  case "$1" in ''|*[!abcdefghijklmnopqrstuvwxyz0123456789_-]*|[_-]*) echo "compose_owner: bad project name '$1'" >&2; return 2 ;; esac
}

# What: does the project have any container, volume or network. Use when: claim and stale reclaim. Inputs: project.
# Output: return 0 when it has none, 1 when it has some, 3 when docker cannot answer. Side effects: read-only docker.
# Role: an existing project belongs to someone else.
_compose_owner_empty() {
  local f="label=com.docker.compose.project=$1" out
  out="$(docker ps -a -q --filter "$f" && docker volume ls -q --filter "$f" && docker network ls -q --filter "$f")" || return 3
  [ -z "$out" ]
}

# What: claim a project for this run. Use when: before the first `up`. Inputs: project. Output: return 0, 2 or 3.
# Side effects: creates the marker dir (atomic mkdir); exports COMPOSE_OWNER_RUN_ID. Role: the only way to own a project.
compose_claim() {
  local p="${1:-}" d rc
  _compose_owner_name_ok "$p" || return 2
  d="$(_compose_owner_dir "$p")"
  mkdir -p "$(dirname "$d")"
  if ! mkdir "$d" 2>/dev/null; then
    local old; old="$(cat "$d/pid" 2>/dev/null || true)"
    if [ -n "$old" ] && kill -0 "$old" 2>/dev/null; then echo "compose_owner: $p is claimed by live pid $old; leaving it" >&2; return 3; fi
    # A dead claim: reclaim only when the project is empty, and atomically (one mv wins).
    _compose_owner_empty "$p" || { echo "compose_owner: $p has a dead claim and still has resources; leaving it" >&2; return 3; }
    mv "$d" "$d.stale.$$" 2>/dev/null || { echo "compose_owner: lost the reclaim race for $p" >&2; return 3; }
    rm -rf "$d.stale.$$"
    mkdir "$d" 2>/dev/null || { echo "compose_owner: lost the claim race for $p" >&2; return 3; }
  fi
  rc=0; _compose_owner_empty "$p" || rc=$?
  if [ "$rc" != 0 ]; then
    rm -rf "$d"
    if [ "$rc" = 3 ]; then echo "compose_owner: docker cannot list $p's resources; refusing to claim" >&2
    else echo "compose_owner: $p already has containers, volumes or networks (not created by this run); leaving it" >&2; fi
    return 3
  fi
  COMPOSE_OWNER_RUN_ID="$$-$(date +%s)-$RANDOM$RANDOM"
  export COMPOSE_OWNER_RUN_ID
  printf '%s\n' "$COMPOSE_OWNER_RUN_ID" > "$d/run_id"
  printf '%s\n' "$$" > "$d/pid"
}

# What: run a teardown command for a project only if this run claimed it. Use when: every `down` site, usually in an
#   EXIT trap. Inputs: project, then the command and its arguments. Output: the command's return code, or 2 or 3.
# Side effects: the command (output to COMPOSE_TEARDOWN_LOG), run with COMPOSE_PROJECT_NAME=<project>; removes the marker.
# Role: the single gate in front of every destructive Compose call.
compose_teardown() {
  local p="${1:-}" d mine; shift || true
  _compose_owner_name_ok "$p" || return 2
  [ "$#" -gt 0 ] || { echo "compose_owner: no teardown command for $p" >&2; return 2; }
  d="$(_compose_owner_dir "$p")"
  mine="$(cat "$d/run_id" 2>/dev/null || true)"
  if [ -z "${COMPOSE_OWNER_RUN_ID:-}" ] || [ "$mine" != "$COMPOSE_OWNER_RUN_ID" ]; then
    echo "compose_owner: $p was not created by this run (no matching marker); leaving it" >&2
    return 3
  fi
  local rc=0
  COMPOSE_PROJECT_NAME="$p" "$@" >>"${COMPOSE_TEARDOWN_LOG:-/dev/null}" 2>&1 || rc=$?
  rm -rf "$d"
  return "$rc"
}
