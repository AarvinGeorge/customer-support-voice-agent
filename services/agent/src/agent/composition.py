"""What: the composition root of the turn path: models from data/models.yaml (ModelRegistry), the seven tools on
the token-split core and knowledge clients, the create_agent graph on the app's checkpointer, the agent turn body,
and one Ingress (Postgres turn store, Redis turn lock, voice call keys, transcript merger) shared by /turns and
voice/ask.
Use when: the app lifespan calls wire_app once after its pool, Redis, HTTP client and checkpointer are open; tests
call build_agent_runtime and build_ingress with fakes.
Inputs: Settings (CORE_URL, KNOWLEDGE_URL, CORE_TOKEN_AGENT, KNOWLEDGE_TOKEN_AGENT, models path, replica name),
app.state (pg_pool, redis, http, checkpointer, voice).
Output: AgentRuntime, AgentTurnBody, voice_user_text, build_agent_runtime, build_ingress, wire_app,
UNSUPPORTED_INPUT_LINE.
Side effects: wire_app sets app.state.ingress and app.state.voice.ingress; reads models.yaml; nothing connects.
Role: wiring only. The turn body here is the minimal step 14 path (turn message, TurnControl, the SSE adapter);
CS-E-03.S7.T5 and T8 (TurnRunner: repair, memory refresh, structured input) replace it through the
`body_factory` seam of build_ingress, and CS-E-03.S7.T9 (PIN scan) slots in before the thread write.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Callable

import httpx
from fastapi import FastAPI
from langchain_core.messages import HumanMessage

from agent.assembly import build_agent_from_registry, run_config
from agent.clients.core_agent import CoreAgentClient
from agent.clients.knowledge import KnowledgeClient
from agent.ingress import AgentBody, CallKeys, Ingress, IngressInput, MergeTranscript, PgTurnStore, RedisTurnLock, \
    TurnLock, TurnRow, TurnStore
from agent.prompt import TurnContext, build_turn_message
from agent.settings import Settings
from agent.sse import TurnOutcome, TurnStream, stream_agent_turn
from agent.tools.catalog import build_tools
from agent.turn_control import TurnControl, current_control

log = logging.getLogger("agent.composition")

# Until CS-E-03.S7.T8 (structured input) lands, a confirm, verification or ticket_offer body ends with this line.
UNSUPPORTED_INPUT_LINE = "I can't take that kind of input yet. Please type your question instead."
MODEL_INPUT_KINDS = frozenset({"text", "voice"})


@dataclass(frozen=True)
class AgentRuntime:
    """What: the built agent and what it was built from. Use when: build_ingress, tests.
    Inputs: compiled agent, tools, core client, knowledge client. Output: value. Side effects: none.
    Role: one bundle per process."""

    agent: Any
    tools: list[Any]
    core: CoreAgentClient
    knowledge: KnowledgeClient


def voice_user_text(body: Any) -> str:
    """What: the voice turn's user text: the Realtime question, plus the final verbatim transcript when it differs.
    Use when: AgentTurnBody on a voice turn. Inputs: VoiceAskRequest (question, user_transcript, transcript_final).
    Output: text. Side effects: none. Role: doc 07 s2.6 row 4 ("question and the redacted user_transcript")."""
    question = body.question.strip()
    said = (body.user_transcript or "").strip()
    if body.transcript_final and said and said.lower() != question.lower():
        return f"{question}\n(the customer said: {said})"
    return question


def _monotonic(deadline_at: datetime) -> float:
    """What: the turn row's wall-clock deadline as a time.monotonic() value. Use when: TurnControl.deadline.
    Inputs: aware datetime. Output: float. Side effects: reads both clocks. Role: helper for the s8.2 clamp."""
    return time.monotonic() + (deadline_at - datetime.now(UTC)).total_seconds()


class AgentTurnBody:
    """What: the AgentBody Ingress runs under the turn deadline: build the turn message, bind a TurnControl and run
    the create_agent graph through the SSE adapter.
    Use when: build_ingress default body. Inputs: the compiled agent.
    Output: __call__(stream, inp, row) -> TurnOutcome. Side effects: model and tool calls, thread writes.
    Role: doc 07 s2.4 step 14 (minimal; TurnRunner replaces it with steps 10, 11 and 13 in front)."""

    def __init__(self, agent: Any) -> None:
        """What: keep the agent. Use when: build_ingress. Inputs: agent. Output: none. Side effects: none.
        Role: constructor."""
        self.agent = agent

    async def __call__(self, stream: TurnStream, inp: IngressInput, row: TurnRow) -> TurnOutcome:
        """What: one turn. Use when: Ingress step 14. Inputs: stream, IngressInput, TurnRow.
        Output: TurnOutcome (a fixed line for an input kind this body does not handle).
        Side effects: see class. Role: entry."""
        if inp.input_kind not in MODEL_INPUT_KINDS:
            return TurnOutcome.fixed(UNSUPPORTED_INPUT_LINE, end_reason="error")
        config = run_config(inp.session_id)
        values = (await self.agent.aget_state(config)).values or {}
        mode = "voice" if inp.mode == "voice" else "chat"
        text = voice_user_text(inp.body) if mode == "voice" else (inp.text or "")
        ctx = TurnContext(mode=mode, verified=bool(values.get("verified")),
                          context_snapshot=values.get("context_snapshot") if values.get("verified") else None)
        messages: list[Any] = []
        if inp.merged_transcript:  # step 12 result: client voice chatter, already wrapped as untrusted
            messages.append(HumanMessage(content=inp.merged_transcript))
        messages.append(build_turn_message(text, ctx))
        # Ingress binds the turn's TurnControl (deadline, demo fault) once CS-E-07.S1.T2 lands; reuse it then.
        control = current_control() or TurnControl(session_id=inp.session_id, turn_id=row.turn_id, mode=inp.mode,
                                                   deadline=_monotonic(row.deadline_at), events=stream)
        return await stream_agent_turn(stream, self.agent, {"messages": messages}, config, control=control)


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
    return AgentRuntime(agent=agent, tools=tools, core=core, knowledge=knowledge)


def build_ingress(agent: Any, *, store: TurnStore, lock: TurnLock, served_by: str, call_keys: CallKeys | None = None,
                  merge_transcript: MergeTranscript | None = None,
                  body_factory: Callable[[Any], AgentBody] = AgentTurnBody, **extra: Any) -> Ingress:
    """What: the one Ingress for chat and voice. Use when: wire_app; tests with memory stores.
    Inputs: agent, turn store, turn lock, replica name, voice call keys, transcript merger, body factory (seam for
    TurnRunner), extra Ingress keyword arguments (for example trajectory, CS-E-03.S8.T8). Output: Ingress.
    Side effects: none. Role: wiring."""
    return Ingress(store, lock, served_by=served_by, run_agent=body_factory(agent), call_keys=call_keys,
                   merge_transcript=merge_transcript, **extra)


def wire_app(app: FastAPI, cfg: Settings) -> Ingress | None:
    """What: build the production Ingress from app.state and attach it to /turns (app.state.ingress) and voice/ask
    (app.state.voice.ingress). Use when: the app lifespan, after the clients and the checkpointer are open.
    Inputs: app (state: checkpointer, pg_pool, redis, http, voice), settings.
    Output: the Ingress, or None when it cannot be built (no checkpointer, models.yaml missing or invalid): the
    error is logged loudly and both routes answer 503. Side effects: sets app.state.ingress and voice.ingress.
    Role: the only place the production adapters meet."""
    from agent.voice import PgVoiceStore, RedisCallKeys, TranscriptMerger
    state = app.state
    state.ingress = None
    if state.checkpointer is None:
        log.error("no checkpointer: turns are off (POST /turns and voice/ask answer 503)")
        return None
    try:
        runtime = build_agent_runtime(cfg, http=state.http, checkpointer=state.checkpointer)
    except Exception:  # noqa: BLE001 - fail loudly but keep the process up (health, access, sessions still work)
        log.exception("agent runtime did not build from %s: turns are off", cfg.models_path)
        return None
    for name, value in ("CORE_TOKEN_AGENT", cfg.core_token_agent), ("KNOWLEDGE_TOKEN_AGENT", cfg.knowledge_token_agent):
        if not value:
            log.error("%s is not set: every tool call will be refused", name)
    # Hook for CS-E-03.S8.T8: the lifespan that opens a TrajectoryWriter sets app.state.trajectory; pass it on.
    extra = {"trajectory": state.trajectory} if getattr(state, "trajectory", None) is not None else {}
    ingress = build_ingress(runtime.agent, store=PgTurnStore(state.pg_pool), lock=RedisTurnLock(state.redis),
                            served_by=cfg.served_by, call_keys=RedisCallKeys(state.redis),
                            merge_transcript=TranscriptMerger(PgVoiceStore(state.pg_pool)), **extra)
    state.ingress = ingress
    if getattr(state, "voice", None) is not None:
        state.voice.ingress = ingress
    return ingress
