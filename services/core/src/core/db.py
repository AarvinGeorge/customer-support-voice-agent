"""What: the core database handle (psycopg connection pool, app role) and the readiness queries.
Use when: the lifespan opens it and handlers or readyz run SQL.
Inputs: the CORE_DB_URL of role core_app. Output: Database.
Side effects: opens and closes a connection pool; reads tables.
Role: the only place core opens connections (sync driver; FastAPI runs `def` handlers in a thread pool)."""
from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

import psycopg
from psycopg_pool import ConnectionPool


class Database:
    """What: wraps a ConnectionPool. Use when: any SQL. Inputs: url, max_size.
    Output: connections inside a transaction via tx(). Side effects: network connections.
    Role: one pool per process, opened in the lifespan and closed at shutdown."""

    def __init__(self, url: str, max_size: int = 10) -> None:
        """What: create a closed pool. Use when: building the app. Inputs: url, max_size. Output: none.
        Side effects: none until open(). Role: construction separate from connecting."""
        self._pool = ConnectionPool(url, min_size=1, max_size=max_size, open=False, kwargs={"autocommit": False})

    def open(self) -> None:
        """What: connect and wait for a first connection. Use when: lifespan start. Inputs: none.
        Output: none. Side effects: connects; raises on failure so a bad URL stops startup. Role: fail loudly."""
        self._pool.open(wait=True, timeout=15)

    def close(self) -> None:
        """What: close the pool. Use when: lifespan end. Inputs: none. Output: none.
        Side effects: closes connections. Role: clean shutdown."""
        self._pool.close()

    @contextmanager
    def tx(self) -> Iterator[psycopg.Connection]:
        """What: borrow a connection; commit on success, roll back on error.
        Use when: any unit of work. Inputs: none. Output: connection.
        Side effects: one transaction. Role: every write path runs inside this."""
        with self._pool.connection() as conn:
            yield conn

    def seed_date(self) -> str:
        """What: the date the data was last seeded ("YYYY-MM-DD") or "unseeded".
        Use when: /readyz. Inputs: none. Output: str. Side effects: one SELECT; raises psycopg errors if the
        database or the table is unavailable. Role: doc 07 s3.6 seed_date (stored in core.meta by the seeder)."""
        with self.tx() as conn:
            row = conn.execute("SELECT value FROM meta WHERE key = 'seed_date'").fetchone()
        return row[0] if row else "unseeded"
