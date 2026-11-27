#!/usr/bin/env bash
# What: the ONE definition of the CI checks (D-068). Every job of .github/workflows/ci.yml calls this script with its job
#   name, and scripts/ci_gate.sh runs it with no arguments (all jobs) on a PR merged onto the latest main.
# Use when: before pushing (`scripts/ci_local.sh` or `scripts/ci_local.sh python:services/agent web`), from ci_gate.sh,
#   and from ci.yml. `scripts/ci_local.sh --list` prints the job names.
# Inputs: job names (none = all). Env: CI_DESCRIPTORS_BASE (default origin/main; ci.yml sets HEAD^ on a push to main,
#   ci_gate.sh sets the main sha), CI_LOG_DIR (default .tmp/ci-local/<stamp>), CI_PG_PROJECT, CI_PG_SUBNET, CI_PG_PORT
#   (the pg test stack; defaults cs-localci, 172.31.79.0/24, 55479; the project must start with cs-localci).
# Output: one PASS or FAIL row per step, a summary table, and the last line
#   `ci_local: N passed, N failed, N skipped` (skipped is always 0: every step is hard, no skip rule remains).
#   Exit 0 only when no step failed; 1 on any failure; 2 on bad usage.
# Side effects: uv syncs, pnpm install and build in the checkout; the compose job writes .env from .env.example only
#   when no other .env exists and removes it after; pg tests start and remove a throwaway Compose project; logs under
#   CI_LOG_DIR; pinned tools (uv 0.8.22 shims, gitleaks 8.28.0) cached under ~/.cache/quillstack-ci.
# Role: the merge gate's content. Tool versions match ci.yml: uv 0.8.22, ruff 0.9.10, pyright 1.1.405, pytest 8.3.5,
#   node 22, pnpm from package.json, gitleaks 8.28.0 (sha256 pinned).
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

UV_VERSION="0.8.22"
RUFF="ruff@0.9.10"
PYRIGHT="pyright==1.1.405"
PSYCOPG="psycopg[binary]==3.3.9"
PYTEST="pytest==8.3.5"
NODE_MAJOR="22"
GITLEAKS_VERSION="8.28.0"
CACHE="${HOME}/.cache/quillstack-ci"

# Appended to every pytest step: fail on any skipped, xfailed or xpassed test, or on a run that passed nothing.
SKIP_GATE='if grep -qE "[0-9]+ (skipped|xfailed|xpassed)" "$out"; then echo "FAIL: pytest skipped (or xfail) tests: they must run or be deselected by marker"; exit 1; fi
    if ! grep -qE "[0-9]+ passed" "$out"; then echo "FAIL: pytest passed nothing"; exit 1; fi'
PYTHON_PROJECTS=(packages/contracts services/agent services/core services/knowledge scripts evals infra)
OTHER_JOBS=(repo-tests web gitleaks contracts-sync import-guard descriptors compose pg-tests)

STAMP="$(date +%Y%m%dT%H%M%S)"
LOG_DIR="${CI_LOG_DIR:-$ROOT/.tmp/ci-local/$STAMP-$$}"
NAMES=() STATUSES=() SECS=()

# What: list every job name. Use when: --list, usage errors, the no-argument run. Inputs: none. Output: names on stdout.
# Side effects: none. Role: the job catalogue ci.yml must match (scripts/test_ci_local.py checks it).
all_jobs() {
  local p
  for p in "${PYTHON_PROJECTS[@]}"; do echo "python:$p"; done
  printf '%s\n' "${OTHER_JOBS[@]}"
}

# What: run one step in its own `bash -euo pipefail`, record PASS or FAIL with its duration.
# Use when: every check. Inputs: name, working dir, command string. Every step is hard (no soft or skip branches).
# Output: a status line; on FAIL the last 60 log lines (locally; on Actions the full output is already streamed).
# Side effects: writes $LOG_DIR/<name>.log. Role: the only place a step's exit code is read.
run_step() {
  local name="$1" dir="$2" cmd="$3"
  local log="$LOG_DIR/$(echo "$name" | tr -c 'A-Za-z0-9._-' '_').log" start=$SECONDS rc=0 status
  echo "--- $name ($dir)"
  if [ "${GITHUB_ACTIONS:-}" = "true" ]; then
    (cd "$dir" && bash -euo pipefail -c "$cmd") 2>&1 | tee "$log" || rc=$?
  else
    (cd "$dir" && bash -euo pipefail -c "$cmd") >"$log" 2>&1 || rc=$?
  fi
  if [ "$rc" -eq 0 ]; then status=PASS; else status=FAIL; fi
  if [ "$status" = FAIL ] && [ "${GITHUB_ACTIONS:-}" != "true" ]; then
    echo "    FAIL (exit $rc), last lines of $log:"; tail -n 60 "$log" | sed 's/^/    | /'
  fi
  NAMES+=("$name"); STATUSES+=("$status"); SECS+=("$((SECONDS - start))")
  echo "    $status in $((SECONDS - start))s"
}

# What: put uv 0.8.22 (the ci.yml version) first on PATH, via shims to the uv package binary when the local uv differs.
# Use when: before any uv job. Inputs: none. Output: none, or exit 1 naming the problem.
# Side effects: creates $CACHE/bin/uv and uvx symlinks; prepends $CACHE/bin to PATH. Role: version parity with ci.yml.
ensure_uv() {
  if [ "$(uv --version 2>/dev/null | awk '{print $2}')" = "$UV_VERSION" ]; then return 0; fi
  local bin
  bin="$(uvx --from "uv==$UV_VERSION" python -c 'import uv; print(uv.find_uv_bin())')" || { echo "FAIL: cannot resolve uv $UV_VERSION" >&2; exit 1; }
  mkdir -p "$CACHE/bin"
  ln -sf "$bin" "$CACHE/bin/uv"; ln -sf "$(dirname "$bin")/uvx" "$CACHE/bin/uvx"
  export PATH="$CACHE/bin:$PATH"
  [ "$(uv --version | awk '{print $2}')" = "$UV_VERSION" ] || { echo "FAIL: uv on PATH is not $UV_VERSION" >&2; exit 1; }
}

# What: make node $NODE_MAJOR the node on PATH (nvm install when the default differs). Use when: before the web job.
# Inputs: none. Output: none, or exit 1. Side effects: prepends a node bin dir to PATH. Role: version parity with ci.yml.
ensure_node() {
  if [ "$(node --version 2>/dev/null | sed -E 's/^v([0-9]+).*/\1/')" = "$NODE_MAJOR" ]; then return 0; fi
  local d
  d="$(ls -d "$HOME"/.nvm/versions/node/v"$NODE_MAJOR".*/bin 2>/dev/null | sort -V | tail -1 || true)"
  [ -n "$d" ] || { echo "FAIL: node $NODE_MAJOR is required (ci.yml uses it) and was not found" >&2; exit 1; }
  export PATH="$d:$PATH"
  [ "$(node --version | sed -E 's/^v([0-9]+).*/\1/')" = "$NODE_MAJOR" ] || { echo "FAIL: node on PATH is not $NODE_MAJOR" >&2; exit 1; }
}

# What: return the path of the pinned gitleaks binary, downloading and sha256-checking it on first use.
# Use when: the gitleaks job. Inputs: none. Output: the binary path on stdout, or exit 1 on a checksum or platform miss.
# Side effects: writes under $CACHE. Role: the same scanner and version as ci.yml, verified, on Linux and macOS.
ensure_gitleaks() {
  local os arch sum
  case "$(uname -s)-$(uname -m)" in
    Linux-x86_64) os=linux arch=x64 sum=a65b5253807a68ac0cafa4414031fd740aeb55f54fb7e55f386acb52e6a840eb ;;
    Linux-aarch64) os=linux arch=arm64 sum=eff65261156100e5d94a6b3dec313d532fddfe19ae1590bf7a2b4f2699128356 ;;
    Darwin-arm64) os=darwin arch=arm64 sum=d942f3ad147250c9edbaab3fed9e482f98d3b59ba10ae97b8d75647e3ade492c ;;
    Darwin-x86_64) os=darwin arch=x64 sum=edf5a507008b0d2ef4959575772772770586409c1f6f74dabf19cbe7ec341ced ;;
    *) echo "FAIL: no pinned gitleaks for $(uname -s)-$(uname -m)" >&2; exit 1 ;;
  esac
  local dir="$CACHE/gitleaks-$GITLEAKS_VERSION-$os-$arch"
  if [ ! -x "$dir/gitleaks" ]; then
    mkdir -p "$dir"
    local tgz="$dir/gitleaks.tar.gz"
    curl -sSfL -o "$tgz" "https://github.com/gitleaks/gitleaks/releases/download/v$GITLEAKS_VERSION/gitleaks_${GITLEAKS_VERSION}_${os}_${arch}.tar.gz"
    if command -v sha256sum >/dev/null; then got="$(sha256sum "$tgz" | awk '{print $1}')"; else got="$(shasum -a 256 "$tgz" | awk '{print $1}')"; fi
    [ "$got" = "$sum" ] || { rm -f "$tgz"; echo "FAIL: gitleaks checksum mismatch" >&2; exit 1; }
    tar -xzf "$tgz" -C "$dir" gitleaks; rm -f "$tgz"
  fi
  echo "$dir/gitleaks"
}

# What: the python job for one project: sync, ruff, pyright, pytest (ci.yml job "python (<project>)").
# Use when: job `python:<project>`. Inputs: project dir. Output: step rows. Side effects: uv sync in the project.
# Role: per-service lint, types and unit tests. REQUIRE_ARTICLES=1 makes the article tests fail instead of skip.
job_python() {
  local p="$1"
  export REQUIRE_ARTICLES=1
  run_step "python:$p sync" "$p" 'if [ -f pyproject.toml ]; then uv sync --frozen; else echo "no pyproject.toml here (stdlib scripts), nothing to sync"; fi'
  # The root tests/ has no project of its own, so the scripts job lints it too.
  run_step "python:$p ruff" "$p" "uvx $RUFF check .
    if [ '$p' = scripts ]; then uvx $RUFF check ../tests; fi"
  # No pyproject (scripts, infra): infra/migrate.py imports psycopg (the service images carry it), so pyright gets the
  # same psycopg version the services lock.
  run_step "python:$p pyright" "$p" "if [ -f pyproject.toml ]; then uv run --frozen --with $PYRIGHT pyright;
    else uv run --no-project --with '$PSYCOPG' --with $PYRIGHT pyright .; fi"
  # No quiet branches: a project without tests fails, and so does any skipped test (a skip is a green run that tested
  # nothing; same rule as scripts/pg_tests.sh). Deselected markers (pg, real, e2e) are not skips. No -q: some projects
  # set -q in addopts and two -q drop the summary line the checks read.
  run_step "python:$p pytest" "$p" "out=\$(mktemp)
    if [ ! -f pyproject.toml ]; then uv run --no-project --with $PYTEST --with pyyaml pytest -rs . 2>&1 | tee \"\$out\";
    elif [ -d tests ]; then uv run --frozen --with pytest pytest -rs 2>&1 | tee \"\$out\";
    else echo 'no tests/ directory: every python project must have tests'; exit 1; fi
    $SKIP_GATE"
}

# What: the dispatcher from a job name to its steps. Use when: main loop. Inputs: job name. Output: step rows.
# Side effects: those of the job. Role: maps ci.yml job ids to commands (the commands live only here).
run_job() {
  case "$1" in
    python:*)
      local p="${1#python:}" ok=0 q
      for q in "${PYTHON_PROJECTS[@]}"; do [ "$q" = "$p" ] && ok=1; done
      [ "$ok" = 1 ] || { echo "unknown python project: $p" >&2; exit 2; }
      ensure_uv; job_python "$p" ;;
    repo-tests)
      ensure_uv; export REQUIRE_ARTICLES=1
      run_step "repo-tests" . "out=\$(mktemp)
        uv run --no-project --with $PYTEST --with pyyaml --with ./packages/contracts pytest -q -rs tests scripts/test_keys.py 2>&1 | tee \"\$out\"
        $SKIP_GATE" ;;
    web)
      ensure_node; export COREPACK_ENABLE_DOWNLOAD_PROMPT=0
      # `corepack pnpm` instead of ci.yml's old `corepack enable`: same pnpm (package.json packageManager), no global shims.
      run_step "web pnpm version" apps/web "corepack pnpm --version"
      run_step "web install" apps/web "corepack pnpm install --frozen-lockfile"
      run_step "web lint" apps/web "corepack pnpm lint"
      run_step "web vitest" apps/web "corepack pnpm test"
      run_step "web build" apps/web "corepack pnpm build"
      run_step "web typecheck" apps/web "corepack pnpm typecheck"
      run_step "web test:bundle" apps/web "corepack pnpm test:bundle" ;;
    gitleaks)
      local gl; gl="$(ensure_gitleaks)"
      run_step "gitleaks" . "'$gl' detect --source . --redact --verbose --exit-code 1" ;;
    contracts-sync)
      ensure_uv
      run_step "contracts-sync" packages/contracts 'uv sync --frozen
        uv run --frozen python scripts/build_fixtures.py
        uv run --frozen python scripts/gen_ts.py
        git diff --exit-code -- . || { echo "Generated contract files are stale: run build_fixtures.py and gen_ts.py and commit the result"; exit 1; }
        if [ -n "$(git status --porcelain -- .)" ]; then git status --short -- .; echo "Untracked generated files"; exit 1; fi' ;;
    import-guard)
      ensure_uv
      run_step "import-guard" . "uv run --no-project --with $PYTEST pytest -q scripts/test_import_guard.py && python3 scripts/check_import_guard.py" ;;
    descriptors)
      run_step "descriptors (base ${CI_DESCRIPTORS_BASE:-origin/main})" . "python3 scripts/check_descriptors.py --base '${CI_DESCRIPTORS_BASE:-origin/main}'"
      # The full repo is at 0 gaps (backfill, PR #70), so any gap anywhere fails.
      run_step "descriptors --all" . "python3 scripts/check_descriptors.py --all" ;;
    compose)
      run_step "compose config" . 'docker compose --env-file .env.example config -q
        docker compose --env-file .env.example -f compose.yaml config -q
        made=0
        if [ -e .env ] || [ -L .env ]; then
          # Never overwrite a real .env (in a lane worktree it is a symlink to the secrets).
          if [ -L .env ] || ! cmp -s .env .env.example; then echo "FAIL: .env exists and is not a copy of .env.example; refusing to overwrite it. Run in a clean checkout (scripts/ci_gate.sh does)."; exit 1; fi
        else cp .env.example .env; made=1; fi
        rc=0; python3 infra/tests/check_topology.py || rc=$?
        [ "$made" = 1 ] && rm -f .env
        exit "$rc"'
      # The deploy form (D-072): every built service takes a digest-pinned release image and has no build section left.
      run_step "compose release form" . 'd="sha256:$(printf "0%.0s" $(seq 64))"
        AGENT_IMAGE="ghcr.io/x/agent@$d" CORE_IMAGE="ghcr.io/x/core@$d" KNOWLEDGE_IMAGE="ghcr.io/x/knowledge@$d" WEB_IMAGE="ghcr.io/x/web@$d" \
          docker compose --env-file .env.example -f compose.yaml -f scripts/deploy/compose.release.yaml --profile seed --profile jobs config --format json \
          | python3 -c "import json,sys; s=json.load(sys.stdin)[\"services\"]; b=[n for n,v in s.items() if \"build\" in v]; u=[n for n,v in s.items() if \"@sha256:\" not in v.get(\"image\",\"\") and n not in (\"caddy\",\"postgres\",\"redis\")]; assert not b and not u, (b, u); print(\"release form: no build section;\", len(s) - 3, \"services pinned by digest\")"' ;;
    pg-tests)
      ensure_uv
      local proj="${CI_PG_PROJECT:-cs-localci}"
      # pg_tests.sh ends with `down -v --remove-orphans`: only a cs-localci* project may be named, never cs-demo or a lane stack.
      case "$proj" in cs-localci*) ;; *) echo "refusing CI_PG_PROJECT=$proj: it must start with cs-localci (down -v would remove that stack)" >&2; exit 2 ;; esac
      run_step "pg tests" . "COMPOSE_PROJECT_NAME='$proj' EDGE_SUBNET='${CI_PG_SUBNET:-172.31.79.0/24}' PG_TEST_PORT='${CI_PG_PORT:-55479}' scripts/pg_tests.sh"
      run_step "pg deploy restore" . "scripts/deploy/restore_pg_test.sh" ;;
    *) echo "unknown job: $1 (scripts/ci_local.sh --list)" >&2; exit 2 ;;
  esac
}

# What: print the summary table and the one-line totals; write it to the Actions step summary when there.
# Use when: the end of every run. Inputs: the recorded rows. Output: table plus `ci_local: ...` line.
# Side effects: appends to $GITHUB_STEP_SUMMARY on Actions. Role: the evidence block ci_gate.sh quotes.
summary() {
  local i pass=0 fail=0 skip=0 table
  table="$(printf '%-52s %-5s %6s\n' STEP STATUS SECS; for i in ${NAMES[@]+"${!NAMES[@]}"}; do printf '%-52s %-5s %6s\n' "${NAMES[$i]}" "${STATUSES[$i]}" "${SECS[$i]}"; done)"
  for i in ${STATUSES[@]+"${!STATUSES[@]}"}; do
    case "${STATUSES[$i]}" in PASS) pass=$((pass+1)) ;; FAIL) fail=$((fail+1)) ;; SKIP) skip=$((skip+1)) ;; esac
  done
  echo; echo "===== ci_local summary (logs: $LOG_DIR) ====="; echo "$table"
  if [ -n "${GITHUB_STEP_SUMMARY:-}" ]; then printf '```\n%s\n```\n' "$table" >> "$GITHUB_STEP_SUMMARY"; fi
  echo "ci_local: $pass passed, $fail failed, $skip skipped (${SECONDS}s)"
  [ "$fail" -eq 0 ]
}

if [ "${1:-}" = "--list" ]; then all_jobs; exit 0; fi
if [ "${1:-}" = "-h" ] || [ "${1:-}" = "--help" ]; then sed -n '2,20p' "$0"; exit 0; fi
git rev-parse --show-toplevel >/dev/null 2>&1 || { echo "ci_local.sh: not inside a git checkout" >&2; exit 2; }
mkdir -p "$LOG_DIR"
# bash 3.2 (macOS) compatible: no mapfile; job names contain no spaces.
if [ "$#" -eq 0 ]; then JOBS=($(all_jobs)); else JOBS=("$@"); fi
KNOWN=" $(all_jobs | tr '\n' ' ') "
for j in "${JOBS[@]}"; do
  case "$KNOWN" in *" $j "*) ;; *) echo "unknown job: $j (scripts/ci_local.sh --list)" >&2; exit 2 ;; esac
done
for j in "${JOBS[@]}"; do run_job "$j"; done
summary
