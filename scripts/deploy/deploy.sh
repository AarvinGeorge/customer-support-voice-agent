#!/usr/bin/env bash
# What: deploy, roll back, reseed or smoke-test the Quillstack Compose stack on the VM (or a laptop with --local).
# Use when: the owner or .github/workflows/deploy.yml ships a tag. Run it ON the VM from the repo checkout (see scripts/deploy/DEPLOY.md).
# Inputs: a command (up, rollback, reseed, smoke, check, render-env) and options (--tag, --env-file, --local, --print,
#   --state-dir, --out, --restore-db). Variables come from the env file (the shell wins on a clash, as in Compose); names are in
#   scripts/deploy/required-env.txt. Output: one line per step on stdout; errors on stderr; exit 0 only when every step passed.
# Side effects: git checkout of the tag; docker builds, one-shot jobs and container restarts; a pg_dump backup under <state-dir>/backups;
#   state files in <state-dir>: good (last tag with a green smoke), rollback_to (what `rollback` restores), pending (an `up` that
#   has not finished), last_backup; `reseed` wipes the demo data. Never prints a secret value. --print runs nothing.
# Role: CS-E-08.S4.T2 (doc 09 s5, doc 06 s10, doc 07 s15.2; D-029, D-047, D-049): one ordered, repeatable deploy.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
HERE="$ROOT/scripts/deploy"
ENV_FILE=".env"
STATE_DIR="$ROOT/.deploy"
TAG=""
LOCAL=0
PRINT=0
RESTORE_DB=0
OUT=""
CMD=""

die() { echo "deploy: $*" >&2; exit "${2:-1}"; }
usage() {
  cat <<'USAGE'
usage: scripts/deploy/deploy.sh [options] <command>
commands:
  up --tag TAG    checkout TAG, build, migrate, seed (first time only), ingest, rolling restart, smoke
  rollback        restore the last GOOD tag (green smoke): after a failed `up` that is the tag that was running before it;
                  --restore-db also restores the pre-migrate backup taken by that `up`
  reseed          wipe and reseed the demo data (run before the Loom take), then smoke
  smoke           read /readyz from core, knowledge and the edge, then one chat turn
  check           validate the env file only
  backup          take and verify the pre-migrate backup now (normally part of `up`)
  restore         restore the recorded backup onto the rollback tag's state (normally `rollback --restore-db`)
  render-env      write an env file from the process environment (GitHub secrets); needs --out
options: --env-file FILE (default .env)  --local (laptop dry-run: no git, local overlay)  --print (show commands, run none)
         --state-dir DIR  --out FILE  --restore-db (rollback only)
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
    --restore-db) RESTORE_DB=1; shift ;;
    -h|--help) usage; exit 0 ;;
    -*) usage >&2; die "unknown option: $1" 2 ;;
    *) [ -z "$CMD" ] || die "unexpected argument: $1" 2; CMD="$1"; shift ;;
  esac
done
[ -n "$CMD" ] || { usage >&2; die "usage: a command is required" 2; }
case "$CMD" in up|rollback|reseed|smoke|check|render-env|backup|restore) ;; *) usage >&2; die "unknown command: $CMD" 2 ;; esac
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
    for n in DEMO_FAULTS_ENABLED FEATURE_A4; do
      case "$(printenv "$n" || true)" in true|false) ;; *) die "$n must be set to true or false in the env file (behaviour flag, no silent default on the VM)" ;; esac
    done
    [ "$SITE_ORIGIN" = "https://$SITE_ADDRESS" ] || die "SITE_ORIGIN ($SITE_ORIGIN) must be https://$SITE_ADDRESS"
    if [ -n "$(find "$ENV_FILE" \( -perm -g+r -o -perm -o+r \) -print 2>/dev/null)" ]; then
      die "$ENV_FILE is readable by group or others: chmod 600 $ENV_FILE"
    fi
  fi
  echo "flags: DEMO_FAULTS_ENABLED=${DEMO_FAULTS_ENABLED:-true(default)} FEATURE_A4=${FEATURE_A4:-false(default)}"
  echo "preflight ok (env file $ENV_FILE; secrets present: $(names "$HERE/required-env.txt" | wc -l | tr -d ' '))"
}

COMPOSE_FILES=(-f compose.yaml)
[ "$LOCAL" = 0 ] || COMPOSE_FILES+=(-f scripts/deploy/compose.local.yaml)
# DEPLOY_COMPOSE_FILES (space separated) replaces the list: only scripts/deploy/restore_pg_test.sh sets it, to a postgres-only stack.
if [ -n "${DEPLOY_COMPOSE_FILES:-}" ]; then COMPOSE_FILES=(); for f in $DEPLOY_COMPOSE_FILES; do COMPOSE_FILES+=(-f "$f"); done; fi
compose() { run docker compose --env-file "$ENV_FILE" "${COMPOSE_FILES[@]}" "$@"; }
# dc: a read-only compose call whose output the script uses (not echoed).
dc() { docker compose --env-file "$ENV_FILE" "${COMPOSE_FILES[@]}" "$@"; }

smoke_base() { if [ "$LOCAL" = 1 ]; then echo "http://127.0.0.1:${EDGE_PORT:-8088}"; else echo "${SITE_ORIGIN}"; fi; }

sha12() { if command -v sha256sum >/dev/null 2>&1; then sha256sum "$1"; else shasum -a 256 "$1"; fi | cut -c1-12; }

# expected_policy / expected_embedding: what the checked-out tag says /readyz must report.
expected_policy() { echo "$(sed -n 's/^version_label: *"\{0,1\}\([^"]*\)"\{0,1\} *$/\1/p' "$ROOT/data/policy.yaml" | head -n1)+$(sha12 "$ROOT/data/policy.yaml")"; }
expected_embedding() { awk '/^ *embeddings:/{f=1} f&&/^ *model:/{print $2; exit}' "$ROOT/data/models.yaml"; }

# has BODY NEEDLE: compact BODY (spaces removed) contains NEEDLE literally.
has() { printf '%s' "$1" | tr -d ' ' | grep -qF -- "$2"; }
# need LABEL BODY NEEDLE...: fail loudly naming the label and the missing needle (never the secrets: bodies carry none).
need() { local label="$1" body="$2" n; shift 2; for n in "$@"; do has "$body" "$n" || { echo "deploy: $label read-back is missing $n; body: $body" >&2; return 1; }; done; }

# probe SERVICE PATH: GET a backend readyz from a throwaway container of the release image (never exec into a live one).
# Prints "<http status> <body>" even for a 503, so the cause is visible.
probe() {
  local svc="$1" path="$2"
  dc run --rm --no-deps -T "$svc" python -c "import urllib.request,urllib.error
try:
    r=urllib.request.urlopen('http://$svc:8000$path',timeout=5); print(r.status, r.read().decode())
except urllib.error.HTTPError as e:
    print(e.code, e.read().decode())"
}

# revision_readback: each app container runs the image that was just built, and the checkout is the tag.
revision_readback() {
  local svc cid img built name
  [ "$LOCAL" = 1 ] || echo "revision: $(git -C "$ROOT" describe --tags --exact-match 2>/dev/null || echo untagged) $(git -C "$ROOT" rev-parse --short HEAD)"
  for svc in core knowledge agent_a agent_b; do
    cid="$(docker compose --env-file "$ENV_FILE" "${COMPOSE_FILES[@]}" ps -q "$svc")"
    [ -n "$cid" ] || { echo "deploy: $svc has no running container" >&2; return 1; }
    img="$(docker inspect -f '{{.Image}}' "$cid")"
    name="$(docker inspect -f '{{.Config.Image}}' "$cid")"
    built="$(docker image inspect -f '{{.Id}}' "$name")"
    [ "$img" = "$built" ] || { echo "deploy: $svc runs an older image than the one built ($name)" >&2; return 1; }
  done
  echo "revision: core knowledge agent_a agent_b run the freshly built images"
}

smoke() {
  step "SMOKE readyz versions, revision, in-flight and one chat turn"
  [ "$PRINT" = 0 ] || { echo "+ (smoke compares contract, policy, embedding model and flags with the checkout, then access, session, one turn)"; return 0; }
  local cv pv em df core know edge base out code chunks
  cv="$(tr -d '[:space:]' < "$ROOT/packages/contracts/VERSION")"; pv="$(expected_policy)"; em="$(expected_embedding)"
  df="$(printf '%s' "${DEMO_FAULTS_ENABLED:-true}" | tr '[:upper:]' '[:lower:]')"
  [ -n "$pv" ] && [ -n "$em" ] || die "could not compute the expected policy version or embedding model from data/"
  echo "expected: contract=$cv policy=$pv embedding=$em demo_faults=$df"
  revision_readback || return 1
  out="$(probe core /readyz)" || die "core /readyz probe failed to run"; code="${out%% *}"; core="${out#* }"
  echo "core:      $code $core"
  [ "$code" = 200 ] || die "core /readyz returned $code"
  need core "$core" '"status":"ready"' "\"contract_version\":\"$cv\"" "\"policy_version\":\"$pv\"" "\"demo_faults\":$df" '"seed_date"' || exit 1
  out="$(probe knowledge /readyz)" || die "knowledge /readyz probe failed to run"; code="${out%% *}"; know="${out#* }"
  echo "knowledge: $code $know"
  [ "$code" = 200 ] || die "knowledge /readyz returned $code"
  need knowledge "$know" '"status":"ready"' "\"embedding_model\":\"$em\"" '"calibration_status"' || exit 1
  chunks="$(printf '%s' "$know" | tr -d ' ' | sed -n 's/.*"chunks":\([0-9]*\).*/\1/p')"
  [ "${chunks:-0}" -ge 1 ] || die "knowledge reports no chunks"
  base="$(smoke_base)"
  edge="$(curl -fsS --max-time 10 "$base/api/readyz")" || die "edge $base/api/readyz failed"
  echo "edge:      $edge"
  need edge "$edge" '"status":"ready"' "\"contract_version\":\"$cv\"" '"redis":"ok"' '"postgres":"ok"' '"core":"ok"' '"knowledge":"ok"' "\"demo_faults\":$df" || exit 1
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

PSQL=(postgres psql -U postgres -v ON_ERROR_STOP=1 -tA)

SCHEMAS="core knowledge agent"
SCHEMA_IN="'core','knowledge','agent'"
# tables_exist: 0 when the service schemas already hold tables (a re-deploy), 1 on a fresh database.
tables_exist() {
  [ "$PRINT" = 0 ] || return 0
  [ "$(dc exec -T "${PSQL[@]}" -d quillstack -c "select count(*) from information_schema.tables where table_schema in ($SCHEMA_IN)")" -gt 0 ]
}
table_count() { dc exec -T "${PSQL[@]}" -d "$1" -c "select count(*) from information_schema.tables where table_schema in ($SCHEMA_IN)"; }
# counts DB: "schema.table rows" per base table, sorted. The manifest of a backup and the check after a restore.
counts() {
  dc exec -T "${PSQL[@]}" -d "$1" -c "select format('%s.%s', table_schema, table_name) || ' ' || (xpath('/row/c/text()', query_to_xml(format('select count(*) as c from %I.%I', table_schema, table_name), false, true, '')))[1]::text from information_schema.tables where table_schema in ($SCHEMA_IN) and table_type='BASE TABLE' order by 1"
}

# Backup record (<state-dir>/backup, three lines): file, BASE (the good tag the dump was taken on top of, or none), TAG (the release it precedes).
rec_get() { sed -n "$1p" "$STATE_DIR/backup" 2>/dev/null || true; }

# backup TAG: pg_dump of the three service schemas before any migration, read back: size, marker, a restore into a scratch DB,
# table count equal to live, and a manifest of row counts written beside the dump (taken from the scratch restore, so it describes the file).
backup() {
  step "backup before migrate (pg_dump of core, knowledge, agent; size, marker, scratch restore and row-count manifest)"
  if [ "$PRINT" = 1 ]; then echo "+ pg_dump quillstack to $STATE_DIR/backups/pre-$1-<time>.sql, restore into scratch db quillstack_restore_check, write <file>.manifest, record base good tag"; return 0; fi
  if ! tables_exist; then echo "backup: fresh database (no service tables yet), nothing to back up"; return 0; fi
  local base; base="$(st_get good)"; base="${base:-none}"
  if [ -f "$STATE_DIR/backup" ] && [ "$(rec_get 2)" = "$base" ] && [ -f "$(rec_get 1)" ]; then
    echo "backup: keeping the backup of the first attempt on top of $base: $(rec_get 1) (a retry must not overwrite the good state)"; return 0
  fi
  mkdir -p "$STATE_DIR/backups"; chmod 700 "$STATE_DIR/backups"
  local f="$STATE_DIR/backups/pre-$1-$(date -u +%Y%m%dT%H%M%SZ).sql" want got size nargs=()
  for n in $SCHEMAS; do nargs+=(-n "$n"); done
  ( umask 077; dc exec -T postgres pg_dump -U postgres --clean --if-exists "${nargs[@]}" quillstack > "$f" ) || die "pg_dump failed"
  size="$(wc -c < "$f" | tr -d ' ')"
  [ "$size" -gt 1000 ] || die "backup is only $size bytes: refusing to continue"
  grep -q 'PostgreSQL database dump complete' "$f" || die "backup has no completion marker: refusing to continue"
  dc exec -T "${PSQL[@]}" -d postgres -c "drop database if exists quillstack_restore_check" >/dev/null
  dc exec -T "${PSQL[@]}" -d postgres -c "create database quillstack_restore_check" >/dev/null
  dc exec -T "${PSQL[@]}" -d quillstack_restore_check -c "create extension if not exists vector" >/dev/null
  dc exec -T "${PSQL[@]}" -d quillstack_restore_check < "$f" >/dev/null || die "restore test into the scratch database failed"
  want="$(table_count quillstack)"; got="$(table_count quillstack_restore_check)"
  ( umask 077; counts quillstack_restore_check > "$f.manifest" ) || die "could not write the manifest"
  dc exec -T "${PSQL[@]}" -d postgres -c "drop database quillstack_restore_check" >/dev/null
  [ "$want" = "$got" ] || die "restore test table count $got differs from live $want"
  [ "$(wc -l < "$f.manifest" | tr -d ' ')" = "$got" ] || die "manifest does not list every table"
  printf '%s\n%s\n%s\n' "$f" "$base" "$1" > "$STATE_DIR/backup"
  echo "backup ok: $f ($size bytes, $got tables restored into a scratch db and dropped; manifest $f.manifest; base $base, precedes $1)"
}

# restore_backup EXPECT_TAG: put the database back to the backup taken on top of EXPECT_TAG (the last good tag), all or nothing.
# The three service schemas are dropped (CASCADE, so objects a failed release added vanish) and the dump replayed in ONE transaction:
# any failure rolls back and leaves the database exactly as it was. Then every table's row count is compared with the manifest.
restore_backup() {
  local want_base="$1" f base
  [ -f "$STATE_DIR/backup" ] || die "--restore-db: no backup recorded in $STATE_DIR/backup"
  f="$(rec_get 1)"; base="$(rec_get 2)"
  [ -f "$f" ] && [ -f "$f.manifest" ] || die "--restore-db: backup file or manifest is missing: $f"
  [ "$base" = "$want_base" ] || die "--restore-db: the recorded backup was taken on top of '$base', not on the rollback tag '$want_base'; refusing to restore a backup from another release"
  step "restore database from $f (app services stopped first; one transaction; manifest check after)"
  compose stop agent_a agent_b core knowledge
  if [ "$PRINT" = 1 ]; then echo "+ psql --single-transaction: drop schemas $SCHEMAS cascade, replay $f, then compare counts with $f.manifest"; return 0; fi
  local drops="" n
  for n in $SCHEMAS; do drops="$drops drop schema if exists $n cascade;"; done
  if ! { echo "$drops"; cat "$f"; } | dc exec -T postgres psql -U postgres -d quillstack -v ON_ERROR_STOP=1 --single-transaction -q >/dev/null; then
    echo "deploy: restore failed and was rolled back: the database is unchanged. Starting the app services again (same form as the rolling restart)." >&2
    # `compose start` would refuse (the migrate job containers do not exist: they are run with `run --rm`); `up -d --no-deps` is the form that works.
    if dc up -d --no-deps --wait --wait-timeout 180 core knowledge agent_a agent_b >&2; then
      echo "deploy: app services are running again (still the failed release's images; run rollback without --restore-db to switch code)." >&2
    else
      die "database restore failed AND the app services could NOT be restarted: the site is down; run: docker compose -f compose.yaml up -d --no-deps core knowledge agent_a agent_b (backup kept at $f)"
    fi
    die "database restore failed; the backup file is kept at $f"
  fi
  if [ "$(counts quillstack)" != "$(cat "$f.manifest")" ]; then
    die "restore finished but the row counts differ from the manifest $f.manifest: do not start the app; investigate (the database is the backup replay, not a partial state)"
  fi
  echo "restore ok: $(wc -l < "$f.manifest" | tr -d ' ') tables, every row count equals the manifest"
}

migrate_and_seed() {
  step "migrate_core migrate_knowledge migrate_agent (owner roles, order core, knowledge, agent)"
  for job in migrate_core migrate_knowledge migrate_agent; do compose run --rm --no-deps "$job"; done
  step "seed_core (seeds an empty schema; an existing demo is left untouched)"
  compose run --rm --no-deps seed_core
  step "ingest_knowledge (re-run safe: unchanged articles cost no embedding call)"
  compose run --rm --no-deps ingest_knowledge
}

# inflight_gate: no live turn may be open when the switch starts. Waits up to INFLIGHT_WAIT_S (default 90), then aborts.
inflight_gate() {
  step "in-flight check before the switch (agent.turns running and not past deadline)"
  if [ "$PRINT" = 1 ]; then echo "+ psql: count running turns; wait up to \${INFLIGHT_WAIT_S:-90} s, abort if any remain"; return 0; fi
  local waited=0 max="${INFLIGHT_WAIT_S:-90}" n reg
  while :; do
    reg="$(dc exec -T "${PSQL[@]}" -d quillstack -c "select to_regclass('agent.turns') is not null")" || die "in-flight check could not read the database: not switching"
    if [ "$reg" = t ]; then
      n="$(dc exec -T "${PSQL[@]}" -d quillstack -c "select count(*) from agent.turns where status='running' and deadline_at > now()")" || die "in-flight count failed: not switching"
    else n=0; fi
    echo "in-flight turns: $n (running tag: $(cat "$STATE_DIR/good" 2>/dev/null || echo none))"
    [ "$n" = 0 ] && return 0
    [ "$waited" -lt "$max" ] || die "$n turn(s) still running after ${max}s: not switching. Retry later (no deploy during a Loom take)."
    sleep 5; waited=$((waited + 5))
  done
}

rolling_restart() {
  step "rolling restart: core and knowledge, then agent_a, then agent_b, then web, then caddy (recreated so Caddyfile changes apply)"
  wait_note
  compose up -d --no-deps --wait --wait-timeout 180 --force-recreate core knowledge
  compose up -d --no-deps --wait --wait-timeout 180 --force-recreate agent_a
  compose up -d --no-deps --wait --wait-timeout 180 --force-recreate agent_b
  compose up -d --no-deps --wait --wait-timeout 180 web
  compose up -d --no-deps --wait --wait-timeout 180 --force-recreate caddy
}

bring_up() {
  step "build images on this host (including the seed and ingest job images)"
  compose --profile seed --profile jobs build
  step "postgres and redis"
  compose up -d --no-deps --wait --wait-timeout 180 postgres redis
  backup "${TAG:-local}"
  migrate_and_seed
  inflight_gate
  rolling_restart
}

# State: good = last tag with a green smoke. rollback_to = what `rollback` restores. pending = an `up` that has not finished.
st_get() { cat "$STATE_DIR/$1" 2>/dev/null | tr -d '[:space:]' || true; }

begin_up() { # begin_up TAG: save the rollback point BEFORE any change
  [ "$PRINT" = 0 ] || { echo "+ (save rollback point: current good tag from $STATE_DIR/good; mark $1 pending)"; return 0; }
  mkdir -p "$STATE_DIR"
  local g; g="$(st_get good)"
  if [ -n "$g" ]; then printf '%s\n' "$g" > "$STATE_DIR/rollback_to"; else rm -f "$STATE_DIR/rollback_to"; fi
  # A backup record survives only for a retry on top of the same good tag (the first attempt's backup is the good state).
  if ! { [ -f "$STATE_DIR/pending" ] && [ -f "$STATE_DIR/backup" ] && [ "$(rec_get 2)" = "${g:-none}" ] && [ -f "$(rec_get 1)" ]; }; then rm -f "$STATE_DIR/backup"; fi
  printf '%s\n' "$1" > "$STATE_DIR/pending"
  echo "rollback point: ${g:-none (first deploy)}"
}

mark_good() { # mark_good TAG: only after a green smoke
  [ "$PRINT" = 0 ] || { echo "+ (mark $1 good; the old good tag becomes the rollback point)"; return 0; }
  local g; g="$(st_get good)"
  if [ -n "$g" ] && [ "$g" != "$1" ]; then printf '%s\n' "$g" > "$STATE_DIR/rollback_to"; fi
  printf '%s\n' "$1" > "$STATE_DIR/good"
  rm -f "$STATE_DIR/pending"
}

cd "$ROOT"
case "$CMD" in
  render-env)
    [ -n "$OUT" ] || die "render-env needs --out FILE" 2
    missing=""
    for n in $(names "$HERE/required-env.txt"); do [ -n "$(printenv "$n" || true)" ] || missing="$missing $n"; done
    [ -z "$missing" ] || die "missing or empty GitHub secrets / variables:$missing"
    bad=""
    for n in $(names "$HERE/required-env.txt") $(names "$HERE/optional-env.txt"); do
      v="$(printenv "$n" || true)"
      case "$v" in *"'"*|*$'\n'*) bad="$bad $n" ;; esac
    done
    [ -z "$bad" ] || die "values with a single quote or newline are not allowed (Compose env-file parsing):$bad"
    tmp="$(umask 077; mktemp "$OUT.XXXXXX")"
    for n in $(names "$HERE/required-env.txt") $(names "$HERE/optional-env.txt"); do
      v="$(printenv "$n" || true)"
      [ -z "$v" ] || printf "%s='%s'\n" "$n" "$v" >> "$tmp"
    done
    chmod 600 "$tmp"; mv "$tmp" "$OUT"
    echo "wrote $OUT (mode 600, $(wc -l < "$OUT" | tr -d ' ') variables, single-quoted; values not shown)" ;;
  backup) backup "${TAG:-manual}" ;;
  restore) [ -f "$STATE_DIR/rollback_to" ] || die "no rollback tag recorded in $STATE_DIR/rollback_to"; restore_backup "$(st_get rollback_to)" ;;
  check) preflight ;;
  smoke) preflight; if ! ( smoke ); then die "smoke FAILED"; fi ;;
  up)
    preflight
    begin_up "${TAG:-local}"
    checkout_tag "${TAG:-local}"
    bring_up
    if ! ( smoke ); then
      echo "deploy: smoke FAILED for ${TAG:-local}. Roll back: scripts/deploy/deploy.sh rollback (add --restore-db to also restore the pre-migrate backup)" >&2
      exit 1
    fi
    mark_good "${TAG:-local}" ;;
  rollback)
    preflight
    [ -f "$STATE_DIR/rollback_to" ] || die "no rollback tag recorded in $STATE_DIR/rollback_to (the first deploy has none): nothing to roll back to"
    prev="$(st_get rollback_to)"
    echo "rolling back to the last good tag: $prev (pending: $(st_get pending || true))"
    checkout_tag "$prev"
    step "rollback build"
    compose --profile seed --profile jobs build
    [ "$RESTORE_DB" = 0 ] || restore_backup "$prev"
    rolling_restart
    if ! ( smoke ); then die "smoke FAILED after rollback to $prev"; fi
    if [ "$PRINT" = 0 ]; then
      failed="$(st_get pending)"; [ -z "$failed" ] || printf '%s\n' "$failed" > "$STATE_DIR/rolled_back_from"
      printf '%s\n' "$prev" > "$STATE_DIR/good"; rm -f "$STATE_DIR/pending"
    fi ;;
  reseed)
    preflight
    step "reseed: wipe and reseed core demo data (audit history is kept)"
    compose --profile seed build seed_core
    compose run --rm --no-deps -e DEMO_RESEED=true seed_core python -m core.seeds --reseed
    if ! ( smoke ); then die "smoke FAILED after reseed"; fi ;;
esac
