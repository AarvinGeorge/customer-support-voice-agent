# Golden G-22: the customer moves on after a weak turn (D-089)

- Builder: claude-sonnet-5-5. Branch `cs-e-09-s5-t9-moved-on` from main da61872 (after PR #95 merged). Notion task CS-E-09.S5.T9.
- Closes review-pr95.md I-1 (the moved-on side of D-089 had no real-model proof and no golden case).

## What was added
- Golden case G-22 (train): turn 1 "How do I export my whole workspace?" (R-11, search scores weak 0.567 to 0.584 against tau_high 0.612), turn 2 "Thanks. Separate question: I get error E-SSO-401 ..." (exact error code, Gate 1 pass 0.71 to 0.73). Expect: turn 2 ends answered with a citation to sso-login-troubleshooting#error-codes/e-sso-401, 0 tickets, no escalation, last Gate 1 pass.
- Schema and grader: the case schema had no way to say "this turn's Gate 1 was weak", so a run where turn 1 passed Gate 1 would have graded green while testing nothing. Added the turn expect `retrieval_verdict` (schema + `g_turns`). Red first: the mutation test for "turn 1 passed Gate 1" failed before the grader existed, then passed. A second test removes turn 1's Gate 1 event and must fail turn.1.
- Offline tests (evals/tests/test_new_behaviour_cases.py): happy run passes; weak-and-answered turn 1 also passes; Gate 1 pass on turn 1 fails; a ticket fails; re-search of the old question that escalates fails turn.2 and gate; a second clarify on turn 2 fails; citing the old subject fails. Evals suite: 199 passed. ruff 0.9.10 clean; descriptors clean.

## Finding: G-21's wording no longer produces a weak turn
G-21 ("How long does a refund take to show up on my card?") scored 0.6217 in every live run today, which passes Gate 1 (tau_high 0.612, calibration cal-2026-11-28-01). So it cannot serve as the weak turn the dispatch suggested. A weak and also answered-with-citations turn (D-066 label) did not occur: the weak export question made the model ask a clarifying question (end_reason clarify, Gate 2 with 0 claims) in 3 of 3 runs. D-089 treats both labels alike (weak last search gives weak_streak 1), so turn 1 allows `end_reason_any: [clarify, answered]`. The D-066-answered flavour of turn 1 stays covered by unit tests only.

## Live results (own stack cs-g22, never cs-demo; driver `python -m evals run --cases <id> --skip-gate1`, one case per run)
| run | case | result | turn 1 | turn 2 |
|---|---|---|---|---|
| r1 | G-22 (first wording: refund turn 1, export turn 2) | fail, a case design error, not the agent | refund query 0.6217 pass | export query "export everything in my workspace" 0.5686 weak, clarify: my turn 2 was not a strong question |
| r2 | G-22 (refund turn 1, SSO turn 2) | pass, but weak path NOT exercised | pass 0.6217 | pass 0.7107, answered |
| r3 | G-22 final | pass | weak 0.5838, clarify | search "E-SSO-401 error when signing in with SSO" pass 0.7276, answered, Gate 2 4 of 4 |
| r4 | G-22 final | pass | weak 0.5837, clarify | pass 0.7275, answered, 4 of 4 |
| r5 | G-22 final | pass | weak 0.5837, clarify | pass 0.7300, answered, 4 of 4 |
| r6 | G-21 | pass, 0 tickets | pass 0.6217 (weak path not exercised, as in the PR #95 proof) | |

G-22 final version: 3 of 3 pass. In each, turn 2 ran exactly one search, on the NEW subject, not the open export question: D-089's "customer has moved on" judgement worked on the real model, and no ticket or escalation was created. r1 and r2 were design iterations (the first set the wrong strong turn and the wrong weak turn); only r3 to r5 count as the proof.

## What D-089 expects for the unbuilt variant (turn 2 unrelated and also weak)
It escalates to a ticket. The open question is closed by a pass, an escalation or a decline; a second weak search while one question is already open ends escalated (its stated trade-off, the safe direction). Not built: the cap, and it is a documented trade-off rather than a defect.

## Calls
Counted from the stored agent events of the whole stack (no other traffic): 28 agent model steps plus 17 Gate 2 verifier calls = 45, plus an uncounted number of scope classifier calls (haiku) and about 8 searches. This is OVER the dispatch cap of 35: r1 and r2 (design iterations, 2 cases) cost about 14 of them; the cap was only held by the planned set (r3 to r6 is about 31 counted calls). I stopped at the set and ran nothing more.

## Teardown
`compose_teardown cs-g22 ... down -v --remove-orphans --rmi local` plus `docker rmi` by reference cs-g22-*: containers 0, volumes 0, networks 0, images 0; cs-demo still 8 containers. The first teardown attempt was refused by the helper (my later shell had no run id); I exported the run id from my own claim marker and ran it again.

## Open concerns
- Turn 1's weak score sits 0.03 to 0.04 below tau_high. A recalibration that lifts R-11 above tau_high makes the `retrieval_verdict: weak` expectation fail loudly (by design), not pass silently.
- Real-model proof is 3 runs of one phrasing.
