# Quillstack support agent

A customer support agent for Quillstack, a fictional B2B SaaS company. A customer can chat or talk (live voice) with one agent that verifies who they are, answers from a help center with confidence checks, and takes account actions such as refunds under rules enforced in code. Chat and voice call the same stateless agent service, so the tools, guardrails and session memory are identical. An in-app drawer shows what happened on each turn.

> Status: this README is a skeleton (CS-E-08.S1.T1). Sections marked TODO name the task that fills them. Setup below describes the stack as of this commit; steps that depend on unmerged work say so.

## Contents

1. [Setup](#setup)
2. [Architecture](#architecture)
3. [Why one agent, not many](#why-one-agent-not-many)
4. [Observability drawer and LangSmith](#observability-drawer-and-langsmith)
5. [Showcase](#showcase)
6. [Production posture](#production-posture)

## Setup

Prerequisites:

- Docker with Compose v2 (tested with 2.37).
- Python 3 (only for the key helper `scripts/keys.py`; it uses the standard library).
- Provider keys: Anthropic, OpenAI, LangSmith. A Notion token is only needed to re-export the help articles; the exported articles are already in `data/articles`.

From a fresh clone:

```sh
# 1. Secrets. Each command asks at a hidden prompt; values are never printed or passed as arguments.
python3 scripts/keys.py set       # paste the provider keys (writes .env, mode 600)
python3 scripts/keys.py gen       # generate the internal tokens, passwords and database URLs
python3 scripts/keys.py verify    # one read-only call per provider key

# 2. Build and start the stack. Migrations run first as one-shot jobs (core, knowledge, agent),
#    then every service waits for its dependencies to report healthy.
docker compose up -d --build
docker compose ps                 # wait until the long-running services show "healthy"

# 3. Migrations already ran in step 2 (the migrate_* jobs). After you change a file under services/*/migrations,
#    run `docker compose up -d --build` again so the images rebuild and the jobs rerun.
# TODO CS-E-01.S3.T2 (PR in review): scripts/migrate.sh, a one-command rerun, once it merges.

# 4. Load the help articles into the knowledge service (needs a real OPENAI_API_KEY for embeddings).
docker compose run --rm ingest_knowledge     # TODO CS-E-02.S1: ingest job is in review, not on main yet

# 5. Reseed the demo accounts (personas such as Priya, Marcus and Dana). Needs DEMO_RESEED=true.
# TODO CS-E-09.S4.T1: reseed CLI is not built yet; command to be written here when it lands.

# 6. Open the app.
#    http://localhost:3000   (Caddy; EDGE_PORT in .env sets the host port, keep SITE_ORIGIN in step with it)
```

What a healthy stack looks like on this commit: `postgres`, `redis`, `core`, `knowledge`, `agent_a`, `agent_b` and `web` show `healthy`, `caddy` is running (it has no health check) and the three `migrate_*` jobs show `Exited (0)`. Through Caddy, `/` and `/console` answer 200 and `/api/readyz` answers 200. On this commit the three Python services are stubs that answer `/readyz` without calling a provider, so dummy keys still reach healthy. Once the real agent lands, check that readiness still holds without a valid key before relying on this.

Running several stacks on one machine: give each its own `COMPOSE_PROJECT_NAME`, `EDGE_SUBNET` (and the same value in `FORWARDED_ALLOW_IPS`) and `EDGE_PORT` (with `SITE_ORIGIN=http://localhost:<EDGE_PORT>`) in `.env`, so networks and the host port do not collide.

Local development adds `compose.override.yaml` automatically (source mounted, uvicorn reload). On the VM use `docker compose -f compose.yaml up -d` with `SITE_ADDRESS` set to the public domain (required there: an empty value makes Caddy refuse to start) and `SITE_ORIGIN=https://<domain>`. TODO CS-E-08.S4.T2: full deploy instructions.

Repository layout:

| Path | Holds |
|---|---|
| `services/agent`, `services/core`, `services/knowledge` | The three FastAPI services (Python 3.12+, one `pyproject.toml` each) |
| `packages/contracts` | Typed wire contract shared by backend and frontend (Pydantic plus generated TypeScript types) |
| `apps/web` | Next.js app: chat, voice panel, observability drawer |
| `data/` | `policy.yaml` (rules), help articles; `models.yaml` (model registry) and seeds are TODO (CS-E-03.S8.T1, CS-E-09.S4.T1) |
| `infra/`, `compose.yaml`, `scripts/` | Container build files, Postgres init, stack definition, helpers |

## Architecture

The diagram shows the designed system. Under the thin-slice decision (D-035) some elements are **designed, not built**. They are tagged `[D]` in the diagram and listed after it; `[T]` marks a time-boxed item. Everything else is in the build.

```
┌────────────────────────────────────────────────────────────────────────────────────────────────┐
│ BROWSER  apps/web (Next.js 16.4, React 19.3, Zustand, shadcn/ui on Base UI); holds no secret   │
│ Chat + step trajectory   |   Voice panel (bars; [T] orb)   |   Drawer (metrics + panel)        │
└────────────────────────────────────────────────────────────────────────────────────────────────┘
   │ HTTPS + SSE, same origin          │ WebRTC audio (client secret minted by the agent)
   │ qs_access cookie on /api/*        ▼
   │                          ┌──────────────────────────────────────────────────────────────────┐
   │                          │ OPENAI REALTIME (ears and mouth)                                 │
   │                          │ one tool: ask_support_agent(question). The browser relays the    │
   │                          │ call to POST /api/sessions/{sid}/voice/ask and returns `say`.    │
   │                          └──────────────────────────────────────────────────────────────────┘
   ▼
┌────────────────────────────────────────────────────────────────────────────────────────────────┐
│ CADDY  TLS, one domain, the only public service, SSE unbuffered                                │
│ "/" -> web container (Next.js server, no secret)        "/api" -> agent_a, agent_b             │
└────────────────────────────────────────────────────────────────────────────────────────────────┘
   ▼ /api
┌────────────────────────────────────────────────────────────────────────────────────────────────┐
│ AGENT SERVICE  FastAPI, stateless, replicas agent_a + agent_b               KARC: COORDINATE   │
│ ingress code (verification, confirmation) -> create_agent loop + middleware                    │
│   -> SSE adapter -> turn_events writer                                                         │
│ also: voice token mint, voice/ask, transcript post, chunk proxy; [D] voice usage post          │
│ [D] ops router (/api/operator/*), [D] trace proxy (/api/traces/{trace_id}), [D] step-up        │
└────────────────────────────────────────────────────────────────────────────────────────────────┘
│ CORE_TOKEN_AGENT         │ CORE_TOKEN_COMMIT        │ [D] CORE_TOKEN_OPS  │ KNOWLEDGE_TOKEN_AGENT
│ caller agent_tools       │ caller agent_commit      │ caller ops          │ caller agent
│ tool wrappers, ingress   │ ingress/confirm.py only  │ ops router only     │ search_kb wrapper,
│ verification, escalation │ commit, submit, cancel,  │ + X-Operator-Token  │ chunk proxy
│ code: reads, verify,     │ action status            │ approvals, tickets  │ search, chunks.read
│ prepare, tickets         │                          │                     │
│ [D] prefs                │                          │                     │
▼                          ▼                          ▼                     ▼
┌──────────────────────────────────────────────────────────┐  ┌──────────────────────────────────┐
│ CORE SERVICE  FastAPI, internal only          KARC: ACT  │  │ KNOWLEDGE SERVICE     KARC: KNOW │
│ identity: verify, claims (session_claims)                │  │ FastAPI, internal only           │
│ accounts + billing reads; [D] notification prefs write   │  │ POST /v1/search: hybrid (vector  │
│ policy rules read from data/policy.yaml                  │  │   + full-text, RRF), k = 5,      │
│ actions: prepare, status, commit, submit, cancel         │  │   Gate 1 verdict                 │
│   ([D] A2 billing email, [D] A4)                         │  │ GET /v1/chunks/{chunk_id}        │
│ tickets + approvals ([D] operator endpoints)             │  │ GET /v1/corpus                   │
│ audit_events, append-only                                │  │ ingest CLI job; thresholds in    │
│ GET /v1/me/context (semantic + episodic memory)          │  │   retrieval_calibration.json     │
└──────────────────────────────────────────────────────────┘  └──────────────────────────────────┘
   ▼ agent, core and knowledge each reach Postgres with their own *_app role
┌────────────────────────────────────────────────────────────────────────────────────────────────┐
│ POSTGRES  pgvector/pgvector:pg17. One schema per service (*_owner and *_app roles);            │
│           no service reads another service's schema                                            │
│ agent:     sessions, voice_sessions, turns, turn_events (trajectories), transcript_items,      │
│            LangGraph checkpointer tables (working memory)                 KARC: REMEMBER       │
│ core:      accounts, users, charges, session_claims, verify_attempts, operators,               │
│            pending_actions, action_history, tickets, ticket_events, idempotency,               │
│            audit_events                                                                        │
│ knowledge: articles, chunks (vector(1536) + HNSW cosine, tsvector + GIN), ingest_runs          │
└────────────────────────────────────────────────────────────────────────────────────────────────┘
 REDIS (agent only): rate limit counters | lock:turn:{session_id}, 60 s | voice call dedupe
 OUTBOUND (egress network): Anthropic (agent, grounding_verifier, scope_classifier) |
   OpenAI (agent_fallback, embeddings, Realtime client secret) | LangSmith (traces)
 NETWORKS: edge = caddy, web, agents | backend (internal) = agents, core, knowledge,
   postgres, redis | egress = agents, knowledge
```

Legend, designed but not built (D-035; each has a trigger in the [production posture](#production-posture) table):

- `[D]` ops router and `/api/operator/*`, operator page and Approvals tab, `CORE_TOKEN_OPS` and `X-Operator-Token`.
- `[D]` trace proxy `/api/traces/{trace_id}`: the drawer uses our own SSE usage events plus a LangSmith link instead.
- `[D]` A2 billing email change and step-up verification, A4, notification preferences, the voice usage post.
- `[T]` time-boxed: the voice panel ships with waveform bars first; the ElevenLabs orb is built only if time allows (90 minutes, cut at the Saturday 13:00 voice checkpoint).
- Help articles are authored in Notion and exported once; there is no live Notion sync. Citations link to the public pages.
- LLM judge and the model comparison (evals).

Key choices behind the diagram:

- The agent service is stateless. Session state lives in Postgres keyed by session id, so two replicas serve any request and a restart loses only the in-flight turn.
- Rules live in code, not prompts. The model picks a tool; core decides whether the action is allowed. There is no commit tool: a refund is prepared, read back, and committed only on an explicit yes in a later turn.
- A customer PIN never enters the model's context in chat: a masked card posts it straight to the service.
- Core and knowledge are on an internal network; Caddy is the only public service.
- Every model is a role in `data/models.yaml`, so models can change without code changes.

TODO CS-E-08.S1.T2: chunking choices (showcase 3.2) and the production posture table.

## Why one agent, not many

Showcase 1.10. The agent is one `create_agent` loop with a small set of tools, plus code around it: ingress (routing, verification, confirmation) and middleware (guardrails, usage). That is the simplest design that works for this scope, and the extra moving parts of several agents are not earned yet:

- Each extra agent adds a hand-off where context can be lost, a second prompt to keep consistent, and more latency per turn. Voice makes the latency cost visible.
- Everything an agent needs here is a tool call (read account, search help, prepare action). A second agent would add no capability, only a place for the safety rules to leak out of code.
- One loop gives one trajectory per turn, so the drawer, the trace and the golden-set graders each read one record.
- Chat and voice share the brain by design: the voice model only listens and speaks, and relays one tool call (`ask_support_agent`) to the same agent.

When multi-agent is earned (the trigger, from doc 06 s11): more than about 15 tools, tool-selection errors above 5 percent in the golden set, or a new domain with conflicting instructions. The tools are already grouped by domain so the split would be cheap. Other patterns are decided one by one: routing is in code, the grounding check is an evaluator on a small model, human approval is used for Business refunds, and the evaluator-optimizer retry loop is deferred.

## Observability drawer and LangSmith

Each turn's `turn.completed` event carries usage and gate data. The drawer reads our own SSE events (no extra backend):

- A metric strip: latency, first-status time, time to first token, tokens, cached tokens, estimated cost, models used, fallback flag and which replica served the turn.
- One panel: the tool steps, the retrieval score against its threshold (with calibration status), grounding claims, rule reason codes, the action outcome and any escalation.
- A link from `turn_id` to the full LangSmith trace (`LANGSMITH_PROJECT=cs-voice-agent` by default). The turn id is the LangSmith run id.

TODO CS-E-06.S3.T1: screenshot and the real LangSmith project link once traces exist. The in-app trace detail tab is designed, not built.

## Showcase

One slot per item in the showcase list (planning-docs/04). Each slot says which task produces the content. Slots with `TODO` are filled as that work lands; `scripts/check_readme_showcase.py` fails if a showcase id loses its slot.

### Agentic architecture and tool orchestration

#### 1.1 Three-step tool chain: verify, tier-filtered retrieval, action (S1)
TODO CS-E-04.S5.T1, CS-E-06.S3.T1: step chips plus the drawer trace.

#### 1.2 Agency vs control matrix
TODO CS-E-08.S1.T2: the matrix figure and how tier and role move an action between cells.

#### 1.3 Prepare then commit
TODO CS-E-03.S2.T5: trace showing prepare, read-back, commit.

#### 1.4 Limits enforced in code, not the prompt
TODO CS-E-03.S2.T2: list of the rule-table unit tests.

#### 1.5 Two confidence checks and escalation triggers
TODO CS-E-06.S3.T1: drawer showing score against threshold (S4).

#### 1.6 Off-topic vs not-in-docs vs weak match
TODO CS-E-08.S5.T3: the three S4 questions and results.

#### 1.7 Declines quote the policy reference
TODO CS-E-03.S2.T2: member decline (S3) screenshot.

#### 1.8 Human approval path
TODO CS-E-03.S3.T1: Business refund becomes a Pending approval ticket only after the customer's yes (S3). The operator screen is designed, not built (D-035).

#### 1.9 Prompt-injection case
TODO CS-E-09.S5.T1: golden-set result for instructions inside a help article.

#### 1.10 Why one agent, not many
Written: see [Why one agent, not many](#why-one-agent-not-many). Owner CS-E-08.S1.T1.

### Voice and real-time streaming UX

#### 2.1 Switch chat to voice mid-session (S2)
TODO CS-E-08.S5.T4: evidence that the agent still knows the customer.

#### 2.2 Interruption
TODO CS-E-05.S4.T3: the agent stops and the transcript shows only what was heard.

#### 2.3 Waveform, live transcripts, state indicators
TODO CS-E-05.S4.T3: screenshot or clip.

#### 2.4 Speakable IDs and a cited answer by voice
TODO CS-E-08.S5.T4: "invoice ending 0412" and a cited answer from shared memory (S2).

#### 2.5 One brain: voice calls the same agent, tools and guardrails
Written: see [Architecture](#architecture) (Realtime box and the shared agent service). Owner CS-E-08.S1.T1.

#### 2.6 Measured latency
TODO CS-E-06.S3.T1: chat first-status time and voice time to first audio (p50) from the drawer.

### Code quality and system design

#### 3.1 Architecture diagram
Written: see [Architecture](#architecture). Owner CS-E-08.S1.T1.

#### 3.2 Chunking choices and why
TODO CS-E-08.S1.T2: heading-aware chunks.

#### 3.3 Typed contracts
One event schema shared by backend and frontend: `packages/contracts` holds the Pydantic models and the generated TypeScript types; the frozen wire contract is doc 07. Owner CS-E-08.S1.T1 See `packages/contracts/src/contracts/events.py` for the SSE events..

#### 3.4 Descriptor on every function, CI-enforced
TODO CS-E-01.S2.T1: the six labels (What, Use when, Inputs, Output, Side effects, Role) and the CI check.

#### 3.5 One policy configuration as the source of truth
TODO CS-E-09.S2.T1: `data/policy.yaml` and the test that articles match it.

#### 3.6 Test suite and CI gates
CI is `.github/workflows/ci.yml` (ruff, pyright, pytest, web lint and tests, secret scan, contracts sync, import guard, compose config). TODO CS-E-01.S5.T1: link a green CI run on the freeze tag.

### Production readiness

#### 4.1 Fault injection: tool failure then ticket (S5)
TODO CS-E-07.S1.T4: "no charge was changed" after a faulted commit.

#### 4.2 LLM rate limit switches to the fallback model (S5)
TODO CS-E-07.S1.T4: fallback badge.

#### 4.3 Rate limits per session and IP, voice minute caps, spending caps
TODO CS-E-07.S2.T1: table of limits.

#### 4.4 Stateless service: state in Postgres, two replicas, survives a restart
Built: all session state is in Postgres and Compose runs two agent replicas. Designed, not built: the restart proof on the VM (doc 09 s1.2); trigger: before real production traffic. TODO CS-E-08.S1.T2: this row goes in the production posture table.

#### 4.5 Idempotent commits
TODO CS-E-03.S2.T5: the test that a replayed commit never refunds twice.

#### 4.6 Audit trail
TODO CS-E-03.S3.T1: audit rows for each action and ticket stage change.

#### 4.7 Golden-set results
TODO CS-E-06.S1.T1: pass rate with N, raw escalation counts, judge agreement.

#### 4.8 Cost per conversation and per resolution
TODO CS-E-06.S3.T1: numbers from LangSmith.

#### 4.9 Designed but not built, each with its trigger
TODO CS-E-08.S1.T2: see [Production posture](#production-posture).

## Production posture

TODO CS-E-08.S1.T2: table of every "designed, not built" row from the build plan (section 1.2), each with its trigger.
