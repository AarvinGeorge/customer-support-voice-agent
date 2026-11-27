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

Chunking choices are in [showcase 3.2](#32-chunking-choices-and-why). What is designed but not built, with the trigger for each, is in the [Production posture](#production-posture) table.

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
Chunks follow the help articles' own structure instead of a fixed window. The chunker (`services/knowledge/src/knowledge/ingest/chunker.py`) works in this order:

- Split by heading: the intro, each H2 body and each H3 section become candidate chunks. A section is one idea an agent can quote, so a citation points at one section (with its source link), not at a slice that starts mid-sentence.
- Merge small sections, within limits. The rule (`_merge` in the chunker): a section under 50 tokens joins the previous section only if both sit under the same H2 and apply to the same tiers; with no previous section it joins the next one. It never merges across two H2 sections or across tiers. Most articles are flat lists of H2 sections, so most small sections stay on their own: 13 of the 81 chunks are under 50 tokens (for example `refund-policy#free-plan`, 14 tokens, a heading with one short line).
- Keep a section whole when it is 150 to 400 tokens. Split only a section over 500 tokens, at paragraph or list boundaries and up to 400 tokens; one block over 500 tokens fails the ingest instead of being cut. No overlap between chunks.
- Add a short context header to the text that is embedded (article path, tiers the section applies to, updated date), so a chunk that says "Step 3" still knows which article it belongs to. The header is not counted in the body size.
- Tokens are counted with the cl100k_base encoding. Chunk ids are deterministic, so a re-ingest of an unchanged article changes nothing.

Why 150 to 400: below about 150 tokens a chunk often lacks the condition that makes a rule apply (a tier, a day limit); above 400 one chunk covers several rules and the embedding blurs them. The range is the target, not a padding rule.

What the chunker produced from the 10 articles in `data/articles` (counted from the ingest code, body tokens): 81 chunks; smallest 14 tokens, median 69, mean 79, largest 203 tokens; 5 chunks are in the 150 to 400 band, 76 are under 150, none is over 400. So most chunks are below the target: the articles are short and are written one rule per section, and the chunker keeps a section whole rather than gluing unrelated sections together to reach 150. The practical effect is precise citations; the cost is that a question needing two rules retrieves two chunks. The provisional Gate 1 thresholds are computed on these chunks (D-052, D-053). Merging sections up to the band would be the change to test if the golden set shows answers missing a neighbouring rule.

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
Built: all session state is in Postgres and Compose runs two agent replicas. Designed, not built: the restart proof on the VM (doc 09 s1.2); trigger: before real production traffic. The restart proof is a row in the [Production posture](#production-posture) table.

#### 4.5 Idempotent commits
TODO CS-E-03.S2.T5: the test that a replayed commit never refunds twice.

#### 4.6 Audit trail
TODO CS-E-03.S3.T1: audit rows for each action and ticket stage change.

#### 4.7 Golden-set results
TODO CS-E-06.S1.T1: pass rate with N, raw escalation counts, judge agreement.

#### 4.8 Cost per conversation and per resolution
TODO CS-E-06.S3.T1: numbers from LangSmith.

#### 4.9 Designed but not built, each with its trigger
Written: see the [Production posture](#production-posture) table. Each item has a design reference and the event that would make it worth building.

## Production posture

Built is the thin slice (D-035). The rows below are designed and not built, so each one has a trigger: the observation that would justify the work.

Built ticket posture (D-070): every ticket has a known requester. Core takes the user and account from the session's verified claim, never from the request body, refuses any other request with `403 REQUESTER_UNKNOWN`, and the database CHECK `tickets_requester_known` backs it. When an escalation is needed before the customer has verified, the agent does not open a ticket: it holds the escalation on the session, shows the verification card ("To open a ticket for our team, I need to know who you are first."), and opens the ticket for that person once the card verifies them. While a card is already on screen the escalation does not fire. A locked-out customer, or one who says they do not have their PIN, gets a fixed line pointing to the "How to reach support" help article section and no ticket. `create_ticket` is not offered to the model while unverified. "Designed in" points at the contract (07), the architecture (06), the scope (L1) or a decision (D-nnn) that holds the design.

| Item | Designed in | Trigger |
|---|---|---|
| Tickets for customers who cannot verify (forgotten PIN, lockout) | D-070 | A second identity path exists (email link or SSO sign-in), so such a ticket would have a requester our team can follow up |
| Approvals UI (`/operator` page) | 07 s2.2 operator rows | A real approver queue exists, or a reviewer asks to see the operator step |
| Ops router and core operator endpoints (login, approvals, approve, reject, resolve, close) | 07 s3.5, s2.2 | Same as the approvals UI |
| A2 billing email change and step-up PIN | 07 s3.1 step-up, s3.3; L1 s5 A2 | Next build window after the Loom; needed before any account-takeover-sensitive action ships |
| A4 grace extension | L1 s5 A4; D-018 | `FEATURE_A4` after a green build window |
| Notification preferences (act alone) | 07 s3.2 | Customers ask for preference changes in support volume |
| Live Notion sync, weekly scheduler, Sync now, policy drift check | 07 s16; D-034 | Articles change more than once a week, or a second author edits them |
| LangSmith detail tab and `/api/traces` proxy | 07 s2.2, s2.3 `TraceDetail` | The drawer needs per-run detail beyond our SSE usage events (the observability drawer is in the build plan, doc 09 s1.1, and is not on main yet) |
| LLM judge and model comparison matrix | 06 s19; doc 08 | Free-text cases the code graders cannot score, or a model switch decision |
| Mocked-model load test | 06 s14 | Before real traffic, or to size replicas; the demo is sized for 1 to 2 concurrent conversations (a target, not measured) |
| Mobile layouts | D-031 cut order | Phone users in the audience |
| Spoken verification (rule V, spike 18) | 07 s7.3 rule V, s14 row 18 | Spike 18 passes on real recordings; until then voice is planned to use the verification card (the contract fallback; the voice client is not on main yet) |
| Gate 2 re-ask (spike 2) | 07 s6.4, s14 row 2 | Golden-set evidence that a re-ask helps; the slice is planned to use a fixed clarifying line instead (the grounding middleware is a stub on main) |
| First-token timeout (spike 5) | 07 s6.4, s14 row 5 | First-token stalls observed in real use |
| Turn drop recovery UI | 07 s2.2, s8.1 | Dropped streams observed in real use |
| Page-refresh rehydrate (`GET /api/turns`, `GET /api/sessions/{sid}` snapshot) | 07 s2.2, s2.3 | Dropped streams or refreshes observed in real use |
| Push-to-talk fallback | 06 s17; 07 s2.2 | Voice activity detection misfires in real rooms |
| Voice usage post (`/voice/usage`) | 06 s17; 07 s2.2 | Server-side voice metrics are needed |
| Admission semaphore (8 per replica) | 07 s9 last row | Concurrency above a few users |
| Worst-case timing arithmetic tests (07 s8.3) | 07 s8.3 | Before production traffic |
| Two-replica restart proof on the VM | 07 s8.3 | Before production traffic. Built: all session state is in Postgres and Compose runs two agent replicas |
| Reranker | 06 s14 | The gold chunk is outside the top 3 in more than 2 answerable test cases |
| Multi-document retrieval (query rewriting, GraphRAG) | 06 s14 | 3 or more golden cases that need two articles fail |
| Evaluator-optimizer loop | 06 s14 | A spike plus golden-set evidence that it helps |
| Sub-agents (supervisor with specialists) | 06 s11, s14 | More than about 15 tools, tool-selection errors above 5 percent in the golden set, or a new domain with conflicting instructions |
| Circuit breakers and a spend-cap flag | 06 s14 | Sustained provider errors |
| Voice sideband (server-side channel to the realtime call) | 06 s14 | Server-enforced voice caps, server-trusted telemetry, or detecting ungrounded realtime speech |
| Conversation summarization | 06 s14 | Sessions near the 40-turn cap or about 80k input tokens |
| Chat turn resume beyond replay | 06 s14 | Dropped streams that replay does not cover (proposed) |
| Voice reconnect | 06 s14 | Voice calls dropping in real use (proposed) |
| In progress and Waiting on customer ticket stages | 06 s14; D-018 | Support staff need to track work in flight (proposed) |
| SLA timers | 06 s14 | A response-time promise is made to customers (proposed) |
| App-level daily budget counter | 06 s14 | Provider spend caps alone are not enough (proposed) |
| Event log retention job | 06 s14 | The event tables grow beyond what the demo needs (proposed) |
| Approval withdrawal (customer withdraws a submitted approval) | 06 s14 | Customers ask to retract a request after submitting it (proposed) |
| Free-text PIN scanner (07 s11.1) is defence in depth; the secure card is the control. **Caught:** a 4-digit PIN typed or spoken next to a PIN word (also misspelled or joined: "pni", "pim", "pinn", "mypin", "pin#"), "code" near it, an account id right before or after it, or alone while a PIN is awaited; spoken, grouped and cardinal forms with fillers ("four eight, uh, two one"); Unicode digits and zero-width tricks. **Known misses:** crafted encodings (other-language number words, homophones such as "for eight to one", "48two1", hex, roman numerals) and rare natural forms: a PIN with no PIN word, no account id next to it and no awaited verification (including digits split across two messages with no announcing words), or a misspelling outside the listed set | D-060; 07 s11.1, s7.3 | Before real customer PINs exist: a core-side candidate check (every 4-digit candidate compared with the PIN HMAC, rate limited) |

Triggers marked (proposed) are this README's wording: the architecture doc names the item as deferred but gives no trigger.

Cut order if the build runs behind (06 s14): A4, the approvals button (ticket shown as pending), the LangSmith detail tab, mobile layouts.

