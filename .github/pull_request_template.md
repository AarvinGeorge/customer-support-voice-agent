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
- [ ] **Live smoke (agent behaviour):** if this PR changes a path in `scripts/smoke_gated_paths.txt` (agent, core and knowledge source, gates, prompts, middleware, `sse.py`, `data/`, wiring, evals), push, then run `RUN_REAL=1 scripts/live_smoke.sh` on the PR head. It posts the `live-smoke` commit status on that exact commit; the `smoke-gate` status then turns green on its own. A later push that changes a gated path needs a new run. About 20 real model calls, hard cap 25. Paste the printed summary below for readers (the gate reads the status, not this text).

## Live smoke summary

```
(paste the scripts/live_smoke.sh summary, or write "not required: no gated path changed")
```
