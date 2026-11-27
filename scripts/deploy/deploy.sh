#!/usr/bin/env bash
# What: deploy, roll back, reseed or smoke-test the Quillstack Compose stack on the VM (or a laptop with --local).
# Use when: the owner or .github/workflows/deploy.yml ships a tag. Run it ON the VM from the repo checkout (see scripts/deploy/DEPLOY.md).
# Inputs: a command (up, rollback, reseed, smoke, check, render-env) and options (--tag, --env-file, --local, --print,
#   --state-dir, --out). Variables come from the env file (the shell wins on a clash, as in Compose); names are in
#   scripts/deploy/required-env.txt. Output: one line per step on stdout; errors on stderr; exit 0 only when every step passed.
# Side effects: git checkout of the tag; docker builds, one-shot jobs and container restarts; writes <state-dir>/current and
#   <state-dir>/previous (tags); `reseed` wipes the demo data. Never prints a secret value. --print runs nothing.
# Role: CS-E-08.S4.T2 (doc 09 s5, doc 06 s10, doc 07 s15.2; D-029, D-047, D-049): one ordered, repeatable deploy.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
HERE="$ROOT/scripts/deploy"
ENV_FILE=".env"
STATE_DIR="$ROOT/.deploy"
TAG=""
LOCAL=0
PRINT=0
OUT=""
CMD=""

die() { echo "deploy: $*" >&2; exit "${2:-1}"; }
usage() {
  cat <<'USAGE'
usage: scripts/deploy/deploy.sh [options] <command>
commands:
  up --tag TAG    checkout TAG, build, migrate, seed (first time only), ingest, rolling restart, smoke
  rollback        redeploy the previous tag recorded by the last successful `up`
  reseed          wipe and reseed the demo data (run before the Loom take), then smoke
  smoke           read /readyz from core, knowledge and the edge, then one chat turn
  check           validate the env file only
  render-env      write an env file from the process environment (GitHub secrets); needs --out
options: --env-file FILE (default .env)  --local (laptop dry-run: no git, local overlay)  --print (show commands, run none)
         --state-dir DIR  --out FILE
USAGE
}

while [ $# -gt 0 ]; do
  case "$1" in
    --tag) [ $# -ge 2 ] || die "--tag needs a value" 2; TAG="$2"; shift 2 ;;
    --env-file) [ $# -ge 2 ] || die "--env-file needs a value" 2; ENV_FILE="$2"; shift 2 ;;
    --state-dir) [ $# -ge 2 ] || die "--state-dir needs a value" 2; STATE_DIR="$2"; shift 2 ;;
    --out) [ $# -ge 2 ] || die "--out needs a value" 2; OUT="$2"; shift 2 ;;
    --local) LOCAL=1; shift ;;
    --print) PRINT=1; shift ;;
    -h|--help) usage; exit 0 ;;
    -*) usage >&2; die "unknown option: $1" 2 ;;
    *) [ -z "$CMD" ] || die "unexpected argument: $1" 2; CMD="$1"; shift ;;
  esac
done
[ -n "$CMD" ] || { usage >&2; die "usage: a command is required" 2; }
case "$CMD" in up|rollback|reseed|smoke|check|render-env) ;; *) usage >&2; die "unknown command: $CMD" 2 ;; esac
[ "$CMD" != up ] || [ "$LOCAL" = 1 ] || [ -n "$TAG" ] || die "up needs --tag TAG (the tag to ship)" 2

step() { echo "== $*"; }
# run: print the command; execute it unless --print.
run() { echo "+ $*"; [ "$PRINT" = 1 ] || "$@"; }

names() { grep -vE '^[[:space:]]*(#|$)' "$1" | awk '{print $1}'; }

# load_env FILE: export KEY=VALUE lines that are not already set in the shell (shell wins, like Compose). Never echoes values.
load_env() {
  [ -f "$1" ] || die "env file not found: $1 (render it with render-env or copy it from your keys.py .env)"
  local line k v
  while IFS= read -r line || [ -n "$line" ]; do
    case "$line" in ''|'#'*) continue ;; esac
    k="${line%%=*}"; v="${line#*=}"
    case "$k" in *[!A-Za-z0-9_]*|'') continue ;; esac
    v="${v%\"}"; v="${v#\"}"; v="${v%\'}"; v="${v#\'}"
    [ -n "$(printenv "$k" || true)" ] || export "$k=$v"
  done < "$1"
}

# preflight: every check that must hold before a step touches anything. Names only in messages.
preflight() {
  load_env "$ENV_FILE"
  local missing="" n
  for n in $(names "$HERE/required-env.txt"); do
    [ -n "$(printenv "$n" || true)" ] || missing="$missing $n"
  done
  [ -z "$missing" ] || die "missing or empty required variables:$missing"
  [ -n "${SITE_ADDRESS:-}" ] || die "SITE_ADDRESS is not set: Caddy refuses to start without it (the domain on the VM, :80 locally)"
  [ -n "${SITE_ORIGIN:-}" ] || die "SITE_ORIGIN is not set: the agent's Origin check needs it (https://<domain> on the VM)"
  local subnet="${EDGE_SUBNET:-172.30.0.0/24}" fwd="${FORWARDED_ALLOW_IPS:-${EDGE_SUBNET:-172.30.0.0/24}}"
  [ "$subnet" = "$fwd" ] || die "FORWARDED_ALLOW_IPS ($fwd) must equal EDGE_SUBNET ($subnet): uvicorn trusts X-Forwarded-For only from the edge network"
  case "${DEMO_RESEED:-false}" in 1|true|TRUE|True) [ "$CMD" = reseed ] || die "DEMO_RESEED is on in the env file: remove it; only the reseed command sets it" ;; esac
  if [ "$LOCAL" = 0 ]; then
    case "$SITE_ADDRESS" in :*|http://*) die "SITE_ADDRESS=$SITE_ADDRESS would serve plain http: set the domain (https is automatic)" ;; esac
    [ "$SITE_ORIGIN" = "https://$SITE_ADDRESS" ] || die "SITE_ORIGIN ($SITE_ORIGIN) must be https://$SITE_ADDRESS"
    if [ -n "$(find "$ENV_FILE" \( -perm -g+r -o -perm -o+r \) -print 2>/dev/null)" ]; then
      die "$ENV_FILE is readable by group or others: chmod 600 $ENV_FILE"
    fi
  fi
  echo "preflight ok (env file $ENV_FILE; secrets present: $(names "$HERE/required-env.txt" | wc -l | tr -d ' '))"
}

compose() {
  local files=(-f compose.yaml)
  [ "$LOCAL" = 0 ] || files+=(-f scripts/deploy/compose.local.yaml)
  run docker compose --env-file "$ENV_FILE" "${files[@]}" "$@"
}

smoke_base() { if [ "$LOCAL" = 1 ]; then echo "http://127.0.0.1:${EDGE_PORT:-8088}"; else echo "${SITE_ORIGIN}"; fi; }

# probe SERVICE PATH: GET a backend /readyz from a throwaway container of the release image (never exec into a live one).
probe() {
  local svc="$1" path="$2"
  compose run --rm --no-deps -T "$svc" python -c "import json,urllib.request;d=json.load(urllib.request.urlopen('http://$svc:8000$path',timeout=5));print(json.dumps(d))"
}

smoke() {
  step "SMOKE readyz and one chat turn"
  [ "$PRINT" = 0 ] || { echo "+ (smoke checks core, knowledge and edge readyz, then access, session, one turn)"; return 0; }
  local want_cv core know edge base
  want_cv="$(tr -d '[:space:]' < "$ROOT/packages/contracts/VERSION")"
  core="$(probe core /readyz 2>/dev/null | tail -n1)" || die "core /readyz failed"
  echo "core:      $core"
  echo "$core" | grep -q "\"contract_version\": *\"$want_cv\"" || die "core contract_version is not $want_cv"
  echo "$core" | grep -q '"policy_version"' || die "core /readyz has no policy_version"
  echo "$core" | grep -q '"status": *"ready"' || die "core is not ready"
  know="$(probe knowledge /readyz 2>/dev/null | tail -n1)" || die "knowledge /readyz failed"
  echo "knowledge: $know"
  echo "$know" | grep -q '"status": *"ready"' || die "knowledge is not ready"
  base="$(smoke_base)"
  edge="$(curl -fsS --max-time 10 "$base/api/readyz")" || die "edge $base/api/readyz failed"
  echo "edge:      $edge"
  echo "$edge" | grep -q "\"contract_version\": *\"$want_cv\"" || die "agent contract_version is not $want_cv"
  chat_turn "$base"
  echo "smoke ok"
}

# chat_turn BASE: access code, session, then one turn; SMOKE_CHAT=turn (default) | session (stop after the session) | off.
chat_turn() {
  local base="$1" sid cid out
  case "${SMOKE_CHAT:-turn}" in off) echo "chat: skipped (SMOKE_CHAT=off)"; return 0 ;; session|turn) ;; *) die "SMOKE_CHAT must be turn, session or off" ;; esac
  JAR="$(mktemp)"; trap 'rm -f "${JAR:-}"' EXIT
  local jar="$JAR"
  printf '{"code":"%s"}' "$DEMO_ACCESS_CODE" | curl -fsS --max-time 10 -o /dev/null -c "$jar" -H "Origin: $SITE_ORIGIN" \
    -H 'Content-Type: application/json' --data @- "$base/api/access" || die "POST /api/access failed (wrong DEMO_ACCESS_CODE or Origin)"
  sid="$(curl -fsS --max-time 10 -b "$jar" -H "Origin: $SITE_ORIGIN" -H 'Content-Type: application/json' --data '{}' "$base/api/sessions" \
    | sed -n 's/.*"session_id": *"\([^"]*\)".*/\1/p')"
  [ -n "$sid" ] || die "POST /api/sessions returned no session_id"
  echo "chat: session created"
  [ "${SMOKE_CHAT:-turn}" = turn ] || return 0
  cid="$(python3 -c 'import uuid;print(uuid.uuid4())')"
  out="$(curl -fsS --max-time 60 -N -b "$jar" -H "Origin: $SITE_ORIGIN" -H 'Content-Type: application/json' \
    --data "{\"client_turn_id\":\"$cid\",\"text\":\"How do I reset my password?\"}" "$base/api/sessions/$sid/turns")" \
    || die "POST /api/sessions/{sid}/turns failed"
  echo "$out" | grep -q '^event: turn.completed' || die "the chat turn stream did not end with turn.completed"
  echo "chat: one turn completed"
}

check_tag() {
  [ "$LOCAL" = 1 ] && return 0
  [ "$PRINT" = 1 ] && return 0
  git -C "$ROOT" rev-parse -q --verify "refs/tags/$1^{commit}" >/dev/null || die "tag $1 not found after fetch"
  [ -z "$(git -C "$ROOT" status --porcelain)" ] || die "the checkout has uncommitted changes; the VM checkout must stay clean"
}

checkout_tag() {
  [ "$LOCAL" = 1 ] && return 0
  step "git checkout $1"
  run git -C "$ROOT" fetch --tags --prune origin
  check_tag "$1"
  run git -C "$ROOT" checkout --detach "refs/tags/$1"
}

wait_note() { echo "(waiting for healthy: --wait, up to 180 s)"; }

migrate_and_seed() {
  step "migrate_core migrate_knowledge migrate_agent (owner roles, order core, knowledge, agent)"
  for job in migrate_core migrate_knowledge migrate_agent; do compose run --rm --no-deps "$job"; done
  step "seed_core (seeds an empty schema; an existing demo is left untouched)"
  compose run --rm --no-deps seed_core
  step "ingest_knowledge (re-run safe: unchanged articles cost no embedding call)"
  compose run --rm --no-deps ingest_knowledge
}

rolling_restart() {
  step "rolling restart: core and knowledge, then agent_a, then agent_b, then web and caddy"
  wait_note
  compose up -d --no-deps --wait --wait-timeout 180 --force-recreate core knowledge
  compose up -d --no-deps --wait --wait-timeout 180 --force-recreate agent_a
  compose up -d --no-deps --wait --wait-timeout 180 --force-recreate agent_b
  compose up -d --no-deps --wait --wait-timeout 180 web
  compose up -d --no-deps --wait --wait-timeout 180 caddy
}

bring_up() {
  step "build images on this host"
  compose build
  step "postgres and redis"
  compose up -d --no-deps --wait --wait-timeout 180 postgres redis
  migrate_and_seed
  rolling_restart
}

record() { # record NEW_TAG: current becomes previous, NEW_TAG becomes current (after a green smoke)
  [ "$PRINT" = 0 ] || { echo "+ (record $1 as current; the old current becomes previous in $STATE_DIR)"; return 0; }
  mkdir -p "$STATE_DIR"
  if [ -f "$STATE_DIR/current" ] && [ "$(cat "$STATE_DIR/current")" != "$1" ]; then cp "$STATE_DIR/current" "$STATE_DIR/previous"; fi
  printf '%s\n' "$1" > "$STATE_DIR/current"
}

cd "$ROOT"
case "$CMD" in
  render-env)
    [ -n "$OUT" ] || die "render-env needs --out FILE" 2
    missing=""
    for n in $(names "$HERE/required-env.txt"); do [ -n "$(printenv "$n" || true)" ] || missing="$missing $n"; done
    [ -z "$missing" ] || die "missing or empty GitHub secrets / variables:$missing"
    tmp="$(umask 077; mktemp "$OUT.XXXXXX")"
    for n in $(names "$HERE/required-env.txt") $(names "$HERE/optional-env.txt"); do
      v="$(printenv "$n" || true)"
      [ -z "$v" ] || printf '%s=%s\n' "$n" "$v" >> "$tmp"
    done
    chmod 600 "$tmp"; mv "$tmp" "$OUT"
    echo "wrote $OUT (mode 600, $(wc -l < "$OUT" | tr -d ' ') variables; values not shown)" ;;
  check) preflight ;;
  smoke) preflight; smoke ;;
  up)
    preflight
    checkout_tag "${TAG:-local}"
    bring_up
    smoke || { echo "deploy: smoke FAILED for ${TAG:-local}; roll back with: scripts/deploy/deploy.sh rollback" >&2; exit 1; }
    record "${TAG:-local}" ;;
  rollback)
    preflight
    [ -f "$STATE_DIR/previous" ] || die "no previous tag recorded in $STATE_DIR/previous: nothing to roll back to"
    prev="$(tr -d '[:space:]' < "$STATE_DIR/previous")"
    checkout_tag "$prev"
    step "rollback build and rolling restart (migrations are forward-only, see DEPLOY.md)"
    compose build
    rolling_restart
    smoke
    [ "$PRINT" = 1 ] || { cp "$STATE_DIR/current" "$STATE_DIR/rolled_back_from"; printf '%s\n' "$prev" > "$STATE_DIR/current"; } ;;
  reseed)
    preflight
    step "reseed: wipe and reseed core demo data (audit history is kept)"
    compose run --rm --no-deps -e DEMO_RESEED=true seed_core python -m core.seeds --reseed
    smoke ;;
esac
