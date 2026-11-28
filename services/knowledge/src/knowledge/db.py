"""What: the knowledge database handle (psycopg, one short connection per call).
Use when: readyz reads status; GET /v1/corpus lists articles. Search and ingest add methods later.
Inputs: KNOWLEDGE_DB_URL (the knowledge_app role). Output: Database with status() and corpus().
Side effects: opens a connection per call with a 1 s connect timeout and statement_timeout 1 s.
Role: the only module that talks to Postgres; unit tests replace Database with a fake."""
from __future__ import annotations

import time
from typing import Any, Callable

import psycopg
from psycopg.rows import dict_row


CACHE_TTL_S = 30.0
_LEG_COLUMNS = ("SELECT c.chunk_id, c.article_slug, a.title AS article_title, c.section_path, c.heading, c.header, "
                "c.body, c.tiers, c.error_codes, c.policy_keys, c.injection_suspect, c.doc_version, "
                "c.last_updated::text AS last_updated, c.content_hash, c.source_url")


class Database:
    """What: thin Postgres access for the knowledge service. Use when: routes need rows.
    Inputs: database url. Output: instance. Side effects: none until a method is called.
    Role: keeps SQL out of routes."""

    def __init__(self, url: str):
        """What: store the url. Use when: lifespan start. Inputs: url. Output: none. Side effects: none.
        Role: constructor."""
        self.url = url
        self._cache: dict[str, tuple[float, Any]] = {}

    def _cached(self, key: str, load: Callable[[], Any]) -> Any:
        """What: a value kept for CACHE_TTL_S seconds. Use when: vocabulary and corpus_version are read on every
        search. Inputs: key, loader. Output: the value; a failing refresh returns the stale value if there is one,
        else re-raises. Side effects: may run the loader (one SELECT). Role: removes two round trips per request."""
        hit = self._cache.get(key)
        if hit and time.monotonic() - hit[0] < CACHE_TTL_S:
            return hit[1]
        try:
            value = load()
        except Exception:
            if hit:
                return hit[1]
            raise
        self._cache[key] = (time.monotonic(), value)
        return value

    def _connect(self) -> psycopg.Connection[dict[str, Any]]:
        """What: open a connection with short timeouts. Use when: any method. Inputs: none.
        Output: connection (dict rows). Side effects: network. Role: shared connect settings."""
        return psycopg.connect(self.url, row_factory=dict_row, connect_timeout=1,  # pyright: ignore[reportArgumentType, reportReturnType]
                               options="-c statement_timeout=1000")

    def status(self) -> dict | None:
        """What: readiness facts: chunk count and the corpus_version of the latest finished run (ok or failed). Use when: /readyz.
        Inputs: none. Output: {"chunks": int, "corpus_version": str} or None when the database does not answer.
        Side effects: two SELECTs. Role: doc 07 s4.2 readiness (calibration check arrives with CS-E-02.S3.T3)."""
        try:
            with self._connect() as conn:
                n = conn.execute("SELECT count(*)::int AS n FROM chunks").fetchone()["n"]  # pyright: ignore[reportOptionalSubscript]
                run = conn.execute("SELECT corpus_version FROM ingest_runs WHERE corpus_version IS NOT NULL "
                                   "ORDER BY finished_at DESC LIMIT 1").fetchone()
            return {"chunks": n, "corpus_version": (run["corpus_version"] if run else None) or "none"}
        except psycopg.Error:
            return None

    def vocabulary(self) -> set[str]:
        """What: every error code found in the corpus (the closed vocabulary). Use when: normalizing a query.
        Inputs: none. Output: set of codes, cached 30 s. Side effects: one SELECT when the cache is cold.
        Role: draft A decision A-8."""
        def load() -> set[str]:
            """What: read the codes. Use when: cache miss. Inputs: none. Output: set. Side effects: one SELECT.
            Role: loader."""
            with self._connect() as conn:
                rows = conn.execute("SELECT DISTINCT unnest(error_codes) AS code FROM chunks").fetchall()
            return {r["code"] for r in rows}
        return self._cached("vocabulary", load)

    def corpus_version(self) -> str:
        """What: the corpus_version of the latest finished ingest run ("none" before any). Use when: search labels
        its response. Inputs: none. Output: str, cached 30 s. Side effects: one SELECT when the cache is cold.
        Role: cheap version read for the hot path (readyz uses status(), uncached)."""
        def load() -> str:
            """What: read the version. Use when: cache miss. Inputs: none. Output: str. Side effects: one SELECT.
            Role: loader."""
            with self._connect() as conn:
                run = conn.execute("SELECT corpus_version FROM ingest_runs WHERE corpus_version IS NOT NULL "
                                   "ORDER BY finished_at DESC LIMIT 1").fetchone()
            return (run["corpus_version"] if run else None) or "none"
        return self._cached("corpus_version", load)

    def vector_leg(self, vec: list[float], tiers: list[str], limit: int) -> list[dict]:
        """What: nearest chunks by cosine within the allowed tiers. Use when: search has a query vector.
        Inputs: vector, tiers, limit. Output: rows with cosine and v_rank (1 is nearest). Side effects: one
        transaction with hnsw.iterative_scan = strict_order and statement_timeout 1 s. Role: vector leg (doc 07 s4.1)."""
        from knowledge.ingest.store import vector_text
        with self._connect() as conn, conn.transaction():
            conn.execute("SET LOCAL hnsw.iterative_scan = strict_order")
            conn.execute("SET LOCAL statement_timeout = '1000ms'")
            return conn.execute(
                _LEG_COLUMNS + ", 1 - (c.embedding <=> %(v)s::vector) AS cosine, "
                "row_number() OVER (ORDER BY c.embedding <=> %(v)s::vector)::int AS v_rank "
                "FROM chunks c JOIN articles a USING (article_slug) WHERE c.tiers && %(tiers)s::text[] "
                "ORDER BY c.embedding <=> %(v)s::vector LIMIT %(n)s",
                {"v": vector_text(vec), "tiers": tiers, "n": limit}).fetchall()

    def cosines(self, vec: list[float], chunk_ids: list[str]) -> dict[str, float]:
        """What: the cosine of one vector against the given chunks. Use when: search scores the customer message
        against the chunks it is about to return (CS-E-02.F11). Inputs: vector, chunk ids (the fused top k).
        Output: {chunk_id: cosine}; {} for no ids. Side effects: one SELECT, statement_timeout 1 s.
        Role: Gate 1 input; reads k rows by primary key, no ranking."""
        from knowledge.ingest.store import vector_text
        if not chunk_ids:
            return {}
        with self._connect() as conn, conn.transaction():
            conn.execute("SET LOCAL statement_timeout = '1000ms'")
            rows = conn.execute("SELECT chunk_id, 1 - (embedding <=> %(v)s::vector) AS cosine FROM chunks "
                                "WHERE chunk_id = ANY(%(ids)s::text[])",
                                {"v": vector_text(vec), "ids": list(chunk_ids)}).fetchall()
        return {r["chunk_id"]: float(r["cosine"]) for r in rows}

    def keyword_leg(self, text: str, codes: list[str], tiers: list[str], vec: list[float] | None, limit: int) -> list[dict]:
        """What: full-text matches within the allowed tiers. Use when: always (also when the embedding is down).
        Inputs: normalized text, detected codes, tiers, query vector or None, limit. Output: rows with f_rank
        and cosine (0 without a vector). Side effects: one SELECT, statement_timeout 1 s. Role: keyword leg: an OR of
        the query's own lexemes plus an exact phrase per code (decision A-7); nothing to search gives no rows."""
        from knowledge.ingest.store import vector_text
        cosine = "1 - (c.embedding <=> %(v)s::vector)" if vec is not None else "0.0"
        with self._connect() as conn, conn.transaction():
            conn.execute("SET LOCAL statement_timeout = '1000ms'")
            return conn.execute(
                "WITH tsq AS (SELECT NULLIF(concat_ws(' | ', "
                "(SELECT string_agg(quote_literal(lexeme), ' | ') FROM unnest(to_tsvector('english', %(q)s))), "
                "(SELECT string_agg('(' || phraseto_tsquery('simple', cd)::text || ')', ' | ') "
                "FROM unnest(%(codes)s::text[]) AS cd)), '')::tsquery AS query) "
                + _LEG_COLUMNS + f", {cosine} AS cosine, "
                "row_number() OVER (ORDER BY ts_rank_cd(c.tsv, tsq.query) DESC, c.chunk_id)::int AS f_rank "
                "FROM chunks c JOIN articles a USING (article_slug), tsq "
                "WHERE tsq.query IS NOT NULL AND c.tiers && %(tiers)s::text[] AND c.tsv @@ tsq.query "
                "ORDER BY ts_rank_cd(c.tsv, tsq.query) DESC, c.chunk_id LIMIT %(n)s",
                {"q": text, "codes": codes, "tiers": tiers, "n": limit,
                 **({"v": vector_text(vec)} if vec is not None else {})}).fetchall()

    def corpus(self) -> dict:
        """What: list articles with chunk counts and the latest ok ingest. Use when: GET /v1/corpus.
        Inputs: none. Output: dict per doc 07 s4.2 (corpus_version "none" until an ingest has run).
        Side effects: two SELECTs. Role: corpus read."""
        with self._connect() as conn:
            arts = conn.execute(
                "SELECT a.article_slug AS slug, a.title, a.doc_version, a.last_updated::text AS last_updated, "
                "count(c.chunk_id)::int AS chunks FROM articles a LEFT JOIN chunks c USING (article_slug) "
                "GROUP BY a.article_slug ORDER BY a.article_slug").fetchall()
            run = conn.execute("SELECT corpus_version, finished_at FROM ingest_runs WHERE corpus_version IS NOT NULL "
                               "ORDER BY finished_at DESC LIMIT 1").fetchone()
        return {"corpus_version": (run["corpus_version"] if run else None) or "none", "articles": arts,
                "last_ingest_at": run["finished_at"].isoformat() if run else None}
