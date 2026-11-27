"""What: the composition root of the turn path: models from data/models.yaml (ModelRegistry), the seven tools on
the token-split core and knowledge clients, the create_agent graph on the app's checkpointer, the TurnRunner
(steps 10, 11, 13, 14) with StructuredInput and the Confirmer on the commit token, and one Ingress (Postgres turn
store, Redis turn lock, voice call keys, transcript merger, trajectory writer) shared by /turns and voice/ask.
Use when: the app lifespan calls wire_app once after its pool, Redis, HTTP client, TrajectoryWriter and checkpointer
are open, then assert_wired; tests call build_agent_runtime and build_ingress with fakes.
Inputs: Settings (CORE_URL, KNOWLEDGE_URL, CORE_TOKEN_AGENT, KNOWLEDGE_TOKEN_AGENT, models path, replica name, fault
flag), the commit token (read only inside CoreCommitClient.from_env), app.state (pg_pool, redis, http, checkpointer,
trajectory, voice).
Output: AgentRuntime, build_agent_runtime, build_ingress, wire_app, PENDING_HOOKS, wiring_gaps, assert_wired.
Side effects: wire_app sets app.state.ingress and app.state.voice.ingress; reads models.yaml; nothing connects.
Role: wiring only (CS-E-03.S7 composition, CS-E-08.S5 wiring parts 1 and 2). CS-E-03.S7.T9: Ingress runs
the step 9a PIN scan before any body, and the typed-PIN refusal (TypedPinRefusal) is wired here.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import httpx
from fastapi import FastAPI

from agent.assembly import build_agent_from_registry
from agent.clients.core_agent import CoreAgentClient
from agent.clients.knowledge import KnowledgeClient
from agent.ingress import CallKeys, Ingress, MergeTranscript, PgTurnStore, RedisTurnLock, TurnLock
from agent.pin_guard import TypedPinRefusal
from agent.settings import Settings
from agent.structured.confirm import Confirmer, CoreCommitClient, agent_ticket_opener
from agent.structured.handler import StructuredInput
from agent.tools.catalog import build_tools
from agent.turn_body import PgSessionFlags, SessionFlags, TurnRunner

log = logging.getLogger("agent.composition")

# Placeholders still in the production path, by design until their PRs merge. The startup check logs these as
# warnings; any other gap fails startup. A test keeps this list equal to middleware.stubs.STUB_NAMES.
PENDING_HOOKS: dict[str, str] = {
    "GroundingMiddleware": "Gate 2 stub until CS-E-02.S4.T2 (PR #52)",
}


@dataclass(frozen=True)
class AgentRuntime:
    """What: the built agent and what it was built from. Use when: build_ingress, tests.
    Inputs: compiled agent, tools, core client, knowledge client, model registry (prices for cost_of).
    Output: value. Side effects: none. Role: one bundle per process."""

    agent: Any
    tools: list[Any]
    core: CoreAgentClient
    knowledge: KnowledgeClient
    registry: Any


def build_agent_runtime(cfg: Settings, *, http: httpx.AsyncClient, checkpointer: Any,
                        registry: Any = None) -> AgentRuntime:
    """What: clients, tools and the compiled agent. Use when: wire_app; tests with a fake registry.
    Inputs: settings, the shared HTTP client, an entered checkpointer, a registry (default: ModelRegistry over
    cfg.models_path). Output: AgentRuntime. Side effects: reads models.yaml when no registry is given; no network.
    Role: the create_agent assembly with real dependencies (agent and agent_fallback roles, D-024)."""
    if registry is None:
        from agent.models import ModelRegistry, load_models_config
        registry = ModelRegistry(load_models_config(cfg.models_path))
    core = CoreAgentClient(cfg.core_url, cfg.core_token_agent, http=http, demo_faults_enabled=cfg.demo_faults_enabled)
    knowledge = KnowledgeClient(cfg.knowledge_url, cfg.knowledge_token_agent, http=http)
    tools = build_tools(core, knowledge)
    agent = build_agent_from_registry(registry, checkpointer=checkpointer, tools=tools)
    return AgentRuntime(agent=agent, tools=tools, core=core, knowledge=knowledge, registry=registry)


def build_ingress(runtime: Any, *, store: PgTurnStore | Any, lock: TurnLock, served_by: str, commit: CoreCommitClient,
                  flags: SessionFlags | None, call_keys: CallKeys | None = None,
                  merge_transcript: MergeTranscript | None = None, **extra: Any) -> Ingress:
    """What: the one Ingress for chat and voice, with the TurnRunner body: StructuredInput (verification card,
    ticket_offer, confirm press, strict yes or no) on the agent client, the Confirmer on the commit client, and
    last_turn from the same turn store (so a typed yes after a delivered read-back commits).
    Use when: wire_app; tests with memory stores. Inputs: AgentRuntime (agent, core, registry), turn store (with
    last_turn), turn lock, replica name, commit client, session flags, voice call keys, transcript merger, extra
    Ingress keyword arguments (trajectory). Output: Ingress. Side effects: none. Role: wiring."""
    confirmer = Confirmer(commit, agent_ticket_opener(runtime.core))
    structured = StructuredInput(runtime.core, confirmer, flags=flags, last_turn=store.last_turn)
    body = TurnRunner(runtime.agent, runtime.core, structured=structured, flags=flags,
                      cost_of=runtime.registry.cost_for_model)
    return Ingress(store, lock, served_by=served_by, run_agent=body, call_keys=call_keys,
                   merge_transcript=merge_transcript,
                   refuse_typed_pin=TypedPinRefusal(runtime.agent, flags), **extra)


def wire_app(app: FastAPI, cfg: Settings) -> Ingress | None:
    """What: build the production Ingress from app.state and attach it to /turns (app.state.ingress) and voice/ask
    (app.state.voice.ingress). Use when: the app lifespan, after the clients, the TrajectoryWriter and the
    checkpointer are open. Inputs: app (state: checkpointer, pg_pool, redis, http, trajectory, voice), settings.
    Output: the Ingress, or None when it cannot be built (no checkpointer, models.yaml missing or invalid,
    commit token unset): logged, and assert_wired then stops startup. Side effects: sets app.state.ingress and
    voice.ingress. Role: the only place the production adapters meet."""
    from agent.voice import PgVoiceStore, RedisCallKeys, TranscriptMerger
    state = app.state
    state.ingress = None
    if state.checkpointer is None:
        log.error("no checkpointer: turns are off (POST /turns and voice/ask answer 503)")
        return None
    try:
        runtime = build_agent_runtime(cfg, http=state.http, checkpointer=state.checkpointer)
        commit = CoreCommitClient.from_env(cfg.core_url, http=state.http, demo_faults_enabled=cfg.demo_faults_enabled)
    except Exception:  # noqa: BLE001 - logged here; assert_wired turns the missing Ingress into a failed startup
        log.exception("agent runtime did not build (models %s, or the commit token unset): turns are off",
                      cfg.models_path)
        return None
    for name, value in ("CORE_TOKEN_AGENT", cfg.core_token_agent), ("KNOWLEDGE_TOKEN_AGENT", cfg.knowledge_token_agent):
        if not value:
            log.error("%s is not set: every tool call will be refused", name)
    extra = {"trajectory": state.trajectory} if getattr(state, "trajectory", None) is not None else {}
    store = PgTurnStore(state.pg_pool)
    ingress = build_ingress(runtime, store=store, lock=RedisTurnLock(state.redis), served_by=cfg.served_by,
                            commit=commit, flags=PgSessionFlags(state.pg_pool), call_keys=RedisCallKeys(state.redis),
                            merge_transcript=TranscriptMerger(PgVoiceStore(state.pg_pool)), **extra)
    state.ingress = ingress
    if getattr(state, "voice", None) is not None:
        state.voice.ingress = ingress
    return ingress


def wiring_gaps(app: Any) -> tuple[list[str], list[str]]:
    """What: list what the turn path is missing: (gaps, pending). Gaps are real wiring faults (no Ingress, no
    trajectory writer, no voice relay hooks, voice without the shared Ingress, a body that is not the TurnRunner with
    StructuredInput and last_turn); pending are the named PENDING_HOOKS still present.
    Use when: assert_wired at startup; tests. Inputs: app (state.ingress, state.voice). Output: two lists of
    messages. Side effects: none. Role: the startup tripwire (review PR #48 I2: nothing fails silently)."""
    from agent.middleware.stubs import STUB_NAMES
    state = app.state
    ingress = getattr(state, "ingress", None)
    gaps: list[str] = []
    if ingress is None:
        gaps.append("ingress: POST /turns and voice/ask would answer 503")
    else:
        if getattr(ingress, "trajectory", None) is None:
            gaps.append("trajectory: no TrajectoryWriter on the Ingress, agent.turn_events would stay empty")
        if ingress.call_keys is None or ingress.merge_transcript is None:
            gaps.append("voice relay: the Ingress has no call keys or transcript merger, voice/ask would answer 503")
        voice = getattr(state, "voice", None)
        if voice is not None and voice.ingress is not ingress:
            gaps.append("voice.ingress: voice/ask is not on the shared Ingress")
        body = ingress.run_agent
        structured = getattr(body, "structured", None)
        if not isinstance(body, TurnRunner) or not isinstance(structured, StructuredInput):
            gaps.append("turn body: not the TurnRunner with StructuredInput, cards and confirms would not work")
        elif structured.last_turn is None:
            gaps.append("last_turn: StructuredInput has no last_turn, every typed yes would repeat the read-back")
    pending = [name for name in PENDING_HOOKS if name in STUB_NAMES]
    return gaps, pending


def assert_wired(app: Any) -> None:
    """What: fail startup when a turn-path hook is not wired; log each named pending placeholder.
    Use when: the app lifespan, right after wire_app (production path only). Inputs: app.
    Output: none; raises RuntimeError naming every gap. Side effects: logs. Role: fail loudly in dev, CI and VM."""
    gaps, pending = wiring_gaps(app)
    for name in pending:
        log.warning("wiring pending: %s (%s)", name, PENDING_HOOKS[name])
    if gaps:
        for g in gaps:
            log.error("wiring gap: %s", g)
        raise RuntimeError("turn path not wired: " + "; ".join(gaps))
