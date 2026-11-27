"""What: the knowledge database handle (psycopg, one short connection per call).
Use when: readyz pings the database; GET /v1/corpus lists articles. Search and ingest add methods later.
Inputs: KNOWLEDGE_DB_URL (the knowledge_app role). Output: Database with ping() and corpus().
Side effects: opens a connection per call with a 1 s connect timeout and statement_timeout 1 s.
Role: the only module that talks to Postgres; unit tests replace Database with a fake."""
from __future__ import annotations

import psycopg
from psycopg.rows import dict_row


class Database:
    """What: thin Postgres access for the knowledge service. Use when: routes need rows.
    Inputs: database url. Output: instance. Side effects: none until a method is called.
    Role: keeps SQL out of routes."""

    def __init__(self, url: str):
        """What: store the url. Use when: lifespan start. Inputs: url. Output: none. Side effects: none.
        Role: constructor."""
        self.url = url

    def _connect(self) -> psycopg.Connection:
        """What: open a connection with short timeouts. Use when: any method. Inputs: none.
        Output: connection (dict rows). Side effects: network. Role: shared connect settings."""
        return psycopg.connect(self.url, row_factory=dict_row, connect_timeout=1,
                               options="-c statement_timeout=1000")

    def ping(self) -> bool:
        """What: check the database answers. Use when: /readyz. Inputs: none. Output: True or False.
        Side effects: one SELECT 1. Role: minimal readiness (chunk count and calibration arrive with CS-E-02.S1.T6)."""
        try:
            with self._connect() as conn:
                conn.execute("SELECT 1")
            return True
        except psycopg.Error:
            return False

    def corpus(self) -> dict:
        """What: list articles with chunk counts and the latest ok ingest. Use when: GET /v1/corpus.
        Inputs: none. Output: dict per doc 07 s4.2 (corpus_version "none" until an ingest has run).
        Side effects: two SELECTs. Role: corpus read."""
        with self._connect() as conn:
            arts = conn.execute(
                "SELECT a.article_slug AS slug, a.title, a.doc_version, a.last_updated::text AS last_updated, "
                "count(c.chunk_id)::int AS chunks FROM articles a LEFT JOIN chunks c USING (article_slug) "
                "GROUP BY a.article_slug ORDER BY a.article_slug").fetchall()
            run = conn.execute("SELECT corpus_version, finished_at FROM ingest_runs WHERE status = 'ok' "
                               "ORDER BY finished_at DESC LIMIT 1").fetchone()
        return {"corpus_version": (run["corpus_version"] if run else None) or "none", "articles": arts,
                "last_ingest_at": run["finished_at"].isoformat() if run else None}
