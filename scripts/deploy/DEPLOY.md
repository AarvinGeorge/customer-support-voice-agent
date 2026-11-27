# Deploy runbook: Quillstack on one VM (CS-E-08.S4.T2)

Source: doc 09 s5, doc 06 s10, doc 07 s15.2; D-029 (same Compose stack locally and on the VM), D-047 (EDGE_SUBNET), D-049 (SITE_ADDRESS).
Everything below is prepared and was proven on a laptop with `--local` (see the PR). What is left for the owner: a VM, a domain, an SSH key.

## 0. What the owner supplies

| Item | Detail |
|---|---|
| VM | Ubuntu 22.04 or 24.04, 2 vCPU and 4 GB RAM minimum (builds three Python images plus Next.js; add 2 GB swap if the build is killed), 25 GB disk |
| Domain | an A record pointing at the VM's public IP; ports 80 and 443 open (Caddy gets the certificate by itself) |
| SSH | a deploy user in the `docker` group, key-based login; the private key goes in the GitHub secret `VM_SSH_KEY` (workflow path) |
| Host stack | `docker` with the compose plugin, `git`, `curl`, `python3` (all standard; the script uses them) |
| Spend caps | provider spend caps and rate limits set BEFORE the URL is shared (CLAUDE.md s6) |

## 1. One-time VM setup

```
ssh deploy@VM
git clone git@github.com:AarvinGeorge/customer-support-voice-agent.git quillstack   # needs a read-only deploy key on the repo
cd quillstack
```

## 2. The .env on the VM (never committed)

Two ways; pick one. The file is mode 600 and holds every secret.

**A. Fresh values from a scratch clone (simplest).** Do not copy your laptop's working `.env`: generate new internal secrets for the VM.
```
git clone git@github.com:AarvinGeorge/customer-support-voice-agent.git /tmp/qs-vm-env && cd /tmp/qs-vm-env
python3 scripts/keys.py gen            # fresh internal secrets and the six database URLs
python3 scripts/keys.py set            # paste the three provider keys (ideally spend-capped ones for the VM)
python3 scripts/keys.py status         # everything set
scp .env deploy@VM:~/quillstack/.env && ssh deploy@VM 'chmod 600 ~/quillstack/.env' ; rm -rf /tmp/qs-vm-env
```
Then on the VM add the edge and behaviour settings from the table below (`SITE_ADDRESS`, `SITE_ORIGIN`, `DEMO_FAULTS_ENABLED`, `FEATURE_A4`) and remove local-only lines (`EDGE_PORT`, a `localhost` `SITE_ORIGIN`). Keep `EDGE_SUBNET` and `FORWARDED_ALLOW_IPS` equal (or both unset).

**B. From GitHub Actions secrets (workflow path).** Add these as repository secrets; `deploy.yml` renders the file with `scripts/deploy/deploy.sh render-env` and copies it to the VM.

| Group | Secret names |
|---|---|
| SSH | `VM_HOST`, `VM_USER`, `VM_SSH_KEY`, `VM_KNOWN_HOSTS` (output of `ssh-keyscan VM_HOST`), `VM_PATH` (for example `/home/deploy/quillstack`) |
| Providers | `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `LANGSMITH_API_KEY` |
| Internal | `CORE_TOKEN_AGENT`, `CORE_TOKEN_COMMIT`, `CORE_TOKEN_OPS`, `KNOWLEDGE_TOKEN_AGENT`, `SESSION_SIGNING_KEY`, `PIN_PEPPER`, `OPERATOR_TOKEN_KEY`, `DEMO_ACCESS_CODE`, `OPERATOR_ACCESS_CODE` |
| Postgres | `POSTGRES_PASSWORD`, `AGENT_OWNER_PASSWORD`, `AGENT_APP_PASSWORD`, `CORE_OWNER_PASSWORD`, `CORE_APP_PASSWORD`, `KNOWLEDGE_OWNER_PASSWORD`, `KNOWLEDGE_APP_PASSWORD`, and the six URLs `AGENT_DB_URL`, `CORE_DB_URL`, `KNOWLEDGE_DB_URL`, `AGENT_DB_OWNER_URL`, `CORE_DB_OWNER_URL`, `KNOWLEDGE_DB_OWNER_URL` (host is `postgres`; keys.py composes them) |
| Edge | secrets `SITE_ADDRESS` (the bare domain, no scheme) and `SITE_ORIGIN` (`https://` plus the same domain) |
| Variables (repository variables) | `DEMO_FAULTS_ENABLED` and `FEATURE_A4` (REQUIRED, `true` or `false`); optional `EDGE_SUBNET` and `FORWARDED_ALLOW_IPS` (equal), `LANGSMITH_TRACING`, `LANGSMITH_PROJECT`, `LANGSMITH_ENDPOINT`, `REDIS_URL`, `CORE_URL`, `KNOWLEDGE_URL` |

### Settings that change behaviour (set explicitly; preflight refuses the first two if unset)

| Name | Value for the demo | Effect |
|---|---|---|
| `DEMO_FAULTS_ENABLED` | `true` for the Loom (the drawer's demo controls need it), `false` once the URL is public beyond the recording | honoured faults `tool_error` and `llm_429` (doc 07 s9); read back from core and the edge by the smoke |
| `FEATURE_A4` | `false` (designed only, doc 09 s1.2) | keeps the A4 flow off |
| `EDGE_SUBNET`, `FORWARDED_ALLOW_IPS` | unset (default `172.30.0.0/24` for both) or equal | uvicorn trusts X-Forwarded-For only from this network (D-047) |
| `DEMO_RESEED` | never in the file | only the `reseed` command sets it |

The workflow keeps the previous VM file as `.env.previous` before it writes the new one, so a rollback has the config that matched the old tag. Values may not contain a single quote or newline (`render-env` refuses them; it writes `NAME='value'`).

The list of required names is `scripts/deploy/required-env.txt`; a test fails if it drifts from `compose.yaml`. A missing one fails the workflow (and `render-env`) naming it, never showing a value.
The workflow only runs by hand (Actions tab, "deploy", enter a tag) and uses the `production` environment, so you can add a required reviewer there.

## 3. Release and deploy

1. Merge to `main` with CI green; tag it: `git tag v0.1.0 && git push origin v0.1.0`.
2. On the VM (or through the workflow): `scripts/deploy/deploy.sh up --tag v0.1.0`.

`up` runs these steps in order and stops at the first failure (each is printed):

| Step | What it does |
|---|---|
| preflight | env file present, mode 600, all 25 secrets set, `SITE_ADDRESS` set (Caddy refuses to start otherwise) and not plain http, `SITE_ORIGIN` equals `https://SITE_ADDRESS`, `FORWARDED_ALLOW_IPS` equals `EDGE_SUBNET`, `DEMO_RESEED` off |
| rollback point | writes the current GOOD tag (`.deploy/good`) to `.deploy/rollback_to` and marks the new tag `pending`, before anything changes |
| checkout | `git fetch --tags`, refuses a dirty checkout or an unknown tag, `git checkout --detach tag` |
| build | `docker compose --profile seed --profile jobs build` on the VM: every image including the seed and ingest job images (no registry), so the jobs run this tag's code |
| infra | postgres and redis up and healthy |
| backup | when the service tables already exist: `pg_dump` to `.deploy/backups/pre-TAG-time.sql`, then read back: size over 1000 bytes, the "dump complete" marker, a restore into a scratch database `quillstack_restore_check` with the same service-table count as live (then dropped). A failed read-back stops the deploy before any migration |
| migrate | `migrate_core`, `migrate_knowledge`, `migrate_agent` as the owner roles; applied files are skipped |
| seed | `seed_core`: seeds an empty schema, leaves an existing demo untouched (safe on every deploy) |
| ingest | `ingest_knowledge`: unchanged articles cost no embedding call (a first ingest embeds every chunk, about 80, in one OpenAI call) |
| in-flight gate | counts `agent.turns` that are running and not past their deadline; waits up to `INFLIGHT_WAIT_S` (default 90 s), then aborts without touching anything. No deploy during a Loom take (doc 09 Sun 14:00 freeze) |
| rolling restart | `core` and `knowledge` (recreated together: turns that need them fail for those seconds, hence the gate), then `agent_a` (wait healthy), then `agent_b` (wait healthy; Caddy keeps routing to the healthy replica), then `web`, then `caddy` recreated so a changed Caddyfile applies |
| smoke | see section 4 |
| mark good | only now is the tag written to `.deploy/good`; the old good tag stays as `rollback_to`; `pending` is cleared |

Compose form on the VM is `-f compose.yaml` only (no override: no source mounts, no reload). Caddy publishes 80 and 443.

## 4. Smoke (also `scripts/deploy/deploy.sh smoke`)

Read-backs use throwaway containers of the release image, never an exec into a live one. Expected values are computed from the checked-out tag, not from a remembered figure: contract from `packages/contracts/VERSION`, policy as `version_label+sha256[:12]` of `data/policy.yaml`, embedding model from `data/models.yaml`, `demo_faults` from the env file.
- revision: the four app containers (core, knowledge, agent_a, agent_b) run the image ids just built; the tag and commit are printed.
- core `/readyz` (status 200): `status` ready, `contract_version` and `policy_version` equal to the expected values, `demo_faults` equal, `seed_date` present.
- knowledge `/readyz` (status 200): `status` ready, `embedding_model` equal, `chunks` at least 1, `calibration_status` present.
- edge `https://DOMAIN/api/readyz`: `status` ready, `contract_version` equal, `redis`, `postgres`, `core`, `knowledge` all `ok`, `demo_faults` equal. A failure prints the missing field and the body.
- chat: `POST /api/access` with `DEMO_ACCESS_CODE`, `POST /api/sessions`, then one turn on `/api/sessions/{sid}/turns` that must end with `event: turn.completed`. `SMOKE_CHAT=session` stops after the session, `SMOKE_CHAT=off` skips chat. One turn is one real model call.

## 5. Reseed before the Loom

The demo data ages: S1 breaks about 10 days after seeding (dates in the seeds are relative to the seeding day). Reseed within a day of recording and again before any later demo:
```
scripts/deploy/deploy.sh reseed
```
It wipes the core tables except `audit_events`, reseeds, and smokes. It sets `DEMO_RESEED=true` for that one job only; an env file with it on fails preflight. Reseeding does not touch knowledge or agent sessions.

## 6. Rollback

Trigger (any one): the smoke at the end of `up` is red (the script prints the rollback command); or the smoke is run by hand within 15 minutes of a release and is red; or the S1 chat scenario fails on the public URL. If it is not green and fixed by Sun 22:00, record the Loom from local Compose (doc 09).
```
scripts/deploy/deploy.sh rollback               # code only
scripts/deploy/deploy.sh rollback --restore-db  # code and the pre-migrate database backup of that deploy
```
It restores the last GOOD tag: the rollback point was saved before the failed `up` changed anything, and a tag becomes good only after a green smoke. It checks out that tag, rebuilds, (with `--restore-db` stops the app services and restores `.deploy/last_backup`), rolling-restarts, and smokes. Use `--restore-db` when the failed release ran a migration that the old code cannot read; otherwise code-only rollback keeps the audit rows and sessions written since the backup.
Limits, stated: migrations are forward-only and assumed additive, so old code normally runs on the newer schema; a restore loses everything written after the backup was taken. The first deploy has no rollback target (the script says so). Rollback does not restore `.env`: copy `.env.previous` back by hand when a settings change caused the failure.

## 7. Day-two checks

- `docker compose -f compose.yaml ps` (all healthy; always pass `-f compose.yaml` on the VM so the local override is not merged), `docker compose -f compose.yaml logs --tail 50 agent_a`.
- Caddy certificate trouble: DNS must already point at the VM; `docker compose -f compose.yaml logs caddy`.
- A stack that will not start with an empty `SITE_ADDRESS` is intended (D-049).

## 8. Proven locally (not on a VM)

`scripts/deploy/deploy.sh --local up` ran end to end (fix round 1 adds the backup, in-flight gate, version read-backs, a forced failure and a rollback, see the report) against Compose (`COMPOSE_PROJECT_NAME=cs-deploy`, `EDGE_SUBNET=172.31.88.0/24`, `SITE_ADDRESS=:80`, host port 8088, same `compose.yaml` images and commands as the VM; the overlay only changes the published port). Not proven: real TLS, a real domain, the SSH and workflow path, a first-time VM build. See `build-log/reports/laneA3-deploy-report.md`.
