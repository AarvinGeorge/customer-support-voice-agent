"""What: the turn body that Ingress runs under the turn deadline (its `run_agent`): doc 07 s2.4 step 10 repair,
step 11 memory refresh, step 13 structured input (a pluggable handler, agent.structured.handler), step 14 the
agent loop with the code-built turn message, stopped 3 s before the deadline, and step 15 escalation in the 3 s
reserve (agent.escalation: route tickets, limit ticket, deadline path, both models unavailable).
Use when: the app (or a test) builds Ingress(run_agent=TurnRunner(agent, core, structured=...)).
Inputs: the compiled agent, the agent-token core client, an optional structured-input handler, optional session
flags writer (agent.sessions.awaiting_verification).
Output: TurnRunner, StructuredResult, StructuredHandler, TurnPrep, SessionFlags, PgSessionFlags, monotonic_deadline,
voice_user_text.
Budget: steps 10 to 13 are bounded by their own clamps (every core call is min(listed, remaining - 3 s)), so a
commit or submit failure ticket started at the end of step 13 runs in the reserve; only step 14 runs under the loop
timeout (deadline - 3 s), and every escalation ticket is opened after it (doc 07 s8.2, s8.3).
Side effects: thread reads and writes (checkpointer), core calls, events on the turn stream, the session flag.
D-074: after step 15 the first customer message of an unverified session gets the verification card
(agent.first_turn.offer_card) when the turn did not already ask for it.
Role: composition of the ingress steps between the turn row and the final row. Step 12's result (the merged
<voice_transcript> message, from Ingress) is placed before the turn message; step 9a (PIN scan) is CS-E-03.S7.T9.
Wiring part 2 (CS-E-08.S5): reuses the TurnControl Ingress bound (deadline, demo fault), passes cost_of to the SSE
adapter and runs the loop with turn_run_config (LangSmith run id = turn id, fault tags).
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Awaitable, Callable, Protocol

from contracts import limits
from contracts.redact import ours_id
from langchain_core.messages import HumanMessage
from psycopg.types.json import Jsonb

from agent.assembly import run_config, turn_run_config
from agent.clients.core_agent import CoreAgentClient
from agent.escalation import EscalationFacts, Escalator
from agent.first_turn import offer_card
from agent.gates import identity_key
from agent.memory import MemoryRefresh, refresh_memory, repair_dangling
from agent.prompt import TurnContext, build_turn_message
from agent.deadline import TurnBudgetExhausted
from agent.sse import TurnOutcome, TurnStream, stream_agent_turn
from agent.turn_control import TURN_CONTROL, TurnControl, bind_turn_control, current_control, loop_clock

log = logging.getLogger("agent.turn_body")

CostOf = Callable[[str, Any], float]


def voice_user_text(body: Any) -> str:
    """What: the voice turn's user text: the Realtime question, plus the final verbatim transcript when it differs.
    Use when: TurnRunner on a voice turn the model answers. Inputs: VoiceAskRequest (question, user_transcript,
    transcript_final). Output: text. Side effects: none. Role: doc 07 s2.6 row 4."""
    question = body.question.strip()
    said = (body.user_transcript or "").strip()
    if body.transcript_final and said and said.lower() != question.lower():
        return f"{question}\n(the customer said: {said})"
    return question


def customer_words(inp: Any, model_text: str | None) -> str | None:
    """What: the customer's own words this turn, for Gate 1 (CS-E-02.F11). Use when: TurnRunner sets
    TurnControl.customer_text. Inputs: the IngressInput (already PIN-redacted at step 9a), the structured result's
    model_text. Output: text, or None when the turn has no customer-authored text.
    Side effects: none. Role: never a code line (model_text set), never model-authored: in voice the question is written
    by the Realtime model, so only the final user transcript counts; no final transcript gives None (query-only
    scoring). Review PR #86 I1, I5."""
    if model_text is not None:
        return None
    body = getattr(inp, "body", None)
    if inp.mode == "voice" and body is not None and hasattr(body, "question"):
        said = (body.user_transcript or "").strip()
        return said if body.transcript_final and said else None
    text = (inp.text or "").strip()
    return text or None


class SessionFlags(Protocol):
    """What: the agent.sessions verification facts ingress writes. Use when: typing. Inputs: see method.
    Output: none. Side effects: database. Role: port (agent.sessions is the authority, doc 07 s2.4 Counting)."""

    async def set_awaiting_verification(self, session_id: str, awaiting: bool,
                                        verified_user_id: str | None = None) -> None:
        """What: set agent.sessions.awaiting_verification (and the verified user). Use when: verification asked, done or lapsed. Inputs: id, flag, user. Output: none.
        Side effects: UPDATE. Role: port method."""
        ...

    async def set_card_offered(self, session_id: str, offered: bool) -> None:
        """What: set or clear agent.sessions.card_offered_at (D-074). Use when: the first-message offer; the card's
        "I don't have my PIN" press. Inputs: id, flag. Output: none. Side effects: UPDATE. Role: port method."""
        ...


class PgSessionFlags:
    """What: SessionFlags over the agent_app pool, and the EscalationHolds store (agent.escalation, D-070).
    Use when: production. Inputs: psycopg pool.
    Output: set_awaiting_verification, escalation_facts, hold_escalation. Side effects: SQL on agent.sessions
    (reads agent.turns). Role: adapter (one owner of the agent.sessions verification facts)."""

    def __init__(self, pool: Any) -> None:
        """What: keep the pool. Use when: app start. Inputs: pool. Output: none. Side effects: none. Role: constructor."""
        self.pool = pool

    async def set_awaiting_verification(self, session_id: str, awaiting: bool,
                                        verified_user_id: str | None = None) -> None:
        """What: set awaiting_verification (and verified_user_id when given). Use when: a verify request or a
        successful verify. Inputs: session id, flag, user id. Output: none. Side effects: one UPDATE. Role: write."""
        async with self.pool.connection() as conn:
            await conn.execute("UPDATE agent.sessions SET awaiting_verification = %s, "
                               "verified_user_id = COALESCE(%s, verified_user_id) WHERE session_id = %s",
                               (awaiting, verified_user_id, session_id))

    async def set_card_offered(self, session_id: str, offered: bool) -> None:
        """What: set card_offered_at to now (kept if already set) or clear it. Use when: the D-074 offer, the no-PIN
        press. Inputs: session id, flag. Output: none. Side effects: one UPDATE. Role: write (review PR #82 I1, I2)."""
        async with self.pool.connection() as conn:
            await conn.execute("UPDATE agent.sessions SET card_offered_at = CASE WHEN %s THEN "
                               "COALESCE(card_offered_at, now()) ELSE NULL END WHERE session_id = %s",
                               (offered, session_id))

    async def escalation_facts(self, session_id: str) -> EscalationFacts:
        """What: the session's awaiting_verification flag, held escalation and last verified user, read from
        agent.sessions only (the card's explicit state, never inferred from turn history: PR #78 review I-2).
        Use when: an unverified escalation (agent.escalation.no_requester) and a successful verify.
        Inputs: session id. Output: EscalationFacts (empty for an unknown session). Side effects: one SELECT.
        Role: read for D-070."""
        async with self.pool.connection() as conn:
            cur = await conn.execute("SELECT awaiting_verification, held_escalation, verified_user_id "
                                     "FROM agent.sessions WHERE session_id = %s", (session_id,))
            row = await cur.fetchone()
        if row is None:
            return EscalationFacts()
        return EscalationFacts(awaiting=bool(row[0]), held=row[1], verified_user_id=row[2])

    async def hold_escalation(self, session_id: str, held: dict[str, Any] | None) -> None:
        """What: write or clear agent.sessions.held_escalation. Use when: an unverified escalation is held, the held
        ticket was attempted, verification locked, or the customer has no PIN. Inputs: session id, hold or None.
        Output: none. Side effects: one UPDATE. Role: write for D-070."""
        async with self.pool.connection() as conn:
            await conn.execute("UPDATE agent.sessions SET held_escalation = %s WHERE session_id = %s",
                               (Jsonb(held) if held is not None else None, session_id))


@dataclass
class TurnPrep:
    """What: what the runner knows when step 13 starts. Use when: the structured handler.
    Inputs: control, run config, thread values after the memory update, the refresh result, the agent.
    Output: value. Side effects: none. Role: handler input."""

    control: TurnControl
    config: dict[str, Any]
    values: dict[str, Any]
    memory: MemoryRefresh
    agent: Any


@dataclass
class StructuredResult:
    """What: the step 13 decision. Either `outcome` (a deterministic reply: the turn ends, `messages` and `state`
    are written to the thread) or no outcome (continue into the agent loop with `model_text`, `verification`,
    `pending_summary`, `context_stale` (overrides the step 11 marker) and `state` merged into the run input).
    Use when: returned by a StructuredHandler. Inputs: see fields. Output: value. Side effects: none.
    Role: doc 07 s7.3 "produces a deterministic reply or continues into the agent loop"."""

    outcome: TurnOutcome | None = None
    messages: list[Any] = field(default_factory=list)
    state: dict[str, Any] = field(default_factory=dict)
    model_text: str | None = None
    verification: str | None = None
    pending_summary: str | None = None
    context_stale: bool | None = None


StructuredHandler = Callable[[TurnStream, Any, TurnPrep], Awaitable[StructuredResult]]


def monotonic_deadline(deadline_at: datetime, *, clock: Callable[[], float] = loop_clock,
                       now: Callable[[], datetime] = lambda: datetime.now(UTC)) -> float:
    """What: the turn row's wall-clock deadline as a value of `clock` (the running loop's time by default).
    Use when: TurnControl.deadline. Inputs: aware datetime, clock, wall clock. Output: float.
    Side effects: reads both clocks. Role: helper for the s8.2 clamp."""
    return clock() + (deadline_at - now()).total_seconds()


class TurnRunner:
    """What: the AgentBody for Ingress. Use when: one per app. Inputs: agent, core client, structured handler,
    session flags. Output: __call__(stream, inp, row) -> TurnOutcome. Side effects: see module.
    Role: steps 10, 11, 13, 14 in order."""

    def __init__(self, agent: Any, core: CoreAgentClient, *, structured: StructuredHandler | None = None,
                 flags: SessionFlags | None = None, cost_of: CostOf | None = None,
                 escalator: Escalator | None = None, reserve_s: float = limits.ESCALATION_RESERVE_S) -> None:
        """What: keep dependencies. Use when: app start or tests. Inputs: see class; cost_of(model, usage) -> USD
        (ModelRegistry.cost_for_model; None: cost 0); the escalator (default: one on the same core client) and the
        reserve. Output: none. Side effects: none. Role: constructor."""
        self.agent, self.core, self.structured, self.flags, self.cost_of = agent, core, structured, flags, cost_of
        self.escalator = escalator or Escalator(core)
        self.reserve_s = reserve_s

    async def __call__(self, stream: TurnStream, inp: Any, row: Any) -> TurnOutcome:
        """What: run one turn's body with a TurnControl bound once (spike 15). Use when: Ingress step 14.
        Inputs: stream, IngressInput, TurnRow. Output: TurnOutcome. Side effects: see module. Role: entry."""
        bound = current_control()
        if bound is not None and bound.turn_id == row.turn_id:  # Ingress already bound this turn's control
            return await self._run(stream, inp, bound)
        control = TurnControl(session_id=inp.session_id, turn_id=row.turn_id, mode=inp.mode,
                              deadline=monotonic_deadline(row.deadline_at), events=stream,
                              demo_fault=getattr(inp, "demo_fault", None))
        token = bind_turn_control(control)
        try:
            return await self._run(stream, inp, control)
        finally:
            TURN_CONTROL.reset(token)

    async def _run(self, stream: TurnStream, inp: Any, control: TurnControl) -> TurnOutcome:
        """What: the ordered steps. Use when: __call__. Inputs: stream, input, control. Output: outcome.
        Side effects: see module. Role: helper."""
        config = run_config(inp.session_id)
        await repair_dangling(self.agent, config, events=stream)  # step 10, before anything else touches the thread
        values = dict((await self.agent.aget_state(config)).values or {})
        memory = await refresh_memory(self.core, control, values)  # step 11
        values.update(memory.update)
        control.identity = identity_key(values)  # the verification this turn runs under (review PR #71 N3)
        if memory.verified_cleared:  # the verification lapsed: a hold never outlives its lifetime (PR #78 review I-3)
            await self.escalator.hold(control, None)
        result = StructuredResult()
        if self.structured is not None:  # step 13
            result = await self.structured(stream, inp, TurnPrep(control, config, values, memory, self.agent))
        if result.outcome is not None:
            await self.agent.aupdate_state(config, {**memory.update, **result.state, "messages": result.messages})
            return result.outcome
        merged = {**values, **result.state}
        control.identity = identity_key(merged)  # a verification in step 13 changes it before the model runs
        verified = bool(merged.get("verified"))
        stale = result.context_stale if result.context_stale is not None else memory.stale
        ctx = TurnContext(mode="voice" if inp.mode == "voice" else "chat", verified=verified,
                          verification="just_verified" if result.verification == "just_verified" else None,
                          pending=result.pending_summary,
                          card_on_screen=not verified and (control.session_card_offered
                                                           or control.session_awaiting_verification),
                          context_snapshot=merged.get("context_snapshot") if verified else None, context_stale=stale)
        if result.model_text is not None:
            text = result.model_text
        elif inp.mode == "voice" and getattr(inp, "body", None) is not None and hasattr(inp.body, "question"):
            text = voice_user_text(inp.body)
        else:
            text = inp.text or ""
        control.customer_text = customer_words(inp, result.model_text)  # CS-E-02.F11, Gate 1 input
        merged_msg = getattr(inp, "merged_transcript", None)  # step 12: wrapped, untrusted client chatter
        messages = ([HumanMessage(content=merged_msg, id=ours_id("voice_transcript"))] if merged_msg else []) + [build_turn_message(text, ctx)]
        agent_input = {**memory.update, **result.state, "messages": messages}
        loop_config = turn_run_config(inp.session_id, control.turn_id, control.demo_fault)
        outcome = await self._loop(stream, control, agent_input, config, loop_config)  # steps 14 and 15
        if offer_card(stream, control, verified) and self.flags is not None:  # D-074: the card on the first message
            await self.flags.set_card_offered(inp.session_id, True)
        if control.awaiting_verification and self.flags is not None:  # s6.3 step 2: agent.sessions is the authority
            await self.flags.set_awaiting_verification(inp.session_id, True)
        return outcome

    async def _loop(self, stream: TurnStream, control: TurnControl, agent_input: dict[str, Any],
                    config: dict[str, Any], loop_config: dict[str, Any] | None = None) -> TurnOutcome:
        """What: step 14 under the loop timeout (time left minus the 3 s reserve), then step 15 in the reserve (D-058):
        the s6.7 apology only when both models were attempted and failed (the fallback attempt started and did not
        return, including one cut by the loop budget at its own clamp); a loop timeout otherwise, or a model attempt
        skipped by the clamp, takes the deadline path; any other outcome is finished by the escalator (route and
        limit tickets). A model-node exception is logged with the turn id before any line is chosen; one with no
        failed fallback attempt is re-raised to the internal error path.
        Use when: _run. Inputs: stream, control, agent input, config (thread), loop_config (the agent run: LangSmith
        run id = turn id and fault tags; default config). Output: TurnOutcome.
        Side effects: the agent run; escalation tickets and events; an ERROR log on a model-node exception.
        Role: doc 07 s8.2 "tickets on failure paths get one attempt, created inside the 3 s reserve"; D-058."""
        esc = self.escalator
        cm = asyncio.timeout(max(control.remaining_s() - self.reserve_s, 0.0))
        try:
            async with cm:
                outcome = await stream_agent_turn(stream, self.agent, agent_input, loop_config or config,
                                                  control=control, cost_of=self.cost_of)
        except TimeoutError:
            if not cm.expired():  # an inner timeout is an error, not the deadline
                raise
            if control.fallback_unfinished:  # both models attempted; the fallback ended at its clamp (s8.3 voice row)
                return await esc.model_unavailable(stream, control, self.agent, config)
            return await esc.deadline(stream, control, self.agent, config)
        except TurnBudgetExhausted:  # a model attempt was skipped by the clamp (doc 07 s8.2 deadline path)
            return await esc.deadline(stream, control, self.agent, config)
        except Exception:
            if not control.model_node_failed:  # not a model failure: the internal error path
                raise
            log.exception("model node failed session_id=%s turn_id=%s fallback_attempted=%s", control.session_id,
                          control.turn_id, control.fallback_used)
            if not control.fallback_unfinished:  # no failed fallback attempt: not s6.7, the internal error path
                raise
            return await esc.model_unavailable(stream, control, self.agent, config)
        return await esc.finish(stream, control, outcome, self.agent, config)
