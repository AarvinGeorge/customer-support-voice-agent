#!/usr/bin/env bash
# What: the live smoke gate for agent-behaviour PRs. Brings up a throwaway Compose stack (its own project name, edge port,
#   Postgres test port and edge subnet), migrates, seeds and ingests, runs `python -m evals smoke` through Caddy (the fixed
#   S1, S3, S4, S5 subset with the golden code graders plus live checks per turn), prints the one-screen summary and tears
#   the stack down.
# Use when: before opening or updating a PR that touches agent gates, middleware, prompts, data/models.yaml or wiring
#   (the paths in scripts/smoke_gated_paths.txt); it posts the live-smoke status the smoke-gate check reads.
# Inputs: RUN_REAL=1 (required: real model calls), .env (secrets; sourced, never printed), a commit pushed to origin
#   (the status is posted on it), optional SMOKE_MAX_CALLS (hard cap, default 25, at most 60), SMOKE_OUT (output
#   directory), KEEP_STACK=1 (skip teardown, for debugging), COMMIT_STATUS_NO_POST=1 (run without posting).
# Output: the summary on stdout; smoke-<time>.txt and .json, status.txt, stack.log and build.log in the output
#   directory; on a clean, pushed tree the commit status `live-smoke` on HEAD (pending, then success only on PASS,
#   failure otherwise; the shared scripts/lib/commit_status.sh, owned by PR #74). Exit 0 PASS, 1 FAIL, 2 refused or setup failed, 3 aborted at the
#   call cap, 130/143/129 interrupted.
# Side effects: docker compose build, up, run and down -v --rmi local for project cs-smoke-<time>-<hex> only; a run
#   marker under .tmp/live-smoke/ (removed at exit); one to two GitHub status POSTs; real Anthropic and OpenAI
#   calls (agent, verifier, classifier, embeddings). Never touches another project (cs-demo on port 3000 included).
# Role: root-cause audit class A, priority 1 (b): real model behaviour is checked before merge, not by a browser pass after.
set -euo pipefail

die() { echo "live_smoke: $*" >&2; exit 2; }

[ "${RUN_REAL:-}" = "1" ] || die "refusing: this makes real model calls (about 20, capped at ${SMOKE_MAX_CALLS:-25}); set RUN_REAL=1"

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
[ -e .deploy ] && die "$ROOT/.deploy exists: this checkout is a deploy target; never run the smoke here"
[ -f .env ] || die ".env is missing (python3 scripts/keys.py status)"
command -v docker >/dev/null || die "docker is not installed"
command -v uv >/dev/null || die "uv is not installed"

# Secrets for the evals CLI (DEMO_ACCESS_CODE, CORE_DB_URL, AGENT_DB_URL); exported, never echoed. Ours are set after.
set -a
# shellcheck disable=SC1091
. ./.env
set +a

# free_port LOW HIGH: the first loopback TCP port in [LOW, HIGH] nothing listens on.
free_port() {
  python3 -c '
import socket, sys
for p in range(int(sys.argv[1]), int(sys.argv[2]) + 1):
    s = socket.socket()
    try:
        s.bind(("127.0.0.1", p)); print(p); break
    except OSError:
        pass
    finally:
        s.close()
else:
    sys.exit(1)' "$1" "$2" || die "no free port in $1..$2"
}

# free_subnet: a 172.30.N.0/24 (N 100..199) no Docker network uses.
free_subnet() {
  local used n
  used="$(docker network inspect $(docker network ls -q) -f '{{range .IPAM.Config}}{{.Subnet}} {{end}}' 2>/dev/null || true)"
  for n in $(seq 100 199); do
    case " $used " in *" 172.30.$n.0/24 "*) ;; *) echo "172.30.$n.0/24"; return 0 ;; esac
  done
  die "no free 172.30.N.0/24 subnet"
}

STAMP="$(date -u +%H%M%S)-$(python3 -c 'import secrets; print(secrets.token_hex(2))')"
export COMPOSE_PROJECT_NAME="cs-smoke-$STAMP"
export COMPOSE_FILE="compose.yaml:compose.override.yaml:infra/compose.pgtest.yaml"
export EDGE_PORT; EDGE_PORT="$(free_port 3100 3199)"
export PG_TEST_PORT; PG_TEST_PORT="$(free_port 55500 55599)"
export EDGE_SUBNET; EDGE_SUBNET="$(free_subnet)"
export DEMO_FAULTS_ENABLED=true
export SITE_ORIGIN="http://localhost:$EDGE_PORT"
export LANGSMITH_PROJECT=cs-smoke   # traces stay out of the demo's LangSmith project (review M6)
case "$COMPOSE_PROJECT_NAME" in *demo*|*prod*) die "refusing project $COMPOSE_PROJECT_NAME" ;; esac
[ "$EDGE_PORT" != 3000 ] || die "refusing port 3000 (cs-demo)"
OUT="${SMOKE_OUT:-${TMPDIR:-/tmp}/live-smoke-$COMPOSE_PROJECT_NAME}"
mkdir -p "$OUT"

case "${CORE_DB_URL:-}" in *@postgres:5432/*) ;; *) die "CORE_DB_URL in .env does not name @postgres:5432 (cannot map it to the test port)" ;; esac
case "${AGENT_DB_URL:-}" in *@postgres:5432/*) ;; *) die "AGENT_DB_URL in .env does not name @postgres:5432" ;; esac
export EVALS_CORE_DB_URL="${CORE_DB_URL/@postgres:5432\//@127.0.0.1:$PG_TEST_PORT/}"
export EVALS_AGENT_DB_URL="${AGENT_DB_URL/@postgres:5432\//@127.0.0.1:$PG_TEST_PORT/}"
export EVALS_AGENT_URL="http://localhost:$EDGE_PORT"
SHA="$(git rev-parse HEAD)"
SMOKE_COMMIT="${SHA:0:12}"; export SMOKE_COMMIT
# Dirty: tracked changes, or untracked files where the stack reads code or data (compose.override.yaml bind-mounts
# services/*/src; review M5).
if [ -n "$(git status --porcelain --untracked-files=no)" ] || \
   [ -n "$(git status --porcelain --untracked-files=all -- services data packages evals infra | grep '^??' || true)" ]; then
  export SMOKE_DIRTY=1
else
  export SMOKE_DIRTY=0
fi
POST=1
if [ "$SMOKE_DIRTY" = 1 ]; then
  POST=0; echo "live_smoke: the tree is dirty: running, but no live-smoke status will be posted" >&2
elif [ "${COMMIT_STATUS_NO_POST:-}" != 1 ]; then
  git fetch -q origin || die "git fetch origin failed (needed to prove $SMOKE_COMMIT is pushed)"
  [ -n "$(git branch -r --contains "$SHA" 2>/dev/null)" ] || die "commit $SMOKE_COMMIT is not on origin: push it first (the live-smoke status is posted on it)"
fi
# The shared status writer (scripts/lib/commit_status.sh, owned by PR #74; this copy is byte-identical). The lifecycle
# is the same as scripts/ci_gate.sh: pending at start, success only on a clean pass, failure from the EXIT trap.
# shellcheck source=lib/commit_status.sh
. "$ROOT/scripts/lib/commit_status.sh"
STARTED=0
FINAL=0

# This run's marker: evals.reseed and `python -m evals smoke` accept only the cs-smoke-* stack whose marker matches.
MARKER_DIR="$ROOT/.tmp/live-smoke"
mkdir -p "$MARKER_DIR"
SMOKE_RUN_NONCE="$(python3 -c 'import secrets; print(secrets.token_hex(16))')"; export SMOKE_RUN_NONCE
MARKER="$MARKER_DIR/$COMPOSE_PROJECT_NAME.run"
( umask 077; printf '%s\n' "$SMOKE_RUN_NONCE" >"$MARKER" )

teardown() {
  local code=$?
  trap - EXIT INT TERM HUP
  if [ "$POST" = 1 ] && [ "$STARTED" = 1 ] && [ "$FINAL" = 0 ]; then
    commit_status "$SHA" failure live-smoke "$(cat "$OUT/status.txt" 2>/dev/null || echo "did not pass (exit $code)")" >&2 || true
  fi
  rm -f "$MARKER"
  docker compose logs --no-color >"$OUT/stack.log" 2>&1 || true
  if [ "${KEEP_STACK:-}" = 1 ]; then
    echo "live_smoke: KEEP_STACK=1, $COMPOSE_PROJECT_NAME left up on 127.0.0.1:$EDGE_PORT" >&2
  else
    docker compose --profile seed --profile jobs down -v --remove-orphans --rmi local >>"$OUT/build.log" 2>&1 || echo "live_smoke: teardown of $COMPOSE_PROJECT_NAME failed" >&2
    left="$(docker ps -a -q --filter "label=com.docker.compose.project=$COMPOSE_PROJECT_NAME" | wc -l | tr -d ' ')"
    echo "live_smoke: teardown $COMPOSE_PROJECT_NAME, containers left: $left; output in $OUT" >&2
    if [ "$left" != 0 ] && [ "$code" = 0 ]; then code=2; fi   # a pass that leaves containers is not clean (review M2)
  fi
  exit "$code"
}
trap teardown EXIT
trap 'exit 130' INT    # Ctrl-C is never a success (review M1)
trap 'exit 143' TERM
trap 'exit 129' HUP

step() { echo "live_smoke: $*" >&2; }
step "project $COMPOSE_PROJECT_NAME, edge 127.0.0.1:$EDGE_PORT, pg 127.0.0.1:$PG_TEST_PORT, subnet $EDGE_SUBNET, commit $SMOKE_COMMIT"
if [ "$POST" = 1 ]; then
  commit_status "$SHA" pending live-smoke "running on $COMPOSE_PROJECT_NAME" >&2 || die "cannot post the live-smoke status on $SMOKE_COMMIT"
  STARTED=1
fi
step "build and up (log: $OUT/build.log)"
if ! { docker compose build && docker compose up -d --wait; } >>"$OUT/build.log" 2>&1; then
  tail -n 30 "$OUT/build.log" >&2; die "stack did not come up"
fi
step "migrate"
scripts/migrate.sh >>"$OUT/build.log" 2>&1 || { tail -n 20 "$OUT/build.log" >&2; die "migrate failed"; }
step "seed"
docker compose --profile seed run --rm seed_core >>"$OUT/build.log" 2>&1 || { tail -n 20 "$OUT/build.log" >&2; die "seed failed"; }
step "ingest"
ingest="$(docker compose --profile jobs run --rm ingest_knowledge 2>>"$OUT/build.log")" || die "ingest failed"
echo "$ingest" >>"$OUT/build.log"
SMOKE_INGEST_EMBEDDING_CALLS="$(printf '%s\n' "$ingest" | sed -n 's/.*INGEST ok .*embedding_calls=\([0-9][0-9]*\).*/\1/p')"
[ -n "$SMOKE_INGEST_EMBEDDING_CALLS" ] || die "ingest printed no embedding_calls count"
export SMOKE_INGEST_EMBEDDING_CALLS
ready="$(curl -fsS "http://127.0.0.1:$EDGE_PORT/api/readyz")" || die "readyz through Caddy failed"
case "$ready" in *'"status":"ready"'*'"demo_faults":true'*) ;; *) die "stack not ready through Caddy: $ready" ;; esac

step "smoke (cap ${SMOKE_MAX_CALLS:-25} real model calls)"
set +e
uv run --project evals --frozen python -m evals smoke --out-dir "$OUT" --status-out "$OUT/status.txt"
code=$?
set -e
if [ "$code" = 0 ] && [ "$POST" = 1 ]; then
  if commit_status "$SHA" success live-smoke "$(cat "$OUT/status.txt")" >&2; then FINAL=1
  else echo "live_smoke: posting the success status failed" >&2; code=1; fi
fi
exit "$code"
