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

**A. From your laptop (simplest).** Generate and verify locally, then copy:
```
python3 scripts/keys.py status          # everything the stack needs is set
scp .env deploy@VM:~/quillstack/.env && ssh deploy@VM 'chmod 600 ~/quillstack/.env'
```
Then on the VM append the edge settings (not secrets): `SITE_ADDRESS=demo.example.com`, `SITE_ORIGIN=https://demo.example.com`.
Remove any local-only lines (`EDGE_PORT`, a `localhost` SITE_ORIGIN) and keep `EDGE_SUBNET` and `FORWARDED_ALLOW_IPS` equal (or both unset: the default `172.30.0.0/24` is used for both).

**B. From GitHub Actions secrets (workflow path).** Add these as repository secrets; `deploy.yml` renders the file with `scripts/deploy/deploy.sh render-env` and copies it to the VM.

| Group | Secret names |
|---|---|
| SSH | `VM_HOST`, `VM_USER`, `VM_SSH_KEY`, `VM_KNOWN_HOSTS` (output of `ssh-keyscan VM_HOST`), `VM_PATH` (for example `/home/deploy/quillstack`) |
| Providers | `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `LANGSMITH_API_KEY` |
| Internal | `CORE_TOKEN_AGENT`, `CORE_TOKEN_COMMIT`, `CORE_TOKEN_OPS`, `KNOWLEDGE_TOKEN_AGENT`, `SESSION_SIGNING_KEY`, `PIN_PEPPER`, `OPERATOR_TOKEN_KEY`, `DEMO_ACCESS_CODE`, `OPERATOR_ACCESS_CODE` |
| Postgres | `POSTGRES_PASSWORD`, `AGENT_OWNER_PASSWORD`, `AGENT_APP_PASSWORD`, `CORE_OWNER_PASSWORD`, `CORE_APP_PASSWORD`, `KNOWLEDGE_OWNER_PASSWORD`, `KNOWLEDGE_APP_PASSWORD`, and the six URLs `AGENT_DB_URL`, `CORE_DB_URL`, `KNOWLEDGE_DB_URL`, `AGENT_DB_OWNER_URL`, `CORE_DB_OWNER_URL`, `KNOWLEDGE_DB_OWNER_URL` (host is `postgres`; keys.py composes them) |
| Edge | `SITE_ADDRESS` (the bare domain, no scheme), `SITE_ORIGIN` (`https://` plus the same domain); optional repository variables `EDGE_SUBNET` and `FORWARDED_ALLOW_IPS` (must be equal) |

The list of required names is `scripts/deploy/required-env.txt`; a test fails if it drifts from `compose.yaml`. A missing one fails the workflow (and `render-env`) naming it, never showing a value.
The workflow only runs by hand (Actions tab, "deploy", enter a tag) and uses the `production` environment, so you can add a required reviewer there.

## 3. Release and deploy

1. Merge to `main` with CI green; tag it: `git tag v0.1.0 && git push origin v0.1.0`.
2. On the VM (or through the workflow): `scripts/deploy/deploy.sh up --tag v0.1.0`.

`up` runs these steps in order and stops at the first failure (each is printed):

| Step | What it does |
|---|---|
| preflight | env file present, mode 600, all 25 secrets set, `SITE_ADDRESS` set (Caddy refuses to start otherwise) and not plain http, `SITE_ORIGIN` equals `https://SITE_ADDRESS`, `FORWARDED_ALLOW_IPS` equals `EDGE_SUBNET`, `DEMO_RESEED` off |
| checkout | `git fetch --tags`, refuses a dirty checkout or an unknown tag, `git checkout --detach tag` |
| build | `docker compose build` on the VM (images are built there, no registry) |
| infra | postgres and redis up and healthy |
| migrate | `migrate_core`, `migrate_knowledge`, `migrate_agent` as the owner roles; applied files are skipped |
| seed | `seed_core`: seeds an empty schema, leaves an existing demo untouched (safe on every deploy) |
| ingest | `ingest_knowledge`: unchanged articles cost no embedding call (a first ingest embeds about 74 chunks, one OpenAI call) |
| rolling restart | `core` and `knowledge`, then `agent_a` (wait healthy), then `agent_b` (wait healthy), then `web`, then `caddy`; Caddy takes a replica out while it fails `/api/readyz`, so one replica serves throughout |
| smoke | see section 4 |
| record | writes the tag to `.deploy/current` (the old one becomes `.deploy/previous`) |

Compose form on the VM is `-f compose.yaml` only (no override: no source mounts, no reload). Caddy publishes 80 and 443.

## 4. Smoke (also `scripts/deploy/deploy.sh smoke`)

Read-backs use throwaway containers of the release image, never an exec into a live one.
- core `/readyz`: `status` ready, `contract_version` equals `packages/contracts/VERSION`, `policy_version` present, `seed_date`.
- knowledge `/readyz`: `status` ready, `chunks` at least 1, `embedding_model`, `calibration_status`.
- edge `https://DOMAIN/api/readyz`: `contract_version` equals the file, `redis`, `postgres`, `core`, `knowledge` all ok.
- chat: `POST /api/access` with `DEMO_ACCESS_CODE`, `POST /api/sessions`, then one turn on `/api/sessions/{sid}/turns` that must end with `event: turn.completed`. `SMOKE_CHAT=session` stops after the session, `SMOKE_CHAT=off` skips chat. One turn is one real model call.

## 5. Reseed before the Loom

The demo data ages: S1 breaks about 10 days after seeding (dates in the seeds are relative to the seeding day). Reseed within a day of recording and again before any later demo:
```
scripts/deploy/deploy.sh reseed
```
It wipes the core tables except `audit_events`, reseeds, and smokes. It sets `DEMO_RESEED=true` for that one job only; an env file with it on fails preflight. Reseeding does not touch knowledge or agent sessions.

## 6. Rollback

Trigger: smoke fails after `up`, or the demo misbehaves after a release.
```
scripts/deploy/deploy.sh rollback          # checks out .deploy/previous, rebuilds, rolling restart, smoke
```
Limits, stated: migrations are forward-only and additive, so the previous code runs on the newer schema; a release with a destructive migration needs a manual database restore (take `docker compose exec postgres pg_dump -U postgres quillstack > backup.sql` before such a release). Rollback needs a previous successful `up`; with none recorded it stops instead of guessing. The first deploy has no rollback target.

## 7. Day-two checks

- `docker compose -f compose.yaml ps` (all healthy), `docker compose -f compose.yaml logs --tail 50 agent_a`.
- Caddy certificate trouble: DNS must already point at the VM; `docker compose -f compose.yaml logs caddy`.
- A stack that will not start with an empty `SITE_ADDRESS` is intended (D-049).

## 8. Proven locally (not on a VM)

`scripts/deploy/deploy.sh --local up` ran end to end against Compose (`COMPOSE_PROJECT_NAME=cs-deploy`, `EDGE_SUBNET=172.31.88.0/24`, `SITE_ADDRESS=:80`, host port 8088, same `compose.yaml` images and commands as the VM; the overlay only changes the published port). Not proven: real TLS, a real domain, the SSH and workflow path, a first-time VM build. See `build-log/reports/laneA3-deploy-report.md`.
