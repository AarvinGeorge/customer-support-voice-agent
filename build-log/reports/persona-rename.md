# Persona rename (CS-E-01.F3, D-088), data only

## Mapping
| Old | New |
|---|---|
| Priya Raman (also Priya Shah in the web dev-mock) | Natasha Romanoff |
| Marcus Lee | Peter Parker |
| Dana Whitfield (also Dana Okafor in web tests and dev-mock) | Tony Stark |
| Sam Okafor (also Sam Rivera in the web dev-mock) | Steve Rogers |
| Tomas Reyes | Reed Richards |
| Helen Park | Sue Storm |
| Ivan Petrov | Stephen Strange |
| Nadia Ali | Wanda Maximoff |
| Omar Haddad | Carol Danvers |

First-name mentions (Priya, Marcus, Dana, Tomas, Helen, Ivan, Nadia, Omar, Sam) follow the same mapping, including test
identifiers (PRIYA, DANA, _marcus) and the ad hoc "Priya" and "Sam" users in services/core/tests/pg worlds.
User ids, account ids, account names, PINs, roles, tiers and scenarios are untouched.

## Inventory
- Before (origin/main c6a95b7): 193 matching lines in 46 files; zero in services/*/src (checked first; no hard-coded name).
  Per surname: Raman 50, Okafor 17, Whitfield 14, Haddad 6, Reyes 4, Petrov 4, plus first-name-only mentions.
- After: zero display names, first-name mentions or email local parts (follow-up 1: priya@ to natasha@, tomas@ to reed@,
  helen@ to sue@, nadia@ to wanda@, omar@ to carol@; domains kept; masked outputs `p***@` became `n***@`).
  Only `pat.lee@acme.com` (a test-only address, not a persona) is unchanged. Core logic: services/core/src/core/reads.py
  reads accounts.billing_email and returns only `billing_email_masked` (mask_email); it is not shown unmasked in the UI and no
  logic depends on the local part.
- Edited by script: 47 files (YAML columns re-aligned in personas, users, triggers and the agent test persona fixture).
- Generated: packages/contracts/fixtures/*.jsonl regenerated with `scripts/build_fixtures.py` (2 files changed); gen_ts output unchanged.

## Suites (scripts/ci_local.sh, after merging origin/main c8866cd, #92)
python:services/core, knowledge, packages/contracts, services/agent, evals (sync, ruff, pyright, pytest), contracts-sync,
repo-tests, web (lint, vitest, build, typecheck, test:bundle), descriptors (incl. --all), import-guard, gitleaks:
33 passed, 0 failed, 0 skipped. Not run: docker jobs (pg-tests, compose), by instruction.
Follow-up 1 re-run: core, contracts, agent, contracts-sync all pass.

## Other open PRs
- #91 (cs-e-02-f14-mask-by-provenance): merges cleanly with this branch (git merge-tree), but it adds 3 lines with
  "Priya" (a test message and an email). After it merges, rename "Thanks Priya" to "Thanks Natasha" in its test.
- CS-E-02.F15 branch: not open on origin at the time of the check.
