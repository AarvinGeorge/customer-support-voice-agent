#!/bin/sh
# What: run only the knowledge Postgres-backed tests (thin wrapper over scripts/pg_tests.sh).
# Use when: you change SQL in services/knowledge and want the quick loop. CI runs scripts/pg_tests.sh (job "pg tests").
# Inputs: same env as pg_tests.sh. Output: pytest result, exit code is pytest's. Side effects: see pg_tests.sh.
# Role: backward-compatible entry point.
PG_SERVICES=knowledge exec "$(dirname "$0")/pg_tests.sh" "$@"
