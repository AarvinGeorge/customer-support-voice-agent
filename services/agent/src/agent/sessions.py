"""What: session storage for the public API: create a session bound to a visitor, read one for ownership checks.
Use when: POST /api/sessions and any route that names a session (through access.owned_session).
Inputs: a psycopg AsyncConnectionPool for the agent_app role.
Output: PgSessionStore (create_session, get_session).
Side effects: INSERT and SELECT on agent.sessions.
Role: storage adapter; tests use an in-memory fake with the same two methods.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Protocol

from langchain_core.utils.uuid import uuid7

from agent.ingress import PgTurnStore, SessionRow


class SessionStore(Protocol):
    """What: the two methods the API needs. Use when: typing app.state.session_store.
    Inputs: see methods. Output: see methods. Side effects: see methods. Role: structural type."""

    async def create_session(self, visitor_id: str, ip_hash: str | None) -> tuple[str, datetime]: ...
    async def get_session(self, session_id: str) -> SessionRow | None: ...


class PgSessionStore:
    """What: SessionStore over the agent_app pool. Use when: production.
    Inputs: pool. Output: store. Side effects: SQL on agent.sessions. Role: adapter."""

    def __init__(self, pool: Any) -> None:
        """What: keep the pool. Use when: app start. Inputs: pool. Output: none. Side effects: none. Role: constructor."""
        self.pool = pool
        self._reads = PgTurnStore(pool)

    async def create_session(self, visitor_id: str, ip_hash: str | None) -> tuple[str, datetime]:
        """What: insert a session for this visitor. Use when: POST /api/sessions.
        Inputs: visitor id, optional salted IP hash. Output: (session_id, created_at).
        Side effects: INSERT. Role: session start."""
        sid = str(uuid7())  # doc 07 s1.2: session ids are uuid7
        async with self.pool.connection() as conn:
            cur = await conn.execute("INSERT INTO agent.sessions (session_id, visitor_id, ip_hash) "
                                     "VALUES (%s, %s, %s) RETURNING created_at", (sid, visitor_id, ip_hash))
            row = await cur.fetchone()
        return sid, row[0]

    async def get_session(self, session_id: str) -> SessionRow | None:
        """What: read one session. Use when: ownership check. Inputs: id. Output: row or None.
        Side effects: SELECT. Role: read (shares the ingress query)."""
        return await self._reads.get_session(session_id)
