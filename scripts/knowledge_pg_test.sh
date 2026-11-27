#!/bin/sh
# What: run the Postgres-backed knowledge tests (pytest marker `pg`) against a throwaway Compose stack.
# Use when: you change SQL in services/knowledge (db.py, ingest/store.py, migrations). CI runs only `-m "not pg"`.
# Inputs: optional env COMPOSE_PROJECT_NAME (default cs-pgtest), EDGE_SUBNET (default 172.31.77.0/24), PG_TEST_PORT
#         (default 55432), PYTEST_ARGS (default "-m pg"). Uses NO real keys: it writes a temporary env file of random
#         dummy secrets, and the tests embed with fixture vectors (zero OpenAI calls).
# Output: pytest result; the stack and its volume are removed on exit (`down -v`). Exit code is pytest's.
# Side effects: builds and starts postgres plus the core and knowledge migrate jobs; one loopback port.
# Role: the only automated check of the tier-filter SQL, the HNSW iterative scan and the corpus_version reader.
set -eu
cd "$(dirname "$0")/.."
export COMPOSE_PROJECT_NAME="${COMPOSE_PROJECT_NAME:-cs-pgtest}"
export EDGE_SUBNET="${EDGE_SUBNET:-172.31.77.0/24}"
export PG_TEST_PORT="${PG_TEST_PORT:-55432}"
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
dc run --rm migrate_knowledge
app_pw="$(sed -n 's/^KNOWLEDGE_APP_PASSWORD=//p' "$envfile")"
export KNOWLEDGE_PG_TEST_URL="postgresql://knowledge_app:${app_pw}@127.0.0.1:${PG_TEST_PORT}/quillstack"
cd services/knowledge
uv run --frozen --with pytest pytest -q ${PYTEST_ARGS:--m pg}
