"""What: the ingress core path shared by chat (/turns) and voice (/voice/ask): doc 07 s2.4 steps 1, 2 (body
validation), 4 (replay), 6 (sweep), 7 (turn lock), 8 (40-turn cap), 9 (turn row, deadline, turn.started),
14 (agent loop under the deadline), 15 (limit line, in agent.sse), 16 (final row, turn.completed, release).
Use when: a route has an authenticated visitor and a parsed body; it calls Ingress.handle and turns the
result into a response with to_response. /voice/ask (lane C) builds an IngressInput with mode="voice".
Inputs: a TurnStore (agent.sessions, agent.turns), a TurnLock (Redis), the replica name, the agent body.
Output: IngressError (HTTP error) or IngressStream (SSE stream plus the background task).
Side effects: rows in agent.turns, user_turns, lock:turn:{sid} in Redis, the turn task.
Role: plain code before and after the model (doc 07 s2.4). Voice (CS-E-05.S3.T2): the step 7 call-key dedupe
(optional CallKeys) and the step 12 transcript merge (optional merge_transcript) are hooks here; step 3 (voice
session match and expiry) runs in the voice/ask route (agent.voice). Step 9a (PIN scan and the typed-PIN refusal,
CS-E-03.S7.T9) runs first in the turn task, through agent.pin_guard. Not here yet, by plan: rate limits
(CS-E-07.S2.T1), admission semaphore (designed only), repair and memory
(CS-E-03.S7.T5), structured input (CS-E-03.S7.T8). Tickets on the deadline and limit paths live in the body
(agent.turn_body, agent.escalation, CS-E-03.S4.T1); ingress keeps only a hard net at the full deadline.
"""
from __future__ import annotations

import asyncio
import json
import logging
import uuid
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from typing import Any, Awaitable, Callable, Protocol

from contracts import limits
from contracts.agent_api import TurnRequest
from contracts.errors import ErrorBody, ErrorEnvelope
from contracts.redact import RedactContext
from contracts.events import FinalAnswer, Usage
from fastapi import Request
from fastapi.responses import JSONResponse, StreamingResponse
from langchain_core.utils.uuid import uuid7
from pydantic import ValidationError

from agent.deadline import TurnBudgetExhausted
from agent.pin_guard import refuse_without_thread, scan_input, session_redact_context
from agent.sse import TurnMeta, TurnOutcome, TurnStream, start_turn
from agent.turn_control import TurnControl, bind_turn_control, loop_clock

log = logging.getLogger("agent.ingress")

CAP_LINE = ("This conversation is getting long. Please start a new conversation; your tickets and account "
            "history are kept.")
# doc 07 fixture edge_turns e3 error body (hard net only; the deadline ticket and line are in agent.escalation).
DEADLINE_MESSAGE = "I couldn't finish that in time."
TURN_DEADLINE_S = {"chat": limits.CHAT_TURN_DEADLINE_S, "voice": limits.VOICE_TURN_DEADLINE_S}
# A swept turn (its process died mid-turn) replays as a failed, retryable deadline (doc 07 s2.4 step 4, s8.1).
SWEPT_MESSAGE = "I couldn't finish that. Please send your message again."


def swept_error() -> ErrorBody:
    """What: the error body stored on, and replayed for, a swept turn.
    Use when: PgTurnStore.sweep and the replay of a failed row with no stored error.
    Inputs: none. Output: ErrorBody (turn_deadline_exceeded, retryable, changed unknown). Side effects: none.
    Role: review PR #18 I1 (a swept turn must not replay blank)."""
    return ErrorBody.model_validate({"class": "turn_deadline_exceeded", "code": "TURN_DEADLINE",
                                     "message": SWEPT_MESSAGE, "retryable": True, "changed": None,
                                     "fault_injected": False, "details": {"swept": True}})


def swept_final() -> FinalAnswer:
    """What: the final answer stored on, and replayed for, a swept turn (text and say carry the retry line).
    Use when: with swept_error. Inputs: none. Output: FinalAnswer. Side effects: none. Role: helper."""
    return FinalAnswer(text=SWEPT_MESSAGE, say=SWEPT_MESSAGE, citations=[], escalated=False)


class DuplicateTurn(Exception):
    """What: unique (session_id, client_turn_id) violated. Use when: insert_turn races.
    Inputs: client turn id. Output: exception. Side effects: none. Role: store signal."""


@dataclass(frozen=True)
class SessionRow:
    """What: the agent.sessions facts ingress needs. Use when: step 1 and step 8.
    Inputs: ids, user_turns, verification flags. Output: value. Side effects: none. Role: row."""

    session_id: str
    visitor_id: str
    user_turns: int = 0
    awaiting_verification: bool = False
    verified_user_id: str | None = None


@dataclass(frozen=True)
class TurnRow:
    """What: one agent.turns row. Use when: replay, insert, final write.
    Inputs: doc 07 s12 columns. Output: value. Side effects: none. Role: row."""

    turn_id: str
    session_id: str
    client_turn_id: str
    mode: str
    input_kind: str
    status: str
    deadline_at: datetime
    started_at: datetime
    end_reason: str | None = None
    realtime_call_id: str | None = None
    final: dict[str, Any] | None = None
    usage: dict[str, Any] | None = None
    error: dict[str, Any] | None = None


class TurnStore(Protocol):
    """What: the agent.sessions and agent.turns operations ingress uses. Use when: typing.
    Inputs: see methods. Output: see methods. Side effects: database. Role: storage port."""

    async def get_session(self, session_id: str) -> SessionRow | None:
        """What: read one session. Use when: step 1. Inputs: id. Output: row or None.
        Side effects: SELECT. Role: port method."""
        ...
    async def find_turn(self, session_id: str, client_turn_id: str,
                        realtime_call_id: str | None = None) -> TurnRow | None:
        """What: replay lookup. Use when: step 4. Inputs: ids. Output: row or None.
        Side effects: SELECT. Role: port method."""
        ...
    async def sweep(self, session_id: str, now: datetime) -> int:
        """What: mark overdue running turns failed. Use when: step 6. Inputs: id, now. Output: rows swept.
        Side effects: UPDATE. Role: port method."""
        ...
    async def insert_turn(self, row: TurnRow) -> None:
        """What: insert the running row. Use when: step 9. Inputs: row. Output: none.
        Side effects: INSERT; DuplicateTurn. Role: port method."""
        ...
    async def count_turn(self, session_id: str) -> None:
        """What: user_turns + 1. Use when: step 9. Inputs: id. Output: none.
        Side effects: UPDATE. Role: port method."""
        ...
    async def finish_turn(self, turn_id: str, outcome: TurnOutcome, ended_at: datetime) -> None:
        """What: write the final row. Use when: step 16. Inputs: id, outcome, end time. Output: none.
        Side effects: UPDATE. Role: port method."""
        ...
    async def last_turn(self, session_id: str, before_turn_id: str) -> tuple[str, str] | None:
        """What: (turn_id, status) of the session's latest other turn. Use when: s7.3 adjacency. Inputs: session id, current turn id. Output: pair or None.
        Side effects: SELECT. Role: port method (delivery record)."""
        ...
    async def mark_events_incomplete(self, turn_ids: set[str]) -> None:
        """What: set events_complete false. Use when: trajectory writer drops. Inputs: turn ids. Output: none.
        Side effects: UPDATE. Role: port method."""
        ...
    async def recent_answers(self, session_id: str, limit: int) -> list[str]:
        """What: the final text and say of the session's latest finished turns. Use when: POST /transcript matches
        realtime speech to our own lines (D-071). Inputs: session id, turn count. Output: strings.
        Side effects: SELECT. Role: port method."""
        ...
    async def issued_tickets(self, session_id: str) -> list[dict[str, Any]]:
        """What: the TicketRef of every turn final of the session that has one. Use when: RelayFacts ids (D-071).
        Inputs: session id. Output: ticket dicts. Side effects: SELECT. Role: port method."""
        ...


class TurnLock(Protocol):
    """What: per-session turn lock with an owner token. Use when: typing.
    Inputs: see methods. Output: see methods. Side effects: Redis. Role: lock port."""

    async def acquire(self, session_id: str, token: str, ttl_ms: int) -> bool:
        """What: SET NX PX with the owner token. Use when: step 8. Inputs: id, token, ttl. Output: True when taken.
        Side effects: Redis. Role: port method."""
        ...
    async def release(self, session_id: str, token: str) -> bool:
        """What: compare-and-delete. Use when: turn end. Inputs: id, token. Output: True when released.
        Side effects: Redis. Role: port method."""
        ...


class CallKeys(Protocol):
    """What: voice_call:{sid}:{realtime_call_id} SET NX EX 120 (doc 07 s2.4 step 7, s12). Use when: typing.
    Inputs: session id, call id. Output: True when this request claimed the key. Side effects: Redis.
    Role: voice call dedupe port."""

    async def claim(self, session_id: str, call_id: str) -> bool:
        """What: SET NX EX 120 on the call key. Use when: step 7, voice. Inputs: session id, call id. Output: True when claimed.
        Side effects: Redis. Role: port method."""
        ...


@dataclass(frozen=True)
class IngressInput:
    """What: one turn request after auth and body parsing. Use when: Ingress.handle.
    Inputs: session, visitor, client turn id, mode, input kind, redacted text, realtime call id, raw body, the
    gated demo fault
    (TurnRequest or VoiceAskRequest); merged_transcript is set by ingress at step 12 (the wrapped
    <voice_transcript> user message for the agent body to place before this turn's message, or None);
    redact_context is the session's scanner context (doc 07 s11.1 conditions) for text that ingress hooks store.
    Output: value. Side effects: none. Role: the shared entry shape for chat and voice."""

    session_id: str
    visitor_id: str
    client_turn_id: str
    mode: str
    input_kind: str
    text: str | None = None
    realtime_call_id: str | None = None
    body: Any = None
    demo_fault: str | None = None  # already gated by demo_fault_from_header (doc 07 s10), request-scoped
    merged_transcript: str | None = None
    later_user_item: bool | None = None  # step 12: a client user item was merged; None = unknown (fail closed)
    redact_context: RedactContext | None = None
    count_turn: Callable[[], Awaitable[IngressError | None]] | None = None  # rate-limit hook, called at step 9


@dataclass(frozen=True)
class IngressError:
    """What: an HTTP error decided before the stream opened. Use when: to_response.
    Inputs: status, class, code, customer-safe message, retryable, retry_after_s, details.
    Output: value. Side effects: none. Role: doc 07 s1.3 envelope source."""

    status: int
    class_: str
    code: str
    message: str
    retryable: bool = False
    retry_after_s: int | None = None
    details: dict[str, Any] = field(default_factory=dict)


@dataclass
class IngressStream:
    """What: an open SSE stream and the task producing it (None for a replay). Use when: to_response.
    Inputs: stream, task. Output: value. Side effects: none. Role: success result."""

    stream: TurnStream
    task: asyncio.Task[TurnOutcome] | None


@dataclass(frozen=True)
class MergeResult:
    """What: a step 12 merge outcome: the wrapped text (or None) and whether a client user item was claimed,
    decided from the rows before any display truncation. Use when: a MergeTranscript that knows its rows
    (agent.voice.TranscriptMerger). Inputs: text, later_user_item. Output: value. Side effects: none.
    Role: lets the voice yes rule (doc 07 s7.3) read data, not display text (review PR #56 I2, M1)."""

    text: str | None
    later_user_item: bool


AgentBody = Callable[[TurnStream, IngressInput, TurnRow], Awaitable[TurnOutcome]]
MergeTranscript = Callable[[IngressInput, TurnRow], Awaitable["str | MergeResult | None"]]


def parse_turn_request(raw: Any) -> TurnRequest | IngressError:
    """What: validate a chat body against contracts.TurnRequest (exactly one input, 1..2000 chars).
    Use when: POST /turns before Ingress.handle. Inputs: decoded JSON. Output: TurnRequest or a 422 error.
    Side effects: none. Role: doc 07 s2.4 step 2 body validation (no input echoed back)."""
    try:
        return TurnRequest.model_validate(raw)
    except ValidationError as exc:
        return IngressError(422, "validation", "VALIDATION_ERROR", "The request was not valid.",
                            details={"errors": [str(e.get("msg")) for e in exc.errors()]})


def input_kind_of(req: TurnRequest) -> str:
    """What: which of the four inputs a TurnRequest carries. Use when: building IngressInput.
    Inputs: TurnRequest. Output: text, confirm, verification or ticket_offer. Side effects: none. Role: helper."""
    for name in ("text", "confirm", "verification", "ticket_offer"):
        if getattr(req, name) is not None:
            return name
    raise ValueError("empty TurnRequest")


def _conflict() -> IngressError:
    """What: 409 TURN_IN_PROGRESS. Use when: steps 4 and 7. Inputs: none. Output: error.
    Side effects: none. Role: helper (class core_conflict: the only conflict class in doc 07 s1.3)."""
    return IngressError(409, "core_conflict", "TURN_IN_PROGRESS",
                        "I'm still working on your last message. Please wait a moment.")


def _redis_down() -> IngressError:
    """What: 503 REDIS_UNAVAILABLE (fail closed: the lock and the call key protect the thread, doc 07 s9).
    Use when: step 7. Inputs: none. Output: error. Side effects: none. Role: helper."""
    return IngressError(503, "redis_unavailable", "REDIS_UNAVAILABLE",
                        "We're having a brief problem. Please try again in a moment.", retryable=True,
                        retry_after_s=3)


class Ingress:
    """What: the callable ingress entry. Use when: one instance per app; handle() per request.
    Inputs: store, lock, replica name, agent body, deadlines and reserve (overridable for tests), clock,
    call keys (voice step 7 dedupe; production RedisCallKeys), merge_transcript (step 12; production
    agent.voice.TranscriptMerger), trajectory writer (app.state.trajectory, or None: events are not persisted),
    refuse_typed_pin (step 9a refusal body; production agent.pin_guard.TypedPinRefusal, default: event and fixed
    line without a thread write), flags (awaiting_verification writer for the step 9a hint).
    Output: handle(). Side effects: see module. Role: doc 07 s2.4 orchestration."""

    def __init__(self, store: TurnStore, lock: TurnLock, *, served_by: str, run_agent: AgentBody,
                 turn_deadline_s: dict[str, float] | None = None, reserve_s: float = limits.ESCALATION_RESERVE_S,
                 now: Callable[[], datetime] = lambda: datetime.now(UTC), call_keys: CallKeys | None = None,
                 merge_transcript: MergeTranscript | None = None, trajectory: Any = None,
                 refuse_typed_pin: AgentBody | None = None, flags: Any = None) -> None:
        """What: wire dependencies. Use when: app start or tests. Inputs: see class. Output: none.
        Side effects: none. Role: constructor."""
        self.store, self.lock, self.served_by, self.run_agent = store, lock, served_by, run_agent
        self.trajectory = trajectory
        self.deadline_s = turn_deadline_s or TURN_DEADLINE_S
        self.reserve_s, self.now = reserve_s, now
        self.call_keys, self.merge_transcript = call_keys, merge_transcript
        self.refuse_typed_pin: AgentBody = refuse_typed_pin or refuse_without_thread
        self.flags = flags  # agent.sessions.awaiting_verification writer for the step 9a hint (None: no hint)

    async def handle(self, inp: IngressInput) -> IngressError | IngressStream:
        """What: run steps 1, 4, 6, 7, 8, 9 and start the turn task (14 to 16). A resend that finds its own
        turn still running runs the step 6 sweep first, so a killed holder's turn replays as failed.
        Use when: every chat or voice turn. Inputs: IngressInput. Output: IngressError or IngressStream.
        Side effects: rows, lock, task. Role: entry point."""
        session = await self.store.get_session(inp.session_id)
        if session is None or session.visitor_id != inp.visitor_id:  # step 1: no existence hint
            return IngressError(404, "not_found", "NOT_FOUND", "Not found.")
        prior = await self._find(inp)
        if prior is not None and prior.status == "running" and await self.store.sweep(inp.session_id, self.now()):
            prior = await self._find(inp)  # step 6 for a resend of a killed holder's turn (review PR #18 I1)
        if prior is not None:  # step 4: replay first, no lock
            return self._replay(prior) if prior.status != "running" else _conflict()
        await self.store.sweep(inp.session_id, self.now())  # step 6
        token = str(uuid.uuid4())
        try:  # step 7: fail closed when Redis is down (doc 07 s9)
            locked = await self.lock.acquire(inp.session_id, token, limits.TURN_LOCK_TTL_S * 1000)
        except Exception:  # noqa: BLE001
            return _redis_down()
        if not locked:
            return _conflict()
        refused = await self._claim_call(inp, token)  # step 7, voice only: after the lock, so a refusal never burns it
        if refused is not None:
            return refused

        async def release(_: TurnOutcome) -> None:
            """What: compare-and-delete the lock. Use when: after the turn. Inputs: outcome.
            Output: none. Side effects: Redis. Role: step 16 release."""
            await self.lock.release(inp.session_id, token)

        try:
            if session.user_turns >= limits.TURNS_PER_SESSION:  # step 8: cap line, no row, not counted
                stream = TurnStream(TurnMeta(inp.session_id, str(uuid7()), inp.mode, self.served_by, inp.input_kind))

                async def cap_body(_: TurnStream) -> TurnOutcome:
                    """What: the cap line. Use when: step 8. Inputs: stream. Output: outcome.
                    Side effects: none. Role: deterministic reply."""
                    return TurnOutcome.fixed(CAP_LINE, end_reason="cap_reached")

                return IngressStream(stream, start_turn(stream, cap_body, write_final=_noop, after=release))
            if inp.count_turn is not None:  # doc 07 s2.4 Counting: only a turn that starts counts (not replays or the cap)
                refused = await inp.count_turn()
                if refused is not None:
                    await self.lock.release(inp.session_id, token)
                    return refused
            return await self._start(inp, session, release)
        except BaseException:
            await self.lock.release(inp.session_id, token)
            raise

    async def _awaiting_hint(self, session: SessionRow, scanned: Any) -> None:
        """What: step 9a session hint: set awaiting_verification when an unverified customer announced a PIN without
        giving it ("my PIN is", or only an account id), clear it when the customer moved on to another topic.
        Use when: the turn task, right after the scan. Inputs: session row, Scanned. Output: none.
        Side effects: one UPDATE agent.sessions at most; a failure is logged, the turn goes on.
        Role: review PR #57 round 2 (split PINs N18, N20; the flag clears per 07 s6.3 and s7.3 intent)."""
        if self.flags is None:
            return
        want: bool | None = None
        if scanned.expects_pin and not session.awaiting_verification and session.verified_user_id is None:
            want = True
        elif scanned.moved_on and session.awaiting_verification and not scanned.refuse:
            want = False
        if want is None:
            return
        try:
            await self.flags.set_awaiting_verification(session.session_id, want)
        except Exception:  # noqa: BLE001 - the hint is defence in depth, never a reason to fail a turn
            log.warning("awaiting hint not written session_id=%s", session.session_id, exc_info=True)

    async def _claim_call(self, inp: IngressInput, token: str) -> IngressError | IngressStream | None:
        """What: SET voice_call:{sid}:{call} NX after the lock; if taken, re-run step 4 (replay a finished turn,
        else 409 DUPLICATE_CALL). Releases the lock on every refusal.
        Use when: handle, voice turns with call keys configured. Inputs: input, lock owner token.
        Output: None to continue, or the refusal or replay. Side effects: Redis SET, lock release.
        Role: doc 07 s2.4 step 7 second half."""
        if inp.mode != "voice" or not inp.realtime_call_id or self.call_keys is None:
            return None
        try:
            claimed = await self.call_keys.claim(inp.session_id, inp.realtime_call_id)
        except Exception:  # noqa: BLE001 - fail closed (doc 07 s9)
            await self.lock.release(inp.session_id, token)
            return _redis_down()
        if claimed:
            return None
        await self.lock.release(inp.session_id, token)
        prior = await self._find(inp)
        if prior is not None and prior.status != "running":
            return self._replay(prior)
        return IngressError(409, "core_conflict", "DUPLICATE_CALL",
                            "I'm already working on that. Please wait a moment.")

    async def _merge(self, inp: IngressInput, row: TurnRow) -> IngressInput:
        """What: step 12: claim unmerged client transcript items for this turn; a failure is logged and the turn
        goes on without them (they stay unmerged for the next turn only if the claim did not commit).
        Use when: the turn task, before the agent loop. Inputs: input, turn row.
        Output: the input with merged_transcript set, and later_user_item when the merger reports it (a missing
        merger, a failure or a plain-text result leave it None: unknown). Side effects: transcript_items UPDATE.
        Role: helper."""
        if self.merge_transcript is None:
            return inp
        try:
            merged = await self.merge_transcript(inp, row)
        except Exception:  # noqa: BLE001 - never block a turn on display chatter
            log.warning("transcript merge failed session_id=%s turn_id=%s", inp.session_id, row.turn_id,
                        exc_info=True)
            merged = None
        if isinstance(merged, MergeResult):
            return replace(inp, merged_transcript=merged.text, later_user_item=merged.later_user_item)
        return replace(inp, merged_transcript=merged)

    async def _find(self, inp: IngressInput) -> TurnRow | None:
        """What: the step 4 lookup (client turn id, and realtime call id for voice). Use when: handle.
        Inputs: input. Output: row or None. Side effects: SELECT. Role: helper."""
        return await self.store.find_turn(inp.session_id, inp.client_turn_id,
                                          inp.realtime_call_id if inp.mode == "voice" else None)

    async def _start(self, inp: IngressInput, session: SessionRow,
                     release: Callable[[TurnOutcome], Awaitable[None]]) -> IngressStream | IngressError:
        """What: step 9 (row, count, stream) and the deadline-bound turn task (step 9a first inside it).
        Use when: handle, under the lock. Inputs: input, session row, release callback. Output: stream or 409.
        Side effects: insert, count, task. Role: helper."""
        started = self.now()
        budget = self.deadline_s[inp.mode]
        row = TurnRow(turn_id=str(uuid7()), session_id=inp.session_id, client_turn_id=inp.client_turn_id,
                      mode=inp.mode, input_kind=inp.input_kind, status="running", started_at=started,
                      deadline_at=started + timedelta(seconds=budget), realtime_call_id=inp.realtime_call_id)
        try:
            await self.store.insert_turn(row)
        except DuplicateTurn:
            await release(TurnOutcome.fixed("", end_reason="error"))
            return _conflict()
        await self.store.count_turn(inp.session_id)
        stream = TurnStream(TurnMeta(inp.session_id, row.turn_id, inp.mode, self.served_by, inp.input_kind),
                            sink=self.trajectory.add if self.trajectory is not None else None)  # doc 07 s12 turn_events
        # The hard net is the whole deadline: the body itself (TurnRunner) stops its agent loop 3 s early and spends the
        # reserve on the escalation ticket (CS-E-03.S4.T1); cutting the body at deadline - 3 s here would cancel that
        # ticket. self.reserve_s is kept for callers; the body owns the reserve.
        hard_net = budget
        deadline_mono = loop_clock() + budget  # TurnControl.deadline: clamps take the reserve off this

        async def body(s: TurnStream) -> TurnOutcome:
            """What: the agent loop under asyncio.timeout, with the turn's one TurnControl bound (deadline = the 45 s
            or 20 s turn deadline, so every clamp subtracts the 3 s reserve from it; the request's demo fault).
            Step 9a runs first: the PIN scan (only redacted text travels further) and the typed-PIN refusal.
            Use when: the turn task. Inputs: stream.
            Output: outcome (deadline outcome, no ticket, on the hard net or on a call skipped by the clamp). Side effects: model
            and tools; binds TURN_CONTROL in this task. Role: steps 9a to 14; doc 07 s8.2 deadline path."""
            scanned = scan_input(inp, session_redact_context(session))  # step 9a
            await self._awaiting_hint(session, scanned)
            control = TurnControl(session_id=inp.session_id, turn_id=row.turn_id, mode=inp.mode,
                                  deadline=deadline_mono, demo_fault=inp.demo_fault, events=s,
                                  session_awaiting_verification=session.awaiting_verification,
                                  first_message=session.user_turns == 0 and session.verified_user_id is None)
            bind_turn_control(control)  # the turn task's own context; the only .set() (spike 15)
            cm = asyncio.timeout(hard_net)
            try:
                async with cm:
                    if scanned.refuse:
                        return await self.refuse_typed_pin(s, scanned.inp, row)
                    return await self.run_agent(s, await self._merge(scanned.inp, row), row)
            except (TimeoutError, TurnBudgetExhausted) as exc:
                if isinstance(exc, TimeoutError) and not cm.expired():  # an inner timeout is an error, not the deadline
                    raise
                err = ErrorBody.model_validate({"class": "turn_deadline_exceeded", "code": "TURN_DEADLINE",
                                                "message": DEADLINE_MESSAGE, "retryable": True, "changed": None,
                                                "fault_injected": False, "details": {}})
                return TurnOutcome.fixed(DEADLINE_MESSAGE, end_reason="deadline", status="failed", error=err)

        async def write_final(outcome: TurnOutcome) -> None:
            """What: final row. Use when: step 16. Inputs: outcome. Output: none. Side effects: UPDATE.
            Role: synchronous write before turn.completed."""
            await self.store.finish_turn(row.turn_id, outcome, self.now())

        return IngressStream(stream, start_turn(stream, body, write_final=write_final, after=release))

    def _replay(self, prior: TurnRow) -> IngressStream:
        """What: replay a finished turn: stored error (if failed) then turn.completed with replayed true.
        Use when: step 4. Inputs: prior row. Output: IngressStream with no task. Side effects: none.
        Role: doc 07 s2.4 step 4 (seq restarts at 1 for the replay stream)."""
        stream = TurnStream(TurnMeta(prior.session_id, prior.turn_id, prior.mode, self.served_by, prior.input_kind))
        failed = prior.status == "failed"
        if failed:  # a row swept before errors were stored still replays a retryable error
            error = prior.error or swept_error().model_dump(mode="json", by_alias=True)
            stream.emit("error", **{k: v for k, v in error.items() if k != "trace_id"})
        blank = swept_final() if failed else FinalAnswer(text="", say="", citations=[], escalated=False)
        final = prior.final or blank.model_dump(mode="json")
        usage = prior.usage or Usage(input_tokens=0, output_tokens=0, cache_read_tokens=0, cache_write_tokens=0,
                                     cost_usd_est=0.0, latency_ms=0, model_calls=0, tool_calls=0, models_used=[],
                                     fallback_used=False).model_dump(mode="json")
        stream.emit("turn.completed", final=final, usage=usage, end_reason=prior.end_reason or "error", replayed=True)
        return IngressStream(stream, None)


async def _noop(_: TurnOutcome) -> None:
    """What: no final row (the capped turn has none). Use when: step 8. Inputs: outcome. Output: none.
    Side effects: none. Role: helper."""


def to_response(request: Request, result: IngressError | IngressStream) -> Any:
    """What: HTTP response for an ingress result: envelope JSON or text/event-stream.
    Use when: the /turns and /voice/ask routes. Inputs: request, result. Output: Response.
    Side effects: none (the turn task already runs). Role: route adapter; X-Trace-Id equals the turn id."""
    if isinstance(result, IngressError):
        body = ErrorBody.model_validate({"class": result.class_, "code": result.code, "message": result.message,
                                         "retryable": result.retryable, "changed": False,
                                         "retry_after_s": result.retry_after_s, "fault_injected": False,
                                         "trace_id": getattr(request.state, "trace_id", None),
                                         "details": result.details})
        headers = {"Retry-After": str(result.retry_after_s)} if result.retry_after_s else None
        return JSONResponse(ErrorEnvelope(error=body).model_dump(mode="json"), status_code=result.status,
                            headers=headers)
    request.state.trace_id = result.stream.meta.turn_id
    return StreamingResponse(result.stream.frames(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


class RedisTurnLock:
    """What: lock:turn:{sid} with SET NX PX and a compare-and-delete Lua release (doc 07 s2.4 step 7, s12).
    Use when: production TurnLock. Inputs: redis.asyncio client. Output: acquire, release.
    Side effects: Redis commands. Role: lock adapter."""

    RELEASE = "if redis.call('get', KEYS[1]) == ARGV[1] then return redis.call('del', KEYS[1]) else return 0 end"

    def __init__(self, redis: Any) -> None:
        """What: keep the client. Use when: app start. Inputs: client. Output: none. Side effects: none. Role: constructor."""
        self.redis = redis

    async def acquire(self, session_id: str, token: str, ttl_ms: int) -> bool:
        """What: SET NX PX. Use when: step 7. Inputs: sid, owner token, ttl ms. Output: True when taken.
        Side effects: one SET. Role: acquire."""
        return bool(await self.redis.set(f"lock:turn:{session_id}", token, nx=True, px=ttl_ms))

    async def release(self, session_id: str, token: str) -> bool:
        """What: delete only if the owner token matches. Use when: step 16. Inputs: sid, token.
        Output: True when released. Side effects: one EVAL. Role: release."""
        return bool(await self.redis.eval(self.RELEASE, 1, f"lock:turn:{session_id}", token))


class PgTurnStore:
    """What: TurnStore over the agent_app psycopg pool (schema agent).
    Use when: production. Inputs: psycopg AsyncConnectionPool. Output: TurnStore methods.
    Side effects: SQL on agent.sessions and agent.turns (including events_complete). Role: storage adapter."""

    def __init__(self, pool: Any) -> None:
        """What: keep the pool. Use when: app start. Inputs: pool. Output: none. Side effects: none. Role: constructor."""
        self.pool = pool

    async def get_session(self, session_id: str) -> SessionRow | None:
        """What: read one session. Use when: step 1. Inputs: id. Output: row or None. Side effects: SELECT. Role: read."""
        try:
            uuid.UUID(session_id)
        except ValueError:
            return None
        async with self.pool.connection() as conn:
            cur = await conn.execute("SELECT session_id::text, visitor_id, user_turns, awaiting_verification, "
                                     "verified_user_id FROM agent.sessions WHERE session_id = %s", (session_id,))
            r = await cur.fetchone()
        return SessionRow(*r) if r else None

    async def find_turn(self, session_id: str, client_turn_id: str,
                        realtime_call_id: str | None = None) -> TurnRow | None:
        """What: replay lookup by client turn id (and realtime call id for voice). Use when: step 4.
        Inputs: ids. Output: row or None. Side effects: SELECT. Role: read."""
        async with self.pool.connection() as conn:
            cur = await conn.execute(
                "SELECT turn_id::text, session_id::text, client_turn_id::text, mode, input_kind, status, deadline_at, "
                "started_at, end_reason, realtime_call_id, final, usage, error FROM agent.turns "
                "WHERE session_id = %s AND (client_turn_id = %s OR (%s::text IS NOT NULL AND realtime_call_id = %s)) "
                "ORDER BY started_at DESC LIMIT 1",
                (session_id, client_turn_id, realtime_call_id, realtime_call_id))
            r = await cur.fetchone()
        return TurnRow(*r) if r else None

    async def sweep(self, session_id: str, now: datetime) -> int:
        """What: doc 07 s2.4 step 6 UPDATE, plus the swept error and final so a replay is not blank.
        Use when: before the lock, and on a resend that finds its own turn running. Inputs: id, now.
        Output: rows swept. Side effects: UPDATE. Role: write."""
        error = json.dumps(swept_error().model_dump(mode="json", by_alias=True))
        final = json.dumps(swept_final().model_dump(mode="json"))
        async with self.pool.connection() as conn:
            cur = await conn.execute(
                "UPDATE agent.turns SET status = 'failed', end_reason = 'swept', ended_at = %s, error = %s, "
                "final = %s WHERE session_id = %s AND status = 'running' "
                "AND deadline_at + interval '10 seconds' < %s",
                (now, error, final, session_id, now))
            return cur.rowcount

    async def insert_turn(self, row: TurnRow) -> None:
        """What: INSERT the running row. Use when: step 9. Inputs: row. Output: none.
        Side effects: INSERT; DuplicateTurn on the unique key. Role: write."""
        from psycopg.errors import UniqueViolation
        try:
            async with self.pool.connection() as conn:
                await conn.execute(
                    "INSERT INTO agent.turns (turn_id, session_id, client_turn_id, mode, input_kind, status, "
                    "deadline_at, started_at, realtime_call_id) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
                    (row.turn_id, row.session_id, row.client_turn_id, row.mode, row.input_kind, row.status,
                     row.deadline_at, row.started_at, row.realtime_call_id))
        except UniqueViolation as exc:
            raise DuplicateTurn(row.client_turn_id) from exc

    async def count_turn(self, session_id: str) -> None:
        """What: user_turns + 1 and last_active_at. Use when: step 9. Inputs: id. Output: none.
        Side effects: UPDATE. Role: write (agent.sessions is the authority, doc 07 s2.4 Counting)."""
        async with self.pool.connection() as conn:
            await conn.execute("UPDATE agent.sessions SET user_turns = user_turns + 1, last_active_at = now() "
                               "WHERE session_id = %s", (session_id,))

    async def last_turn(self, session_id: str, before_turn_id: str) -> tuple[str, str] | None:
        """What: (turn_id, status) of the session's latest turn other than before_turn_id (the current one).
        Use when: the s7.3 adjacency rule (StructuredInput last_turn): a yes commits only when this is the read-back
        turn and it is completed. Inputs: session id, current turn id. Output: pair or None. Side effects: SELECT.
        Role: read (the turn's final row status is the delivery record)."""
        async with self.pool.connection() as conn:
            cur = await conn.execute(
                "SELECT turn_id::text, status FROM agent.turns WHERE session_id = %s AND turn_id <> %s "
                "ORDER BY started_at DESC, turn_id DESC LIMIT 1", (session_id, before_turn_id))
            r = await cur.fetchone()
        return (r[0], r[1]) if r else None

    async def recent_answers(self, session_id: str, limit: int) -> list[str]:
        """What: final text and say of the session's latest `limit` turns that have a final.
        Use when: POST /transcript (D-071: realtime speech is ours only when it is one of these lines).
        Inputs: session id, turn count. Output: strings, newest turn first. Side effects: SELECT. Role: read."""
        async with self.pool.connection() as conn:
            cur = await conn.execute(
                "SELECT final->>'text', final->>'say' FROM agent.turns WHERE session_id = %s AND final IS NOT NULL "
                "ORDER BY started_at DESC, turn_id DESC LIMIT %s", (session_id, limit))
            rows = await cur.fetchall()
        return [t for r in rows for t in r if t]

    async def issued_tickets(self, session_id: str) -> list[dict[str, Any]]:
        """What: final.ticket (the TicketRef core returned) of every turn of the session that has one.
        Use when: RelayFacts ids for POST /transcript (D-071 part 3). Inputs: session id. Output: ticket dicts.
        Side effects: SELECT. Role: read (structured, code-held: never parsed from text)."""
        async with self.pool.connection() as conn:
            cur = await conn.execute(
                "SELECT final->'ticket' FROM agent.turns WHERE session_id = %s AND jsonb_typeof(final->'ticket') = "
                "'object' ORDER BY started_at", (session_id,))
            rows = await cur.fetchall()
        return [r[0] for r in rows if isinstance(r[0], dict)]

    async def finish_turn(self, turn_id: str, outcome: TurnOutcome, ended_at: datetime) -> None:
        """What: the final row. Use when: step 16. Inputs: id, outcome, end time. Output: none.
        Side effects: UPDATE. Role: write (turns.final makes GET /api/turns/{id} work without events)."""
        error = outcome.error.model_dump(mode="json", by_alias=True) if outcome.error else None
        async with self.pool.connection() as conn:
            await conn.execute(
                "UPDATE agent.turns SET status = %s, end_reason = %s, ended_at = %s, final = %s, usage = %s, "
                "error = %s, model_used = %s, fallback_used = %s WHERE turn_id = %s",
                (outcome.status, outcome.end_reason, ended_at, json.dumps(outcome.final.model_dump(mode="json")),
                 json.dumps(outcome.usage.model_dump(mode="json")), json.dumps(error) if error else None,
                 outcome.model_used, outcome.fallback_used, turn_id))

    async def mark_events_incomplete(self, turn_ids: set[str]) -> None:
        """What: events_complete = false for turns whose turn_events rows were dropped.
        Use when: TrajectoryWriter(on_incomplete=...). Inputs: turn ids. Output: none.
        Side effects: one UPDATE on this store's pool (not the writer's). Role: doc 07 s12 events_complete."""
        ids = [i for i in turn_ids if i]
        if not ids:
            return
        async with self.pool.connection() as conn:
            await conn.execute("UPDATE agent.turns SET events_complete = false WHERE turn_id = ANY(%s::uuid[])", (ids,))
