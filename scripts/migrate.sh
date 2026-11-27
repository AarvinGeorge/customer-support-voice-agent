#!/bin/sh
# What: rebuild and rerun the three migrate jobs (core, knowledge, agent) against a stack that already exists.
# Use when: you added or changed a file under services/*/migrations and the stack's volume already has data.
# Inputs: optional compose args after the script name are ignored; honours COMPOSE_PROJECT_NAME.
# Output: each job's log (applied or skip lines); exit non-zero when any job fails.
# Side effects: builds the migrate images; runs one-shot containers (removed after) as each *_owner role.
# Role: deterministic dev command for CS-E-01.S3.T2. `up -d --build` also recreates a migrate job whose image changed
#       (verified on compose 2.37), but that depends on image ids and compose version; this runs every job explicitly.
set -eu
cd "$(dirname "$0")/.."
docker compose build migrate_core migrate_knowledge migrate_agent
for job in migrate_core migrate_knowledge migrate_agent; do
  echo "== $job"
  docker compose run --rm --no-deps "$job"
done
