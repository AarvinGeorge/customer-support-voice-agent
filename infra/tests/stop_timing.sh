#!/usr/bin/env bash
# What: live check that a stopped agent shuts down on SIGTERM quickly with exit 0 (uvicorn is PID 1).
# Use when: after `docker compose up -d` (set COMPOSE_PROJECT_NAME), and after changing a start command.
# Inputs: running agent_a and core services; optional MAX_SECONDS (default 15).
# Output: PASS or FAIL lines with the measured seconds and exit code; exit 1 on FAIL.
# Side effects: stops agent_a and core, then starts them again.
# Role: guards review finding I1: a start command through `sh -c` without exec swallows SIGTERM, so stop waits the
#       full grace period (60 s) and ends in SIGKILL (exit 137), which would never drain in-flight turns.
set -euo pipefail
cd "$(dirname "$0")/../.."
max="${MAX_SECONDS:-15}"
fails=0

check_stop() { # service
  local svc="$1" start end secs cid code
  cid="$(docker compose ps -a -q "$svc")"
  start=$(date +%s)
  docker compose stop "$svc" >/dev/null
  end=$(date +%s)
  secs=$((end - start))
  code="$(docker inspect -f '{{.State.ExitCode}}' "$cid")"
  if [ "$secs" -lt "$max" ] && [ "$code" -eq 0 ]; then
    echo "PASS $svc stopped in ${secs}s with exit $code (limit ${max}s)"
  else
    echo "FAIL $svc stopped in ${secs}s with exit $code (limit ${max}s, want exit 0)"
    fails=$((fails + 1))
  fi
  docker compose start "$svc" >/dev/null
}

check_stop agent_a
check_stop core
[ "$fails" -eq 0 ] && echo "ALL PASS" || { echo "$fails FAILED"; exit 1; }
