#!/usr/bin/env bash
# What: Postgres-backed test of deploy.sh backup and restore, including the case that destroyed a database before: the failed release ADDED a table.
# Use when: you change backup or restore in scripts/deploy/deploy.sh, and in CI (job "pg tests"). Needs docker; no provider, no secrets.
# Inputs: none. Output: PASS lines and exit 0, or a FAIL line and exit 1. Side effects: starts a throwaway postgres (project cs-restore-test), removed on exit.
# Role: proves restore is all-or-nothing and that a failed restore brings the apps back: added tables vanish, row counts equal the manifest, a corrupt backup changes nothing, a backup from another release is refused.
set -euo pipefail
cd "$(dirname "$0")/../.."
export COMPOSE_PROJECT_NAME=cs-restore-test DEPLOY_COMPOSE_FILES=scripts/deploy/compose.restore-test.yaml
tmp="$(mktemp -d)"; : > "$tmp/env"; S="$tmp/state"; mkdir -p "$S"
dc() { docker compose --env-file "$tmp/env" -f "$DEPLOY_COMPOSE_FILES" "$@"; }
# shellcheck source=../lib/compose_owner.sh
source scripts/lib/compose_owner.sh
# Tear down only a project this run created (PR #74 N1): an existing cs-restore-test is another run's; refuse, exit 2.
compose_claim "$COMPOSE_PROJECT_NAME" || { rm -rf "$tmp"; echo "restore_pg_test: refusing project $COMPOSE_PROJECT_NAME; nothing started or removed" >&2; exit 2; }
trap 'compose_teardown "$COMPOSE_PROJECT_NAME" dc down -v || true; rm -rf "$tmp"' EXIT
sql() { dc exec -T postgres psql -U postgres -d quillstack -v ON_ERROR_STOP=1 -tAq -c "$1"; }
D() { scripts/deploy/deploy.sh --local --env-file "$tmp/env" --state-dir "$S" "$@"; }
fail() { echo "FAIL: $*" >&2; exit 1; }

dc up -d --wait --wait-timeout 120 postgres agent_a agent_b core knowledge >/dev/null
dc rm -f -s migrate_stub >/dev/null 2>&1 || true   # like the real stack: the job container is gone, so `compose start` of an app would fail
sql "create schema core; create schema knowledge; create schema agent;
     create table core.accounts(id int primary key, name text); insert into core.accounts select g, 'a'||g from generate_series(1,5) g;
     create table knowledge.chunks(id int primary key, body text); insert into knowledge.chunks select g, repeat('x',300) from generate_series(1,40) g;
     create table agent.turns(id int primary key); insert into agent.turns select g from generate_series(1,2) g;"
printf 'v1\n' > "$S/good"; printf 'v1\n' > "$S/rollback_to"

D backup --tag v2 | tail -1
[ "$(sed -n 2p "$S/backup")" = v1 ] && [ "$(sed -n 3p "$S/backup")" = v2 ] || fail "backup record not bound to base v1 and tag v2"
echo "PASS backup recorded for base v1, precedes v2"

# the failed release: adds a table (with a dependency on the schema), adds a column, changes and deletes rows
sql "create table core.rev_extra(id int primary key); insert into core.rev_extra values (1),(2);
     alter table core.accounts add column extra text; delete from core.accounts where id < 3; update knowledge.chunks set body='changed';"
D restore | tail -1
[ "$(sql "select to_regclass('core.rev_extra') is null")" = t ] || fail "the added table survived the restore"
[ "$(sql "select count(*) from core.accounts")" = 5 ] && [ "$(sql "select count(*) from information_schema.columns where table_name='accounts' and column_name='extra'")" = 0 ] || fail "accounts not restored"
[ "$(sql "select count(*) from knowledge.chunks where body='changed'")" = 0 ] || fail "chunk rows not restored"
echo "PASS add-a-table case: table gone, rows and columns back, counts equal the manifest"

# a corrupt backup must change nothing (single transaction)
sql "create table core.rev_extra2(id int); insert into core.rev_extra2 values (1); delete from agent.turns;"
f="$(sed -n 1p "$S/backup")"; printf 'THIS IS NOT SQL;\n' >> "$f"
if D restore >/dev/null 2>&1; then fail "a corrupt backup restored without error"; fi
[ "$(sql "select count(*) from core.rev_extra2")" = 1 ] && [ "$(sql "select count(*) from core.accounts")" = 5 ] || fail "failed restore left a partial database"
[ "$(dc ps --status running -q agent_a agent_b core knowledge | wc -l | tr -d ' ')" = 4 ] || fail "the app services were not running again after the failed restore"
echo "PASS corrupt backup: restore failed, the database is exactly as it was and the 4 app services run again"

# a backup taken on another base is refused
printf 'v9\n' > "$S/rollback_to"
if D restore >/dev/null 2>"$tmp/err"; then fail "restore accepted a backup from another release"; fi
grep -q "another release" "$tmp/err" || fail "refusal message missing"
echo "PASS backup from another release refused"
