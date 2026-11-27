"""What: in-memory VoiceStore, call keys and Realtime minter with the same contract as the Postgres, Redis and
OpenAI versions. Use when: voice endpoint tests. Inputs: none. Output: MemoryVoiceStore, MemoryCallKeys, FakeMinter.
Side effects: none. Role: test doubles (PgVoiceStore SQL is checked against Postgres separately, see the T2 report)."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from contracts.agent_api import TranscriptItemIn

from agent.realtime import MintedSecret, MintError
from agent.voice import CoveredItem, MergedItem, VoiceSessionRow


@dataclass
class _Item:
    """What: one transcript_items row. Use when: MemoryVoiceStore. Inputs: columns. Output: value.
    Side effects: none. Role: fake row."""

    session_id: str
    realtime_item_id: str
    role: str
    text: str
    interrupted: bool
    created_at: datetime
    voice_session_id: str | None = None
    turn_id: str | None = None
    merged_into_turn_id: str | None = None


class MemoryVoiceStore:
    """What: dict-backed agent.voice_sessions and agent.transcript_items. Use when: tests.
    Inputs: none. Output: VoiceStore methods. Side effects: mutates its dicts. Role: fake."""

    def __init__(self) -> None:
        """What: empty tables. Use when: tests. Inputs: none. Output: none. Side effects: none. Role: fake."""
        self.voice: dict[str, VoiceSessionRow] = {}
        self.items: list[_Item] = []
        self.tick = 0

    async def create_voice_session(self, row: VoiceSessionRow) -> None:
        """What: insert. Use when: voice/token. Inputs: row. Output: none. Side effects: stores. Role: fake."""
        self.voice[row.voice_session_id] = row

    async def get_voice_session(self, voice_session_id: str) -> VoiceSessionRow | None:
        """What: lookup. Use when: step 3. Inputs: id. Output: row or None. Side effects: none. Role: fake."""
        return self.voice.get(voice_session_id)

    def _now(self) -> datetime:
        """What: strictly increasing created_at. Use when: inserts. Inputs: none. Output: time.
        Side effects: tick. Role: fake clock for ordering."""
        self.tick += 1
        return datetime(2026, 11, 28, 13, 0, tzinfo=UTC) + timedelta(milliseconds=self.tick)

    def _find(self, session_id: str, item_id: str) -> _Item | None:
        """What: unique (session_id, realtime_item_id) lookup. Use when: inserts. Inputs: ids. Output: row or None.
        Side effects: none. Role: fake unique key."""
        return next((i for i in self.items if i.session_id == session_id and i.realtime_item_id == item_id), None)

    async def add_transcript_items(self, session_id: str, voice_session_id: str,
                                   items: list[TranscriptItemIn]) -> int:
        """What: insert, ON CONFLICT DO NOTHING. Use when: POST /transcript. Inputs: ids, items (already redacted).
        Output: rows inserted. Side effects: stores. Role: fake."""
        n = 0
        for it in items:
            if self._find(session_id, it.realtime_item_id) is None:
                self.items.append(_Item(session_id, it.realtime_item_id, it.role, it.text, it.interrupted,
                                        self._now(), voice_session_id))
                n += 1
        return n

    async def take_unmerged(self, session_id: str, turn_id: str, covered: CoveredItem | None) -> list[MergedItem]:
        """What: mark the covered item merged, then claim every unmerged client item for this turn.
        Use when: ingress step 12. Inputs: session, turn, covered item. Output: claimed items, oldest first.
        Side effects: updates rows. Role: fake of the PgVoiceStore transaction."""
        if covered is not None:
            row = self._find(session_id, covered.realtime_item_id)
            if row is None:
                self.items.append(_Item(session_id, covered.realtime_item_id, "user", covered.text, False,
                                        self._now(), covered.voice_session_id, turn_id, turn_id))
            else:
                row.turn_id = turn_id
                row.merged_into_turn_id = row.merged_into_turn_id or turn_id
        out = []
        for i in sorted(self.items, key=lambda x: x.created_at):
            if i.session_id == session_id and i.merged_into_turn_id is None:
                i.merged_into_turn_id = turn_id
                out.append(MergedItem(i.role, i.text, i.interrupted))
        return out


class MemoryCallKeys:
    """What: SET voice_call:{sid}:{call} NX. Use when: tests. Inputs: optional failure. Output: claim().
    Side effects: mutates its set. Role: fake of RedisCallKeys."""

    def __init__(self, fail: Exception | None = None) -> None:
        """What: empty key set. Use when: tests. Inputs: exception to raise. Output: none. Side effects: none.
        Role: fake."""
        self.keys: set[str] = set()
        self.fail = fail

    async def claim(self, session_id: str, call_id: str) -> bool:
        """What: claim once. Use when: step 7. Inputs: ids. Output: True when new. Side effects: adds the key.
        Role: fake."""
        if self.fail:
            raise self.fail
        key = f"voice_call:{session_id}:{call_id}"
        if key in self.keys:
            return False
        self.keys.add(key)
        return True


@dataclass
class FakeMinter:
    """What: records sessions and returns a fixed secret, or raises. Use when: voice/token tests.
    Inputs: optional MintError. Output: mint(). Side effects: records. Role: fake of OpenAIRealtimeMinter."""

    error: MintError | None = None
    sessions: list[dict[str, Any]] = field(default_factory=list)

    async def mint(self, session: dict[str, Any]) -> MintedSecret:
        """What: fake mint. Use when: voice/token. Inputs: session config. Output: MintedSecret.
        Side effects: records the session. Role: fake."""
        self.sessions.append(session)
        if self.error:
            raise self.error
        return MintedSecret(value="ek_fake_secret", expires_at=datetime(2026, 11, 28, 13, 10, tzinfo=UTC))
