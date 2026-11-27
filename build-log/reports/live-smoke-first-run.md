# Live smoke, first real run (CS-E-06.S1.T2)

- When: Fri 27 Nov 2026, 18:36:17 to 18:39:43 UTC (`date -u` before and after the run).
- Command: `RUN_REAL=1 SMOKE_OUT=.tmp/smoke-run1 scripts/live_smoke.sh` from `wt-lane-c`, commit `7b161f7c4fee`.
- Stack: `cs-smoke-183617`, Caddy on 127.0.0.1:3100, Postgres test port 55500, subnet 172.30.100.0/24. Ingest: `embedding_calls=1`.
- Teardown: `down -v`; 0 containers and 0 volumes of the project left. cs-demo (port 3000) was not touched: its containers started 17:57 to 18:05 UTC, before this run.
- Exit 1 (RESULT: FAIL). That is the gate doing its job: two real agent defects, found before any merge.

## Summary as printed

```
LIVE SMOKE 2026-11-27T18:38Z commit 7b161f7c4fee
stack cs-smoke-183617 via Caddy 127.0.0.1:3100  models claude-haiku-5-5,claude-sonnet-5-5,gpt-5.4-mini-2026-03-17

case                                  result   calls  end reasons per turn
S1 verify, commit, policy follow-up   FAIL         7  verification_requested > readback > committed > clarify
S3 member decline                     FAIL         4  verification_requested > clarify
S3 owner approval                     FAIL         3  verification_requested > readback > submitted
S4 out of docs                        PASS         2  escalated
S5 llm_429 fallback                   PASS         3  answered

checks: stop 9/11 | no truncation 11/11 | gate1 7/7 | gate2 5/6 | clarify only where expected 8/11
agent log warnings: none
real calls: agent 15, verifier 3, classifier 1 = 19 model calls (cap 25); embeddings 5 (ingest 1, search 4, retries 0); cost est $0.0574
  FAIL S1 verify, commit, policy follow-up: outcome: observed clarify, allowed ['action', 'answer']
  FAIL S1 verify, commit, policy follow-up: end_reason: observed clarify, expected answered
  FAIL S1 verify, commit, policy follow-up: turn.3: end_reason clarify, expected answered
  FAIL S1 verify, commit, policy follow-up: live.stop.3: end_reason clarify, expected answered
  FAIL S1 verify, commit, policy follow-up: live.clarify.3: retrieval decision clarify; end_reason clarify
  FAIL S3 member decline: outcome: observed clarify, allowed ['decline']
  FAIL S3 member decline: end_reason: observed clarify, expected answered
  FAIL S3 member decline: turn.1: end_reason clarify, expected answered
  FAIL S3 member decline: must_mention: missing one of [['owner'], ['billing admin'], ['ticket']]
  FAIL S3 member decline: live.stop.1: end_reason clarify, expected answered
  FAIL S3 member decline: live.clarify.1: grounding decision clarify; end_reason clarify; clarify line in the reply
  FAIL S3 member decline: live.gate2.case: 0 grounding gates passed in the case
  ... 1 more in the JSON
RESULT: FAIL
```

## What the failures are (read from the saved SSE events)

1. **S1 policy follow-up, real defect.** The follow-up searched "how long until a refund appears on my statement". Gate 1 returned `weak` (top cosine 0.556, tau_low 0.355, tau_high 0.612), so the retrieval decision was `clarify`. The model still answered in full, and Gate 2 passed it (5 of 5 claims supported, citing `refund-policy#did-my-refund-go-through-and-when-will-i-see-it`). The turn still ended `clarify`, because `agent/sse.py` (classify_final) sets end_reason clarify whenever the retrieval decision is clarify. The golden graders fail it too (outcome, end_reason, turn.3). Two possible owners: the Gate 1 thresholds are provisional and the answerable query scores below tau_high, and the end_reason rule labels an answer as a clarify.
2. **S3 member decline, real defect (the same class as the browser pass).** The decline draft had 6 claims. 5 were supported. "I can open a ticket for our support team that describes your request" was marked unsupported with no source, so Gate 2 failed and the fixed clarify line replaced a correct decline. The golden graders fail it too (outcome, end_reason, turn.1, must_mention owner, billing admin, ticket).
3. **S3 owner approval, false positive in the smoke (fixed).** The search on the card turn was weak (0.601), so Gate 1's decision was `clarify`, but the turn correctly ended `readback` and then `submitted`. The first version of the clarify check counted the Gate 1 decision. That decision is an instruction to the model, not the clarify line. Fixed in commit 96e0f6f with a test that fails on the old code. The golden graders passed this case.

## Offline regrade with the fixed check (0 model calls, from the saved JSON)

```
S1 verify, commit, policy follow-up    live fails [live.stop.3 end_reason clarify, expected answered; live.clarify.3 end_reason clarify] | golden fails [outcome, end_reason, turn.3]
S3 member decline                      live fails [live.stop.1; live.clarify.1 grounding decision clarify, end_reason clarify, clarify line in the reply; live.gate2.case 0 grounding gates passed] | golden fails [outcome, end_reason, turn.1, must_mention]
S3 owner approval                      live fails none | golden fails none
S4 out of docs                         live fails none | golden fails none
S5 llm_429 fallback                    live fails none | golden fails none
```

## Real calls

19 model calls (agent 15 including the gpt-5.4-mini fallback, verifier 3, classifier 1) under the cap of 25. 5 embeddings (ingest 1, search 4, retries 0). Estimated cost $0.0574. No truncation in any turn, and no agent warning lines.
