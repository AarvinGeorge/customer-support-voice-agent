"""What: the composition root of the turn path: models from data/models.yaml (ModelRegistry), the seven tools on
the token-split core and knowledge clients, the create_agent graph on the app's checkpointer, the TurnRunner
(steps 10, 11, 13, 14) with StructuredInput and the Confirmer on the commit token, and one Ingress (Postgres turn
store, Redis turn lock, voice call keys, transcript merger, trajectory writer) shared by /turns and voice/ask.
Use when: the app lifespan calls wire_app once after its pool, Redis, HTTP client, TrajectoryWriter and checkpointer
are open, then assert_wired; tests call build_agent_runtime and build_ingress with fakes.
Inputs: Settings (CORE_URL, KNOWLEDGE_URL, CORE_TOKEN_AGENT, KNOWLEDGE_TOKEN_AGENT, models path, replica name, fault
flag), the commit token (read only inside CoreCommitClient.from_env), app.state (pg_pool, redis, http, checkpointer,
trajectory, voice).
Output: AgentRuntime, build_agent_runtime, build_ingress, wire_app, gate2_wired, pending_safety, wiring_gaps,\nassert_wired.
Side effects: wire_app sets app.state.ingress and app.state.voice.ingress; reads models.yaml; nothing connects.
Role: wiring only (CS-E-03.S7 composition, CS-E-08.S5 wiring parts 1 and 2). CS-E-03.S7.T9: Ingress runs
the step 9a PIN scan before any body, and the typed-PIN refusal (TypedPinRefusal) is wired here.
"""
from __future__ import annotations

import importlib
import logging
from dataclasses import dataclass
from typing import Any

import httpx
from fastapi import FastAPI

from agent.assembly import build_agent_from_registry
from agent.clients.core_agent import CoreAgentClient
from agent.clients.knowledge import KnowledgeClient
from agent.escalation import EscalationHolds, Escalator
from agent.ingress import CallKeys, Ingress, IngressInput, MergeTranscript, PgTurnStore, RedisTurnLock, TurnLock
from agent.settings import Settings, secret_problem
from agent.pin_guard import TypedPinRefusal
from agent.structured.confirm import Confirmer, CoreCommitClient, agent_ticket_opener
from agent.structured.handler import StructuredInput
from agent.tools.catalog import build_tools
from agent.turn_body import PgSessionFlags, SessionFlags, TurnRunner

log = logging.getLogger("agent.composition")

# Safety pieces still pending by design until their PRs merge (pending_safety detects each one from the code, so the
# line disappears by itself when the real piece lands). Local dev logs them as warnings; production config
# (Settings.production) fails startup on them: block the shared URL, not the lanes' local stacks (review PR #58 I4).
GATE2_PENDING = ("Gate 2 verifier: the agent's GroundingMiddleware has no grounding verifier (CS-E-02.S4.T2); "
                 "drafts would stream with no grounding check")
PIN_PENDING = ("PIN scanner: step 9a is {state} (CS-E-03.S7.T9, PR #57); a typed PIN would reach the thread, "
               "turn_events and LangSmith")
_PIN_PROBE = "my PIN is 4821"


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
                  merge_transcript: MergeTranscript | None = None, holds: EscalationHolds | None = None,
                  **extra: Any) -> Ingress:
    """What: the one Ingress for chat and voice, with the TurnRunner body: StructuredInput (verification card,
    ticket_offer, confirm press, strict yes or no) on the agent client, the Confirmer on the commit client, and
    last_turn from the same turn store (so a typed yes after a delivered read-back commits).
    Use when: wire_app; tests with memory stores. Inputs: AgentRuntime (agent, core, registry), turn store (with
    last_turn), turn lock, replica name, commit client, session flags, voice call keys, transcript merger, the
    escalation hold store (D-070; production passes the same PgSessionFlags), extra Ingress keyword arguments
    (trajectory). Output: Ingress. Side effects: none. Role: wiring."""
    confirmer = Confirmer(commit, agent_ticket_opener(runtime.core))
    escalator = Escalator(runtime.core, holds=holds)  # one escalator: the loop, step 13 and the card share the hold
    structured = StructuredInput(runtime.core, confirmer, flags=flags, last_turn=store.last_turn, escalator=escalator)
    body = TurnRunner(runtime.agent, runtime.core, structured=structured, flags=flags,
                      cost_of=runtime.registry.cost_for_model, escalator=escalator)
    return Ingress(store, lock, served_by=served_by, run_agent=body, call_keys=call_keys,
                   merge_transcript=merge_transcript,
                   refuse_typed_pin=TypedPinRefusal(runtime.agent, flags), flags=flags, **extra)


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
    state.wiring_error = None
    if state.checkpointer is None:
        state.wiring_error = "no checkpointer (agent database not reachable at startup)"
        log.error("no checkpointer: turns are off (POST /turns and voice/ask answer 503)")
        return None
    try:
        runtime = build_agent_runtime(cfg, http=state.http, checkpointer=state.checkpointer)
        commit = CoreCommitClient.from_env(cfg.core_url, http=state.http, demo_faults_enabled=cfg.demo_faults_enabled)
    except Exception as exc:  # noqa: BLE001 - logged here; assert_wired turns the missing Ingress into a failed start
        log.exception("agent runtime did not build (models %s, or the commit token unset): turns are off",
                      cfg.models_path)
        state.wiring_error = f"{type(exc).__name__}: {exc}"  # messages name variables and paths, never secret values
        return None
    extra = {"trajectory": state.trajectory} if getattr(state, "trajectory", None) is not None else {}
    store = PgTurnStore(state.pg_pool)
    flags = PgSessionFlags(state.pg_pool)  # also the D-070 escalation hold store (agent.sessions)
    ingress = build_ingress(runtime, store=store, lock=RedisTurnLock(state.redis), served_by=cfg.served_by,
                            commit=commit, flags=flags, call_keys=RedisCallKeys(state.redis),
                            merge_transcript=TranscriptMerger(PgVoiceStore(state.pg_pool)), holds=flags, **extra)
    state.ingress = ingress
    if getattr(state, "voice", None) is not None:
        state.voice.ingress = ingress
    return ingress


def _pin_scan_state() -> str | None:
    """What: whether ingress step 9a runs a real PIN scan: agent.pin_guard imports, agent.ingress calls its
    scan_input, and scan_input redacts a probe PIN. Use when: pending_safety. Inputs: none.
    Output: None when real, else "absent" or "a no-op". Side effects: imports a module. Role: detector."""
    import agent.ingress as ingress_module
    try:
        pin_guard = importlib.import_module("agent.pin_guard")
    except ImportError:
        return "absent"
    scan = getattr(pin_guard, "scan_input", None)
    if scan is None or getattr(ingress_module, "scan_input", None) is not scan:
        return "absent"
    from contracts.redact import RedactContext
    try:
        probe = IngressInput(session_id="probe", visitor_id="probe", client_turn_id="probe", mode="chat",
                             input_kind="text", text=_PIN_PROBE)
        out = scan(probe, RedactContext())
        text = getattr(getattr(out, "inp", None), "text", None)
    except Exception:  # noqa: BLE001 - a scanner that cannot scan is treated as missing
        return "a no-op"
    return None if text is not None and "4821" not in text else "a no-op"


def gate2_wired(agent: Any) -> bool:
    """What: true when the compiled agent runs the real GroundingMiddleware with a grounding verifier attached.
    Reads the graph node "GroundingMiddleware.after_model" and the middleware bound to it.
    Use when: pending_safety. Inputs: the compiled agent (or anything). Output: bool; False for any shape it cannot
    read, so an unknown agent counts as pending (fail closed). Side effects: none. Role: Gate 2 detector."""
    from agent.middleware.grounding import GroundingMiddleware
    try:
        node = agent.nodes["GroundingMiddleware.after_model"]
        mw = getattr(getattr(node.bound, "afunc", None), "__self__", None)
    except Exception:  # noqa: BLE001 - an agent this code cannot inspect is treated as not wired
        return False
    return isinstance(mw, GroundingMiddleware) and mw.verifier is not None


def pending_safety(agent: Any = None) -> list[str]:
    """What: name each safety piece that is not live: the Gate 2 verifier (the agent's GroundingMiddleware has no
    verifier, or the agent cannot be inspected) and the step 9a PIN scanner (module absent or a no-op).
    Use when: wiring_gaps. Inputs: the compiled agent of the turn body (None: Gate 2 counts as pending).
    Output: messages (empty when both are real). Side effects: imports. Role: detector (review PR #58 I4)."""
    out = [] if gate2_wired(agent) else [GATE2_PENDING]
    pin = _pin_scan_state()
    if pin is not None:
        out.append(PIN_PENDING.format(state=pin))
    return out


_SECRETS = (("CORE_TOKEN_AGENT", "core_token_agent", "every tool call would get 401"),
            ("KNOWLEDGE_TOKEN_AGENT", "knowledge_token_agent", "search_kb would get 401"),
            ("SESSION_SIGNING_KEY", "session_signing_key", "nobody could sign in"))


def wiring_gaps(app: Any) -> tuple[list[str], list[str]]:
    """What: list what the turn path is missing: (gaps, pending). Gaps are real wiring faults (an empty or
    placeholder secret, no Ingress with its cause, no trajectory writer, no voice relay hooks, voice without the
    shared Ingress, a body that is not the TurnRunner with StructuredInput, last_turn, session flags and cost_of);
    pending are the safety placeholders from pending_safety. The commit token is checked by
    CoreCommitClient.from_env, its only reader, and shows up here as the ingress cause.
    Use when: assert_wired at startup; tests. Inputs: app (state.settings, state.ingress, state.voice,
    state.wiring_error). Output: two lists of messages (variable names, never values). Side effects: none.
    Role: the startup tripwire (review PR #48 I2: nothing fails silently)."""
    state = app.state
    gaps: list[str] = []
    cfg = getattr(state, "settings", None)
    if cfg is None:
        gaps.append("settings: app.state has no Settings, secrets cannot be checked")
    else:
        for name, field, effect in _SECRETS:
            problem = secret_problem(getattr(cfg, field))
            if problem is not None:
                gaps.append(f"secret {name} is {problem}: {effect}")
    ingress = getattr(state, "ingress", None)
    if ingress is None:
        cause = getattr(state, "wiring_error", None)
        gaps.append("ingress: POST /turns and voice/ask would answer 503" + (f" (cause: {cause})" if cause else ""))
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
        else:
            if structured.last_turn is None:
                gaps.append("last_turn: StructuredInput has no last_turn, every typed yes would repeat the read-back")
            if body.flags is None:
                gaps.append("session flags: the TurnRunner has no flags, awaiting_verification would never be set")
            if body.escalator.holds is None or structured.escalator is not body.escalator:
                gaps.append("escalation holds: an unverified escalation would not be held for the verification card "
                            "(D-070), or the loop and the card use different escalators")
            if body.cost_of is None:
                gaps.append("cost_of: the TurnRunner has no price lookup, cost_usd_est would stay 0")
    return gaps, pending_safety(getattr(getattr(ingress, "run_agent", None), "agent", None))


def assert_wired(app: Any) -> None:
    """What: fail startup when a turn-path hook or secret is not wired; for each pending safety piece, warn in local
    dev and fail in production config (Settings.production: a public SITE_ADDRESS or AGENT_ENV=production).
    Use when: the app lifespan, right after wire_app (production path only). Inputs: app.
    Output: none; raises RuntimeError naming every gap or every pending piece. Side effects: logs.
    Role: fail loudly in dev, CI and VM; never serve the shared URL without Gate 2 and the PIN scan."""
    gaps, pending = wiring_gaps(app)
    cfg = getattr(app.state, "settings", None)
    production = cfg is None or cfg.production  # no settings: fail closed
    for p in pending:
        (log.error if production else log.warning)("wiring pending: %s", p)
    if gaps:
        for g in gaps:
            log.error("wiring gap: %s", g)
        raise RuntimeError("turn path not wired: " + "; ".join(gaps))
    if pending and production:
        raise RuntimeError("production config refuses to start without: " + "; ".join(pending))
