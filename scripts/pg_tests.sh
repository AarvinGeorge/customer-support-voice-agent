#!/usr/bin/env bash
# What: run every Postgres-backed test suite (pytest marker `pg`) against one throwaway Compose stack.
# Use when: you change SQL, grants, indexes or migrations (services/core, services/knowledge), and in CI (job "pg tests").
# Inputs: optional env COMPOSE_PROJECT_NAME (default cs-pgtest), EDGE_SUBNET (default 172.31.77.0/24), PG_TEST_PORT
#         (default 55432), PG_SERVICES (default "core knowledge"; any subset). Uses NO real keys: it writes a temporary
#         env file of random dummy secrets, and the tests embed with fixture vectors (zero provider calls).
# Output: pytest results per service; exit code is non-zero if any suite fails, errors, or skips a test (a skipped DB
#         test is a green run that tested nothing). The stack and its volume are removed on exit (`down -v`).
# Side effects: builds and starts postgres plus the migrate jobs; one loopback port.
# Role: the only automated check of Postgres-only behaviour (array casts, grants, unique indexes, HNSW scan).
set -euo pipefail
cd "$(dirname "$0")/.."
export COMPOSE_PROJECT_NAME="${COMPOSE_PROJECT_NAME:-cs-pgtest}"
export EDGE_SUBNET="${EDGE_SUBNET:-172.31.77.0/24}"
export PG_TEST_PORT="${PG_TEST_PORT:-55432}"
services="${PG_SERVICES:-core knowledge}"
tmp="$(mktemp -d)"
envfile="$tmp/env"
python3 - "$envfile" "$EDGE_SUBNET" <<'PY'
import re, secrets, sys
out, subnet = sys.argv[1], sys.argv[2]
vals = {}
for line in open(".env.example"):
    m = re.match(r"([A-Z0-9_]+)=(.*)$", line.strip())
    if m:
        vals[m.group(1)] = m.group(2)
for k, v in list(vals.items()):
    if not v and (k.endswith(("_PASSWORD", "_KEY", "_PEPPER", "_CODE")) or k.startswith(("CORE_TOKEN", "KNOWLEDGE_TOKEN", "OPERATOR_TOKEN"))):
        vals[k] = secrets.token_hex(16)
vals["OPENAI_API_KEY"] = "dummy-not-used"
vals["EDGE_SUBNET"] = subnet
vals["FORWARDED_ALLOW_IPS"] = subnet
for svc in ("agent", "core", "knowledge"):
    for role, suffix in (("owner", "_DB_OWNER_URL"), ("app", "_DB_URL")):
        pw = vals[f"{svc.upper()}_{role.upper()}_PASSWORD"]
        vals[f"{svc.upper()}{suffix}"] = f"postgresql://{svc}_{role}:{pw}@postgres:5432/quillstack"
open(out, "w").write("".join(f"{k}={v}\n" for k, v in vals.items()))
PY
chmod 600 "$envfile"
dc() { docker compose --env-file "$envfile" -f compose.yaml -f infra/compose.pgtest.yaml "$@"; }
cleanup() { dc down -v --remove-orphans >/dev/null 2>&1 || true; rm -rf "$tmp"; }
trap cleanup EXIT
dc up -d --wait postgres
dc run --rm migrate_knowledge   # depends on migrate_core, so core and knowledge schemas are both built
envval() { sed -n "s/^$1=//p" "$envfile"; }
status=0
for svc in $services; do
  echo "=== pg tests: services/$svc ==="
  case "$svc" in
    core)
      export CORE_DB_OWNER_URL="$(envval CORE_DB_OWNER_URL)" CORE_DB_URL="$(envval CORE_DB_URL)" ;;
    knowledge)
      export KNOWLEDGE_PG_TEST_URL="postgresql://knowledge_app:$(envval KNOWLEDGE_APP_PASSWORD)@127.0.0.1:${PG_TEST_PORT}/quillstack" ;;
    *) echo "unknown service $svc" >&2; exit 2 ;;
  esac
  out="$tmp/$svc.out"
  (cd "services/$svc" && uv run --frozen --with pytest pytest -m pg -rs) 2>&1 | tee "$out" || status=1
  if grep -qE '[0-9]+ skipped' "$out"; then echo "FAIL: $svc pg tests skipped (must run)" >&2; status=1; fi
  if ! grep -qE '[0-9]+ passed' "$out"; then echo "FAIL: $svc pg tests passed nothing" >&2; status=1; fi
done
exit "$status"
