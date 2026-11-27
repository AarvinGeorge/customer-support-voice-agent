"""What: the voice endpoints POST /api/sessions/{sid}/voice/token, /voice/ask and /transcript (doc 07 s2.2, s2.3),
the voice store (agent.voice_sessions, agent.transcript_items), the Redis call keys and the step 12 transcript merger.
Use when: create_app calls install_voice once; Ingress gets RedisCallKeys and TranscriptMerger as its voice hooks.
Inputs: VoiceDeps on app.state.voice (store, minter, voice role, thread summary, the shared Ingress, clock).
Output: install_voice, VoiceDeps, VoiceSessionRow, PgVoiceStore, RedisCallKeys, TranscriptMerger, format_merged,
thread_summary, CoveredItem, MergedItem.
Side effects: one OpenAI mint per token request, rows in agent.voice_sessions and agent.transcript_items,
Redis call keys; voice/ask runs a full agent turn through Ingress.
Role: one brain for chat and voice (D-002): voice/ask is the chat ingress with mode="voice"; doc 07 s2.4 step 3
(voice session match, 10 minutes from mint) is checked here before Ingress.handle.
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Awaitable, Callable, Protocol

from contracts import limits
from contracts.agent_api import TranscriptItemIn, TranscriptPost, VoiceAskRequest, VoiceToken
from contracts.base import StrictModel
from contracts import redact as redaction
from contracts.redact import RedactContext
from contracts.trust import wrap_voice_transcript
from fastapi import Depends, FastAPI, Request, Response
from fastapi.responses import JSONResponse
from langchain_core.utils.uuid import uuid7

from agent.access import owned_session
from agent.assembly import run_config
from agent.ingress import Ingress, IngressInput, SessionRow, TurnRow, to_response
from agent.limits.ratelimit import limit_session_post, limit_turn, limit_voice_token
from agent.models.config import RoleSpec
from agent.prompt import _escape_frame  # pyright: ignore[reportPrivateUsage]  (the frame-tag escaper, M5)
from agent.realtime import MintedSecret, MintError, SessionSummary, build_session_config, render_instructions, \
    summary_from_thread
from agent.web import error_response

log = logging.getLogger("agent.voice")

VOICE_CALL_KEY_TTL_S = 120  # doc 07 s2.4 step 7 and s12
THREAD_READ_TIMEOUT_S = limits.POSTGRES_CHECKPOINTER_TIMEOUT_S
MERGE_MAX_CHARS = 6000  # cap on the merged block; the newest items are kept (review PR #2 M9)


@dataclass(frozen=True)
class VoiceSessionRow:
    """What: one agent.voice_sessions row. Use when: mint (insert) and step 3 (read).
    Inputs: ids, minted_at, expires_at. Output: value. Side effects: none. Role: row (mint time authority, s2.4)."""

    voice_session_id: str
    session_id: str
    minted_at: datetime
    expires_at: datetime


@dataclass(frozen=True)
class CoveredItem:
    """What: the Realtime user item a voice/ask answers (its user_item_id). Use when: step 12.
    Inputs: item id, voice session id, its text (redacted). Output: value. Side effects: none.
    Role: the item already reaches the thread through the voice turn, so the merge leaves it out (doc 07 s2.6)."""

    realtime_item_id: str
    voice_session_id: str
    text: str


@dataclass(frozen=True)
class MergedItem:
    """What: one client transcript item claimed by a turn. Use when: format_merged.
    Inputs: role, text, interrupted. Output: value. Side effects: none. Role: merge result."""

    role: str
    text: str
    interrupted: bool


class VoiceStore(Protocol):
    """What: the voice_sessions and transcript_items operations. Use when: typing.
    Inputs: see methods. Output: see methods. Side effects: database. Role: storage port."""

    async def create_voice_session(self, row: VoiceSessionRow) -> None: ...
    async def get_voice_session(self, voice_session_id: str) -> VoiceSessionRow | None: ...
    async def add_transcript_items(self, session_id: str, voice_session_id: str,
                                   items: list[TranscriptItemIn]) -> int: ...
    async def take_unmerged(self, session_id: str, turn_id: str, covered: CoveredItem | None) -> list[MergedItem]: ...


class Minter(Protocol):
    """What: mints a Realtime client secret. Use when: typing. Inputs: session config. Output: MintedSecret.
    Side effects: provider call. Role: port (OpenAIRealtimeMinter in production)."""

    async def mint(self, session: dict[str, Any]) -> MintedSecret: ...


@dataclass
class VoiceDeps:
    """What: everything the voice routes need. Use when: create_app(voice=...) or the lifespan builds it.
    Inputs: store, minter, voice RoleSpec (None: models.yaml failed to load, token answers 503), thread summary
    callable, the shared Ingress (None until the agent loop is wired: voice/ask answers 503), clock.
    Output: value on app.state.voice. Side effects: none. Role: dependency bundle."""

    store: VoiceStore
    minter: Minter
    voice_spec: RoleSpec | None
    summary: Callable[[str, RedactContext], Awaitable[SessionSummary]]
    ingress: Ingress | None = None
    now: Callable[[], datetime] = field(default=lambda: datetime.now(UTC))


def format_merged(items: list[MergedItem]) -> str | None:
    """What: one user-role text `<voice_transcript source="client" untrusted="true">` block, oldest first, with the
    wrapper and frame tags escaped and at most MERGE_MAX_CHARS of item lines (the newest items are kept).
    Use when: step 12. Inputs: claimed items. Output: wrapped text, or None when there is nothing.
    Side effects: none. Role: doc 07 s2.6 row 5 (client-reported display text, escaped, never instructions)."""
    if not items:
        return None
    lines: list[str] = []
    used = 0
    for it in reversed(items):
        who = "customer" if it.role == "user" else "voice assistant"
        line = f"{who}{' (interrupted)' if it.interrupted else ''}: {_escape_frame(it.text)}"
        if lines and used + len(line) + 1 > MERGE_MAX_CHARS:
            break
        lines.append(line[:MERGE_MAX_CHARS])
        used += len(line) + 1
    return wrap_voice_transcript("\n" + "\n".join(reversed(lines)) + "\n")


class TranscriptMerger:
    """What: the Ingress merge_transcript hook. Use when: every chat and voice turn (step 12).
    Inputs: VoiceStore. Output: callable (input, turn row) -> wrapped text or None.
    Side effects: marks items merged into the turn (once). Role: doc 07 s2.4 step 12."""

    def __init__(self, store: VoiceStore) -> None:
        """What: keep the store. Use when: app start. Inputs: store. Output: none. Side effects: none.
        Role: constructor."""
        self.store = store

    async def __call__(self, inp: IngressInput, row: TurnRow) -> str | None:
        """What: claim unmerged client items, leaving out the item this voice/ask answers.
        Use when: Ingress._merge. Inputs: input (body is a VoiceAskRequest for voice), row.
        Output: wrapped text or None. Side effects: UPDATE/INSERT. Role: merge."""
        covered = None
        req = inp.body
        if isinstance(req, VoiceAskRequest) and req.user_item_id:
            text = redaction.redact(req.user_transcript or req.question, inp.redact_context or RedactContext())
            covered = CoveredItem(req.user_item_id, req.voice_session_id, text)  # redacted before storage (I1)
        return format_merged(await self.store.take_unmerged(inp.session_id, row.turn_id, covered))


class RedisCallKeys:
    """What: voice_call:{sid}:{call} SET NX EX 120. Use when: production CallKeys.
    Inputs: redis.asyncio client. Output: claim(). Side effects: one SET. Role: adapter."""

    def __init__(self, redis: Any) -> None:
        """What: keep the client. Use when: app start. Inputs: client. Output: none. Side effects: none.
        Role: constructor."""
        self.redis = redis

    async def claim(self, session_id: str, call_id: str) -> bool:
        """What: claim the call key once. Use when: step 7. Inputs: ids. Output: True when new.
        Side effects: SET NX EX. Role: dedupe."""
        return bool(await self.redis.set(f"voice_call:{session_id}:{call_id}", "1", nx=True,
                                         ex=VOICE_CALL_KEY_TTL_S))


class PgVoiceStore:
    """What: VoiceStore over the agent_app pool. Use when: production.
    Inputs: psycopg AsyncConnectionPool. Output: VoiceStore methods.
    Side effects: SQL on agent.voice_sessions and agent.transcript_items. Role: adapter."""

    def __init__(self, pool: Any) -> None:
        """What: keep the pool. Use when: app start. Inputs: pool. Output: none. Side effects: none. Role: constructor."""
        self.pool = pool

    async def create_voice_session(self, row: VoiceSessionRow) -> None:
        """What: insert the mint row. Use when: voice/token. Inputs: row. Output: none. Side effects: INSERT.
        Role: write."""
        async with self.pool.connection() as conn:
            await conn.execute("INSERT INTO agent.voice_sessions (voice_session_id, session_id, minted_at, expires_at) "
                               "VALUES (%s, %s, %s, %s)",
                               (row.voice_session_id, row.session_id, row.minted_at, row.expires_at))

    async def get_voice_session(self, voice_session_id: str) -> VoiceSessionRow | None:
        """What: read one mint row. Use when: step 3, transcript post. Inputs: id. Output: row or None.
        Side effects: SELECT. Role: read."""
        async with self.pool.connection() as conn:
            cur = await conn.execute("SELECT voice_session_id, session_id::text, minted_at, expires_at "
                                     "FROM agent.voice_sessions WHERE voice_session_id = %s", (voice_session_id,))
            r = await cur.fetchone()
        return VoiceSessionRow(*r) if r else None

    async def add_transcript_items(self, session_id: str, voice_session_id: str,
                                   items: list[TranscriptItemIn]) -> int:
        """What: insert client items; a re-posted realtime_item_id is ignored (unique key).
        Use when: POST /transcript. Inputs: ids, redacted items. Output: rows inserted.
        Side effects: INSERT ... ON CONFLICT DO NOTHING. Role: write; created_at is clock_timestamp() per row, not the
        transaction's now(), so items of one post keep their order in the merge (found by the Postgres probe)."""
        n = 0
        async with self.pool.connection() as conn:
            async with conn.transaction():
                for it in items:
                    cur = await conn.execute(
                        "INSERT INTO agent.transcript_items (id, session_id, voice_session_id, realtime_item_id, role, "
                        "mode, source, text, interrupted, created_at) VALUES (%s, %s, %s, %s, %s, 'voice', 'client', %s, %s, "
                        "clock_timestamp()) "
                        "ON CONFLICT (session_id, realtime_item_id) DO NOTHING",
                        (str(uuid.uuid4()), session_id, voice_session_id, it.realtime_item_id, it.role, it.text,
                         it.interrupted))
                    n += cur.rowcount
        return n

    async def take_unmerged(self, session_id: str, turn_id: str, covered: CoveredItem | None) -> list[MergedItem]:
        """What: in one transaction: upsert the covered item as merged into this turn, then claim every unmerged
        client item of the session for this turn. Use when: step 12 (under the turn lock, so turns never race).
        Inputs: session, turn, covered item. Output: claimed items, oldest first. Side effects: INSERT, UPDATE.
        Role: write ("merge once": merged_into_turn_id is set exactly once per item)."""
        async with self.pool.connection() as conn:
            async with conn.transaction():
                if covered is not None:
                    await conn.execute(
                        "INSERT INTO agent.transcript_items (id, session_id, voice_session_id, realtime_item_id, "
                        "turn_id, role, mode, source, text, interrupted, merged_into_turn_id, created_at) "
                        "VALUES (%s, %s, %s, %s, %s, 'user', 'voice', 'client', %s, false, %s, clock_timestamp()) "
                        "ON CONFLICT (session_id, realtime_item_id) DO UPDATE SET turn_id = EXCLUDED.turn_id, "
                        "merged_into_turn_id = COALESCE(agent.transcript_items.merged_into_turn_id, "
                        "EXCLUDED.merged_into_turn_id)",
                        (str(uuid.uuid4()), session_id, covered.voice_session_id, covered.realtime_item_id, turn_id,
                         covered.text, turn_id))
                cur = await conn.execute(
                    "UPDATE agent.transcript_items SET merged_into_turn_id = %s WHERE session_id = %s "
                    "AND source = 'client' AND merged_into_turn_id IS NULL "
                    "RETURNING role, text, interrupted, created_at, id",
                    (turn_id, session_id))
                rows = await cur.fetchall()
        rows.sort(key=lambda r: (r[3], str(r[4])))
        return [MergedItem(r[0], r[1], r[2]) for r in rows]


async def thread_summary(checkpointer: Any, session_id: str, context: RedactContext) -> SessionSummary:
    """What: read the session's checkpointed thread and build the Realtime session summary (2 s cap).
    Use when: production VoiceDeps.summary. Inputs: LangGraph saver (or None), session id, RedactContext.
    Output: SessionSummary (empty on a new session, no checkpointer or a read failure). Side effects: one read.
    Role: fail open: a missing summary never blocks a voice call."""
    if checkpointer is None:
        return SessionSummary()
    try:
        async with asyncio.timeout(THREAD_READ_TIMEOUT_S):
            tup = await checkpointer.aget_tuple(run_config(session_id))
    except Exception:  # noqa: BLE001
        log.warning("thread read for the voice summary failed session_id=%s", session_id, exc_info=True)
        return SessionSummary()
    return summary_from_thread(tup.checkpoint.get("channel_values") if tup else None, context)


class EmptyBody(StrictModel):
    """What: the `{}` body of POST voice/token. Use when: validating it. Inputs: none.
    Output: model that rejects any field. Side effects: none. Role: doc 07 s2.2 request shape."""


def _redact_context(session: SessionRow) -> RedactContext:
    """What: the scanner context for this session. Use when: redacting posted transcript text.
    Inputs: session row. Output: RedactContext. Side effects: none.
    Role: the s11.1 PIN scanner (CS-E-03.S7.T9) runs through contracts.redact.scan_text."""
    return RedactContext(awaiting_verification=session.awaiting_verification,
                         verified=session.verified_user_id is not None)


def _relay_ready(deps: VoiceDeps | None) -> bool:
    """What: whether voice/ask can run a turn: an Ingress with its call keys and transcript merge attached.
    Use when: voice/token (never mint for a dead relay, review M1) and voice/ask (never run voice without the
    step 7 dedupe, review M10). Inputs: deps. Output: bool. Side effects: none. Role: guard."""
    ing = deps.ingress if deps is not None else None
    return ing is not None and ing.call_keys is not None and ing.merge_transcript is not None


def _unavailable(request: Request, class_: str = "provider_5xx") -> JSONResponse:
    """What: 503 VOICE_UNAVAILABLE envelope. Use when: mint or wiring fails. Inputs: request, error class.
    Output: response. Side effects: none. Role: helper (chat keeps working; the browser says voice is off)."""
    return error_response(request, 503, class_, "VOICE_UNAVAILABLE",
                          "Voice is not available right now. You can keep chatting here.",
                          retryable=True, retry_after_s=5)


def install_voice(app: FastAPI) -> None:
    """What: register the three voice routes. Use when: once in create_app.
    Inputs: app (reads app.state.voice and app.state.session_store). Output: none. Side effects: mutates app.
    Role: route wiring (the access gate already ran: cookie and Origin)."""

    @app.post("/api/sessions/{sid}/voice/token", dependencies=[Depends(limit_voice_token)])
    async def post_voice_token(sid: str, _: EmptyBody, request: Request) -> Any:
        """What: mint a Realtime client secret for this session and record the voice session.
        Use when: the mic click. Inputs: session id, `{}`. Output: VoiceToken (max_session_s 600).
        Side effects: one OpenAI call, INSERT agent.voice_sessions. Role: doc 07 s2.2, s6.6."""
        owned = await owned_session(request.app.state.session_store, sid, request.state.visitor_id)
        sid = owned.session_id  # canonical spelling from the row, never the path alias (review M4)
        deps: VoiceDeps | None = getattr(request.app.state, "voice", None)
        if deps is None or deps.voice_spec is None or not _relay_ready(deps):
            return _unavailable(request)
        summary = await deps.summary(sid, _redact_context(owned))
        session = build_session_config(deps.voice_spec, render_instructions(summary))
        try:
            secret = await deps.minter.mint(session)
        except MintError as exc:
            log.warning("voice mint failed session_id=%s class=%s status=%s", sid, exc.class_, exc.status)
            return _unavailable(request, exc.class_)
        minted = deps.now()
        row = VoiceSessionRow(str(uuid7()), sid, minted, minted + timedelta(seconds=limits.VOICE_SESSION_MAX_S))
        await deps.store.create_voice_session(row)
        out = VoiceToken(client_secret=secret.value, expires_at=secret.expires_at.isoformat(),
                         model=deps.voice_spec.model, voice_session_id=row.voice_session_id,
                         max_session_s=limits.VOICE_SESSION_MAX_S)
        return JSONResponse(out.model_dump(mode="json"), headers={"Cache-Control": "no-store"})

    @app.post("/api/sessions/{sid}/voice/ask", dependencies=[Depends(limit_turn)])
    async def post_voice_ask(sid: str, body: VoiceAskRequest, request: Request) -> Any:
        """What: run one voice turn: step 3 here, then the shared ingress with mode="voice" (steps 4 to 16).
        Use when: the browser relays an ask_support_agent function call.
        Inputs: session id, VoiceAskRequest. Output: text/event-stream; turn.completed.final.say is spoken.
        Side effects: a full agent turn. Role: doc 07 s2.2, s2.4 (20 s deadline from TURN_DEADLINE_S)."""
        owned = await owned_session(request.app.state.session_store, sid, request.state.visitor_id)
        sid = owned.session_id  # canonical (review M4)
        deps: VoiceDeps | None = getattr(request.app.state, "voice", None)
        if deps is None or deps.ingress is None or not _relay_ready(deps):
            return _unavailable(request)
        vs = await deps.store.get_voice_session(body.voice_session_id)
        live = (vs is not None and vs.session_id == sid
                and deps.now() - vs.minted_at < timedelta(seconds=limits.VOICE_SESSION_MAX_S))
        if not live:  # step 3; no retry_after_s: retrying cannot succeed, a new call is needed (review M6)
            return error_response(request, 429, "rate_limited", "VOICE_SESSION_EXPIRED",
                                  "This voice call has ended. Start a new one to keep talking.")
        result = await deps.ingress.handle(IngressInput(
            session_id=sid, visitor_id=request.state.visitor_id, client_turn_id=str(body.client_turn_id),
            mode="voice", input_kind="voice", text=body.question, realtime_call_id=body.realtime_call_id,
            body=body, redact_context=_redact_context(owned)))
        return to_response(request, result)

    @app.post("/api/sessions/{sid}/transcript", status_code=204, dependencies=[Depends(limit_session_post)])
    async def post_transcript(sid: str, body: TranscriptPost, request: Request) -> Response:
        """What: store client-reported voice chatter for the step 12 merge.
        Use when: the browser flushes final transcription items. Inputs: session id, TranscriptPost.
        Output: 204. Side effects: INSERT agent.transcript_items (redacted, re-posts ignored).
        Role: doc 07 s2.2, s2.6 (untrusted display text)."""
        session = await owned_session(request.app.state.session_store, sid, request.state.visitor_id)
        sid = session.session_id  # canonical (review M4)
        deps: VoiceDeps | None = getattr(request.app.state, "voice", None)
        if deps is None:
            return _unavailable(request)
        vs = await deps.store.get_voice_session(body.voice_session_id)
        if vs is None or vs.session_id != sid:
            return error_response(request, 404, "not_found", "NOT_FOUND", "Not found.")
        ctx = _redact_context(session)
        clean = [it.model_copy(update={"text": redaction.redact(it.text, ctx)}) for it in body.items]  # I1
        await deps.store.add_transcript_items(sid, vs.voice_session_id, clean)
        return Response(status_code=204)
