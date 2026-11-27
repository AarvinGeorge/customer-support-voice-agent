# Deploy runbook: Quillstack on one VM (CS-E-08.S4.T2)

Source: doc 09 s5, doc 06 s10, doc 07 s15.2; D-029 (same Compose stack locally and on the VM), D-047 (EDGE_SUBNET), D-049 (SITE_ADDRESS),
D-072 (images are built once in CI after the gates, pushed to GHCR, and deployed by digest; the VM never builds).
Everything below is prepared and was proven on a laptop with `--local` (see the PR). What is left for the owner: a VM, a domain, an SSH key.

## 0. What the owner supplies

| Item | Detail |
|---|---|
| VM | Ubuntu 22.04 or 24.04, 2 vCPU and 4 GB RAM (runs the stack; nothing is built on it), 25 GB disk |
| Domain | an A record pointing at the VM's public IP; ports 80 and 443 open (Caddy gets the certificate by itself) |
| SSH | a deploy user in the `docker` group, key-based login; the private key goes in the GitHub secret `VM_SSH_KEY` (workflow path) |
| Host stack | `docker` with the compose plugin (2.24 or later, for `!reset`), `git`, `curl`, `python3` 3.10 or later (all standard; the script uses them); `gh` only if you deploy by hand without `--manifest` |
| Registry read access | the images are private packages under `ghcr.io/aarvingeorge/customer-support-voice-agent/`. The workflow logs the VM in for the run with its own read-only token and logs out after. For a deploy or rollback by hand, log in once: `docker login ghcr.io -u <github user>` with a classic token that has only `read:packages`. Images already on the VM (a rollback) need no login |
| Spend caps | provider spend caps and rate limits set BEFORE the URL is shared (CLAUDE.md s6) |
| Web build variables | repository variables `LANGSMITH_PROJECT` (the LangSmith project name) and `LANGSMITH_PROJECT_URL` (the project page, `https://...`), set BEFORE the commit on `main` that will be released: CI bakes them into the web image (the drawer's trace link). On a push to `main` the `images` job FAILS when either is empty or the URL is not https, so no release can carry an empty link; on a pull request it only warns |

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
| Variables (repository variables) | `DEMO_FAULTS_ENABLED` and `FEATURE_A4` (REQUIRED, `true` or `false`); `LANGSMITH_PROJECT` and `LANGSMITH_PROJECT_URL` (REQUIRED by the CI `images` job on `main`, build time only, see section 0); optional `EDGE_SUBNET` and `FORWARDED_ALLOW_IPS` (equal), `LANGSMITH_TRACING`, `LANGSMITH_PROJECT`, `LANGSMITH_ENDPOINT`, `REDIS_URL`, `CORE_URL`, `KNOWLEDGE_URL` |

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

## 3. Build once, release, deploy (D-072)

The artifact production runs is the one CI built after the gates. Nothing rebuilds it.

| Stage | Where | What happens |
|---|---|---|
| build once | `.github/workflows/ci.yml`, job `images`, on every push to `main` | runs only after every gate job is green (`needs`); `docker buildx bake -f compose.yaml` builds `agent`, `core`, `knowledge` and `web` from the same build sections local Compose uses, labels them (`org.opencontainers.image.source`, `.revision`, `.title`), pushes `ghcr.io/aarvingeorge/customer-support-voice-agent/<image>:<sha>` with the run's `GITHUB_TOKEN` (`packages: write`, `contents: read`), reads each digest back from GHCR, and uploads the artifact `images-<sha>` (`images-<sha>.json`: revision, repo, per image digest and `<repo>/<image>@sha256:...`). The migrate, seed and ingest jobs use their service's image (same Dockerfile and `SERVICE` argument) |
| dry run on a PR | the same job on a same-repo pull request | the same build and push under the tag `pr-<n>`, which no release can name |
| release | `.github/workflows/release.yml` | push a tag `r-NN` (or `r-NN.N` for a fix release) on a commit of `main`, or run it from the Actions tab with `tag` and `sha`. It refuses a commit not on `main` or without a green `ci` run on `main`, takes that run's `images-<sha>` artifact (no build), writes `release-<tag>.json`, validates it as the deploy will, reads every digest back from GHCR and attaches the file to GitHub Release `<tag>` |
| deploy | `scripts/deploy/deploy.sh up --tag r-NN` on the VM, or `.github/workflows/deploy.yml` (Actions tab, enter the tag) | below |

Commands:
1. Merge to `main` and wait for `ci` (including `images`) to go green.
2. `git tag r-01 <sha> && git push origin r-01` (or run the `release` workflow with `tag` and `sha`).
3. On the VM: `scripts/deploy/deploy.sh up --tag r-01` (it runs `gh release download r-01` for the manifest when it has none; or pass `--manifest release-r-01.json`), or run the `deploy` workflow with the tag.

The web image bakes two public build arguments (`NEXT_PUBLIC_LANGSMITH_PROJECT`, `NEXT_PUBLIC_LANGSMITH_PROJECT_URL`) from the repository variables `LANGSMITH_PROJECT` and `LANGSMITH_PROJECT_URL` at CI build time; changing them needs a new build (a new commit on `main`), not a VM setting. The `images` job refuses to build on `main` while either is empty (step "Require the web image's public build arguments").

### First run of the release path: rehearsal release r-00 (do this once, right after this change merges, before Sat 20:00)

`release.yml` cannot run before it is on `main`, so its first run is this rehearsal, not `r-01`. r-00 is never deployed; it proves the tag, the artifact hand-off, the GHCR read-back and the asset.
```
gh variable set LANGSMITH_PROJECT --body '<project name>'                   # owner or coordinator, once (section 0)
gh variable set LANGSMITH_PROJECT_URL --body 'https://smith.langchain.com/...'
git fetch origin && sha="$(git rev-parse origin/main)"                       # the merge commit (or a later one)
gh run list --workflow ci.yml --branch main --event push --commit "$sha" --json status,conclusion,databaseId
#   wait until it shows conclusion "success" (its images job pushed the four <sha> images)
git tag r-00 "$sha" && git push origin r-00                                  # triggers release.yml
gh run watch "$(gh run list --workflow release.yml --limit 1 --json databaseId --jq '.[0].databaseId')" --exit-status
gh release view r-00 --json assets --jq '.assets[].name'                     # expect: release-r-00.json
gh release download r-00 --pattern release-r-00.json --dir /tmp/r00 && python3 scripts/deploy/release_manifest.py env --manifest /tmp/r00/release-r-00.json --tag r-00
#   expect four *_IMAGE lines pinned @sha256 and RELEASE_REVISION=<sha>
```
Expected refusals worth one try each (they must fail, which shows the guards can fire): `gh workflow run release.yml -f tag=r-0x -f sha=$sha` (bad tag format) and the same with a sha not on `main`. To discard the rehearsal: `gh release delete r-00 --cleanup-tag --yes`. Record the run URLs in the decision log. The deploy half (VM login over SSH, `gh release download` on the VM) is first run with `r-01` through `deploy.yml`.

`up` runs these steps in order and stops at the first failure (each is printed):

| Step | What it does |
|---|---|
| preflight | env file present, mode 600, all 25 secrets set, `SITE_ADDRESS` set (Caddy refuses to start otherwise) and not plain http, `SITE_ORIGIN` equals `https://SITE_ADDRESS`, `FORWARDED_ALLOW_IPS` equals `EDGE_SUBNET`, `DEMO_RESEED` off |
| release manifest | `release-<tag>.json` from `--manifest`, the copy kept in `.deploy/releases/`, or `gh release download`; validated by `scripts/deploy/release_manifest.py env`: it is a release for this tag, lists exactly the four images, each pinned as `ghcr.io/aarvingeorge/customer-support-voice-agent/<image>@sha256:<64 hex>` (a mutable tag or another registry is refused). A missing or refused manifest stops here, before anything changes; there is no build fallback |
| rollback point | writes the current GOOD tag (`.deploy/good`) to `.deploy/rollback_to` and marks the new tag `pending`, before anything changes |
| checkout | `git fetch --tags`, refuses a dirty checkout or an unknown tag, `git checkout --detach tag`; then the checkout HEAD must equal the manifest's revision, so the config files (`compose.yaml`, `Caddyfile`, `data/`, `infra/postgres/init`) and the images come from one commit |
| pull | `docker pull <repo>/<image>@sha256:...` for the four images (an image already on the host under that digest is the same bytes and is not fetched again). Every Compose call merges `scripts/deploy/compose.release.yaml`, which replaces each build section with `build: !reset null` and the digest reference, so no Compose command on the VM can build |
| infra | postgres and redis up and healthy |
| backup | when the service tables already exist: `pg_dump` of the `core`, `knowledge` and `agent` schemas to `.deploy/backups/pre-TAG-time.sql`, read back: size over 1000 bytes, the "dump complete" marker, a restore into a scratch database `quillstack_restore_check` with the same table count as live, and a manifest of row counts (`.sql.manifest`, taken from that scratch restore) beside it. The record `.deploy/backup` binds the file to the good tag it was taken on top of (BASE) and the release it precedes; a retry of a failed release keeps the first backup (the good state) instead of taking a second one of the half-changed database; a new release clears an older record. A failed read-back stops the deploy before any migration |
| migrate | `migrate_core`, `migrate_knowledge`, `migrate_agent` as the owner roles; applied files are skipped |
| seed | `seed_core`: seeds an empty schema, leaves an existing demo untouched (safe on every deploy) |
| ingest | `ingest_knowledge`: unchanged articles cost no embedding call (a first ingest embeds every chunk, about 80, in one OpenAI call) |
| in-flight gate | counts `agent.turns` that are running and not past their deadline; waits up to `INFLIGHT_WAIT_S` (default 90 s), then aborts before any container is replaced (the backup, migrations, seed and ingest have already run; migrations are additive). No deploy during a Loom take (doc 09 Sun 14:00 freeze) |
| rolling restart | `core` and `knowledge` (recreated together: turns that need them fail for those seconds, hence the gate), then `agent_a` (wait healthy), then `agent_b` (wait healthy; Caddy keeps routing to the healthy replica), then `web`, then `caddy` recreated so a changed Caddyfile applies |
| smoke | see section 4 |
| mark good | only now is the tag written to `.deploy/good`; the old good tag stays as `rollback_to`; `pending` is cleared |

Compose form on the VM is `-f compose.yaml -f scripts/deploy/compose.release.yaml` (no `compose.override.yaml`: no source mounts, no reload). Caddy publishes 80 and 443.

## 4. Smoke (also `scripts/deploy/deploy.sh smoke`)

Read-backs use throwaway containers of the release image, never an exec into a live one. Expected values are computed from the checked-out tag, not from a remembered figure: contract from `packages/contracts/VERSION`, policy as `version_label+sha256[:12]` of `data/policy.yaml`, embedding model from `data/models.yaml`, `demo_faults` from the env file.
- revision: each app container (core, knowledge, agent_a, agent_b, web) has the manifest's digest reference as its image and the image id that reference resolves to on the host (so a container left on an older image fails); the tag and commit are printed, and the readings are written to `.deploy/running` (`release <tag> revision <sha>`, then `<service> <reference> <image id>` per line). `.deploy/current` names the release the switch started on: it is written before the first container is recreated, so after a restart that failed part way it already names the new release.
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
It redeploys the last GOOD release: the rollback point was saved before the failed `up` changed anything, and a tag becomes good only after a green smoke. It reads that release's manifest (kept in `.deploy/releases/` by its deploy), checks out its tag, pulls its digests (normally still on the VM, so no login is needed), rolling-restarts and smokes with the same read-backs as `up`: the running digests must equal the previous release's. It never rebuilds an old tag; a release with no manifest (one deployed before D-072) cannot be a rollback target, and the script says so. Like `up`, it checks that the checkout is the commit the release's images were built from. Unlike `up`, it has no in-flight gate: it is an outage action and does not wait for open turns.

`--restore-db` also puts the database back, from the backup recorded for that tag. It refuses a backup whose BASE is not the rollback tag. It then stops the four app services (the restore's Compose calls use the images of the release that is running, `.deploy/current`, so a failed restore restarts exactly what was running) and, in ONE transaction, drops the three service schemas with CASCADE (so tables or columns a failed release added vanish) and replays the dump. If anything fails the transaction rolls back, the database is exactly as before, the app services are started again with `up -d --no-deps --wait` (the form the rolling restart uses; `compose start` does not work here) and the script stops. If that restart also fails the script says the site is down and prints the command to run; it never hides the failure. After a successful replay every table's row count is compared with the manifest. The database is never left partial or empty. Use it when the failed release ran a migration old code cannot read; otherwise a code-only rollback keeps the audit rows and sessions written since the backup. `scripts/deploy/restore_pg_test.sh` proves the add-a-table, corrupt-backup and wrong-release cases on a real Postgres (also in CI).
Limits, stated: migrations are forward-only and assumed additive, so old code normally runs on the newer schema; a restore loses everything written after the backup was taken (extensions, roles and database-level grants are not part of the dump and are left untouched). The first deploy has no rollback target (the script says so). Rollback does not restore `.env`: copy `.env.previous` back by hand when a settings change caused the failure.

## 7. Day-two checks

- `scripts/deploy/deploy.sh smoke` (read-backs against the running release), `cat .deploy/running` (the digests the containers run).
- Compose on the VM only through the script's passthrough, which adds the release override and the running release's digests (a bare Compose call has build sections and would build from source): `scripts/deploy/deploy.sh compose ps` (all healthy), `scripts/deploy/deploy.sh compose logs --tail 50 agent_a`. A restart by hand: `scripts/deploy/deploy.sh compose up -d --no-deps --wait core knowledge agent_a agent_b` (this is also the command the script prints when a restart after a failed restore fails).
- Caddy certificate trouble: DNS must already point at the VM; `scripts/deploy/deploy.sh compose logs caddy`.
- A stack that will not start with an empty `SITE_ADDRESS` is intended (D-049).

## 8. Proven locally (not on a VM)

D-072 path (build once, deploy by digest): images built with the CI bake command, pushed to a throwaway local registry, then `--local up` of two releases and a `rollback` to the first release's digests on real Docker, with the images removed from the host first so every one was pulled; plus an image inspection showing no `.env` file or planted secret in any image (control: the old ignore rules let two nested `.env` files in). The GHCR push itself is proven by the `images` job on the pull request (tag `pr-<n>`). See `build-log/reports/ghcr-promote.md`.

Not run yet: `release.yml` (first run is the r-00 rehearsal in section 3), the GHCR login over SSH and `gh release download` in `deploy.yml` (first run with `r-01`).

Earlier (build on the VM, superseded by D-072):

`scripts/deploy/deploy.sh --local up` ran end to end (fix round 1 adds the backup, in-flight gate, version read-backs, a forced failure and a rollback, see the report) against Compose (`COMPOSE_PROJECT_NAME=cs-deploy`, `EDGE_SUBNET=172.31.88.0/24`, `SITE_ADDRESS=:80`, host port 8088, same `compose.yaml` images and commands as the VM; the overlay only changes the published port). Not proven: real TLS, a real domain, the SSH and workflow path, a first-time VM build. See `build-log/reports/laneA3-deploy-report.md`.
