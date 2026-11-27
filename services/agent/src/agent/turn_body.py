"""What: the turn body that Ingress runs under the turn deadline (its `run_agent`): doc 07 s2.4 step 10 repair,
step 11 memory refresh, step 13 structured input (a pluggable handler, agent.structured.handler), step 14 the
agent loop with the code-built turn message, stopped 3 s before the deadline, and step 15 escalation in the 3 s
reserve (agent.escalation: route tickets, limit ticket, deadline path, both models unavailable).
Use when: the app (or a test) builds Ingress(run_agent=TurnRunner(agent, core, structured=...)).
Inputs: the compiled agent, the agent-token core client, an optional structured-input handler, optional session
flags writer (agent.sessions.awaiting_verification).
Output: TurnRunner, StructuredResult, StructuredHandler, TurnPrep, SessionFlags, PgSessionFlags, monotonic_deadline.
Budget: steps 10 to 13 are bounded by their own clamps (every core call is min(listed, remaining - 3 s)), so a
commit or submit failure ticket started at the end of step 13 runs in the reserve; only step 14 runs under the loop
timeout (deadline - 3 s), and every escalation ticket is opened after it (doc 07 s8.2, s8.3).
Side effects: thread reads and writes (checkpointer), core calls, events on the turn stream, the session flag.
Role: composition of the ingress steps between the turn row and the final row. Steps 9a (PIN scan) and 12
(transcript merge) belong to CS-E-03.S7.T9 and CS-E-05.S3.T2 and slot in here.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Awaitable, Callable, Protocol

from contracts import limits

from agent.assembly import run_config
from agent.clients.core_agent import CoreAgentClient
from agent.escalation import Escalator
from agent.memory import MemoryRefresh, refresh_memory, repair_dangling
from agent.prompt import TurnContext, build_turn_message
from agent.sse import ModelUnavailable, TurnOutcome, TurnStream, stream_agent_turn
from agent.turn_control import TURN_CONTROL, TurnControl, bind_turn_control, loop_clock


class SessionFlags(Protocol):
    """What: the agent.sessions verification facts ingress writes. Use when: typing. Inputs: see method.
    Output: none. Side effects: database. Role: port (agent.sessions is the authority, doc 07 s2.4 Counting)."""

    async def set_awaiting_verification(self, session_id: str, awaiting: bool,
                                        verified_user_id: str | None = None) -> None: ...


class PgSessionFlags:
    """What: SessionFlags over the agent_app pool. Use when: production. Inputs: psycopg pool.
    Output: set_awaiting_verification. Side effects: UPDATE agent.sessions. Role: adapter."""

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
                 flags: SessionFlags | None = None, escalator: Escalator | None = None,
                 reserve_s: float = limits.ESCALATION_RESERVE_S) -> None:
        """What: keep dependencies. Use when: app start or tests. Inputs: see class, plus the escalator (default:
        one on the same core client) and the reserve. Output: none. Side effects: none. Role: constructor."""
        self.agent, self.core, self.structured, self.flags = agent, core, structured, flags
        self.escalator = escalator or Escalator(core)
        self.reserve_s = reserve_s

    async def __call__(self, stream: TurnStream, inp: Any, row: Any) -> TurnOutcome:
        """What: run one turn's body with a TurnControl bound once (spike 15). Use when: Ingress step 14.
        Inputs: stream, IngressInput, TurnRow. Output: TurnOutcome. Side effects: see module. Role: entry."""
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
        result = StructuredResult()
        if self.structured is not None:  # step 13
            result = await self.structured(stream, inp, TurnPrep(control, config, values, memory, self.agent))
        if result.outcome is not None:
            await self.agent.aupdate_state(config, {**memory.update, **result.state, "messages": result.messages})
            return result.outcome
        merged = {**values, **result.state}
        verified = bool(merged.get("verified"))
        stale = result.context_stale if result.context_stale is not None else memory.stale
        ctx = TurnContext(mode="voice" if inp.mode == "voice" else "chat", verified=verified,
                          verification="just_verified" if result.verification == "just_verified" else None,
                          pending=result.pending_summary,
                          context_snapshot=merged.get("context_snapshot") if verified else None, context_stale=stale)
        text = result.model_text if result.model_text is not None else (inp.text or "")
        agent_input = {**memory.update, **result.state, "messages": [build_turn_message(text, ctx)]}
        outcome = await self._loop(stream, control, agent_input, config)  # steps 14 and 15
        if control.awaiting_verification and self.flags is not None:  # s6.3 step 2: agent.sessions is the authority
            await self.flags.set_awaiting_verification(inp.session_id, True)
        return outcome

    async def _loop(self, stream: TurnStream, control: TurnControl, agent_input: dict[str, Any],
                    config: dict[str, Any]) -> TurnOutcome:
        """What: step 14 under the loop timeout (time left minus the 3 s reserve), then step 15 in the reserve:
        a loop timeout takes the deadline path, a failed model node takes the s6.7 apology (or the deadline path
        when no loop time is left), any other outcome is finished by the escalator (route and limit tickets).
        Use when: _run. Inputs: stream, control, agent input, config. Output: TurnOutcome.
        Side effects: the agent run; escalation tickets and events. Role: doc 07 s8.2 "tickets on failure paths
        get one attempt, created inside the 3 s reserve"."""
        esc = self.escalator
        cm = asyncio.timeout(max(control.remaining_s() - self.reserve_s, 0.0))
        try:
            async with cm:
                outcome = await stream_agent_turn(stream, self.agent, agent_input, config, control=control)
        except TimeoutError:
            if not cm.expired():  # an inner timeout is an error, not the deadline
                raise
            return await esc.deadline(stream, control, self.agent, config)
        except ModelUnavailable:
            if control.remaining_s() - self.reserve_s <= 0:
                return await esc.deadline(stream, control, self.agent, config)
            return await esc.model_unavailable(stream, control, self.agent, config)
        return await esc.finish(stream, control, outcome, self.agent, config)
