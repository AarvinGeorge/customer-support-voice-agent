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
- After: zero display names or first-name mentions. 33 lines remain by design, all email local parts:
  `priya@acme.com` and `priya@acme-analytics.example` in redaction tests (mask output `p***@` is asserted in many tests and
  generated fixtures), and `tomas@`, `helen@`, `nadia@`, `omar@` billing emails in data/seeds/accounts.yaml (account data, not display names).
  Deliberate exception: data identifiers, not persona names.
- Edited by script: 47 files (YAML columns re-aligned in personas, users, triggers and the agent test persona fixture).
- Generated: packages/contracts/fixtures/*.jsonl regenerated with `scripts/build_fixtures.py` (2 files changed); gen_ts output unchanged.

## Suites (scripts/ci_local.sh, after merging origin/main c8866cd, #92)
python:services/core, knowledge, packages/contracts, services/agent, evals (sync, ruff, pyright, pytest), contracts-sync,
repo-tests, web (lint, vitest, build, typecheck, test:bundle), descriptors (incl. --all), import-guard, gitleaks:
33 passed, 0 failed, 0 skipped. Not run: docker jobs (pg-tests, compose), by instruction.
First run failed 4 steps because the email rename changed masked output (`p***@`); fixed by reverting the email local parts.

## Other open PRs
- #91 (cs-e-02-f14-mask-by-provenance): merges cleanly with this branch (git merge-tree), but it adds 3 lines with
  "Priya" (a test message and the priya@ email). After it merges, rename "Thanks Priya" to "Thanks Natasha" in its test.
- CS-E-02.F15 branch: not open on origin at the time of the check.
