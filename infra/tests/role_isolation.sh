#!/usr/bin/env bash
# What: live check of Postgres schema and role isolation against the running Compose stack.
# Use when: after `docker compose up -d` (set COMPOSE_PROJECT_NAME), and after changing the init script or a migration.
# Inputs: ../../.env (sourced, never printed); a running postgres service; the six role passwords.
# Output: PASS or FAIL lines; exit 1 on any FAIL. A positive control proves the denied checks could succeed.
# Side effects: creates and drops one probe table per schema (as each owner role).
# Role: proves doc 07 s12: each *_app role touches only its own schema, a core_app SELECT on knowledge.chunks fails.
set -euo pipefail
cd "$(dirname "$0")/../.."
set -a; . ./.env; set +a
fails=0
pass() { echo "PASS $1"; }
fail() { echo "FAIL $1"; fails=$((fails + 1)); }

# run SQL as a role over TCP (hostname postgres, so scram password auth is exercised, not local trust)
sql() { # role password statement -> stdout+stderr, exit status of psql
  docker compose exec -T -e PGPASSWORD="$2" postgres psql -h postgres -U "$1" -d quillstack -v ON_ERROR_STOP=1 -tAc "$3" 2>&1
}
upper() { echo "$1" | tr '[:lower:]' '[:upper:]'; }
pw() { local v; v="$(upper "$1")_$(upper "$2")_PASSWORD"; echo "${!v}"; }

cleanup() {
  for s in agent core knowledge; do sql "${s}_owner" "$(pw "$s" owner)" "DROP TABLE IF EXISTS $s.isolation_probe" >/dev/null || true; done
}
trap cleanup EXIT

for s in agent core knowledge; do
  out=$(sql "${s}_owner" "$(pw "$s" owner)" "CREATE TABLE IF NOT EXISTS $s.isolation_probe (id int); INSERT INTO $s.isolation_probe VALUES (1)") \
    && pass "${s}_owner can create in schema $s" || fail "${s}_owner create in $s: $out"
done

for a in agent core knowledge; do
  for s in agent core knowledge; do
    out=$(sql "${a}_app" "$(pw "$a" app)" "SELECT count(*) FROM $s.isolation_probe") && rc=0 || rc=$?
    if [ "$a" = "$s" ]; then
      [ "$rc" -eq 0 ] && [ "$out" = "1" ] && pass "${a}_app can SELECT $s.isolation_probe (positive control)" || fail "${a}_app SELECT own schema: $out"
    else
      [ "$rc" -ne 0 ] && grep -q "permission denied" <<<"$out" && pass "${a}_app cannot SELECT $s.isolation_probe ($out)" || fail "${a}_app SELECT $s.isolation_probe was not denied: $out"
    fi
  done
done

out=$(sql core_app "$CORE_APP_PASSWORD" "SELECT * FROM knowledge.chunks") && rc=0 || rc=$?
[ "$rc" -ne 0 ] && grep -q "permission denied" <<<"$out" && pass "core_app cannot SELECT knowledge.chunks ($out)" || fail "core_app SELECT knowledge.chunks was not denied: $out"

for a in agent core knowledge; do
  out=$(sql "${a}_app" "$(pw "$a" app)" "CREATE TABLE $a.should_fail (id int)") && rc=0 || rc=$?
  [ "$rc" -ne 0 ] && grep -q "permission denied" <<<"$out" && pass "${a}_app cannot run DDL in its own schema" || fail "${a}_app DDL was not denied: $out"
  out=$(sql "${a}_app" "$(pw "$a" app)" "CREATE TABLE public.should_fail (id int)") && rc=0 || rc=$?
  [ "$rc" -ne 0 ] && grep -q "permission denied" <<<"$out" && pass "${a}_app cannot create in public" || fail "${a}_app create in public was not denied: $out"
  out=$(sql "${a}_owner" "$(pw "$a" owner)" "SELECT count(*) FROM $a.schema_migrations") && rc=0 || rc=$?
  [ "$rc" -eq 0 ] && pass "migrate job ran for $a ($a.schema_migrations exists)" || fail "no $a.schema_migrations: $out"
done

out=$(sql postgres "$POSTGRES_PASSWORD" "SELECT extname FROM pg_extension WHERE extname='vector'")
[ "$out" = "vector" ] && pass "vector extension installed" || fail "vector extension missing: $out"

[ "$fails" -eq 0 ] && echo "ALL PASS" || { echo "$fails FAILED"; exit 1; }
