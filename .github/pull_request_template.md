<!-- Title starts with the story id, for example "CS-E-06.S1: ...". No em dashes. -->

## Story

- Notion card:
- Source doc sections:

## What was built (per task)

-

## Test evidence

<!-- Commands and summarized output. -->

## Decisions

-

## Checklist

- [ ] ruff, pyright, pytest and the descriptor check pass on the changed files
- [ ] No secrets in the diff (BRIEF secret scan); no `.env` tracked
- [ ] **Live smoke (agent behaviour):** if this PR touches agent gates, middleware, prompts, tool descriptions, model adapters, `data/models.yaml` or the agent wiring (the `GATED` list in `scripts/check_smoke_summary.py`), run `RUN_REAL=1 scripts/live_smoke.sh` on a pushed commit of this PR and paste its whole summary below, from the `LIVE SMOKE` line to `RESULT: PASS`. The `smoke summary` check fails without it. About 20 real model calls, hard cap 25.

## Live smoke summary

```
(paste here, or write "not required: no gated path changed")
```
