"""What: PgStore, the Postgres side of the ingest job (the Store interface the job calls).
Use when: the ingest job runs for real (KNOWLEDGE_DB_URL, the knowledge_app role).
Inputs: a database url. Output: methods start_run, finish_run, existing_article, existing_vectors, swap_article,
delete_missing_articles, corpus_pairs. Side effects: SQL against schema knowledge (search_path of the role).
Role: all ingest SQL in one place. swap_article is one transaction per article so a failure leaves the old version served.
Vectors travel as text ('[0.1,...]' cast to vector) so no pgvector Python package is needed."""
from __future__ import annotations

from typing import Any

import psycopg
from psycopg.rows import dict_row

_INSERT_CHUNK = """
INSERT INTO chunks (chunk_id, article_slug, ordinal, section_path, heading, header, body, source_url, tiers,
  error_codes, policy_keys, injection_suspect, token_count, content_hash, embedding_model, embedding, tsv,
  doc_version, last_updated)
VALUES (%(chunk_id)s, %(article_slug)s, %(ordinal)s, %(section_path)s::text[], %(heading)s, %(header)s, %(body)s,
  %(source_url)s, %(tiers)s::text[], %(error_codes)s::text[], %(policy_keys)s::text[], %(injection_suspect)s, %(token_count)s,
  %(content_hash)s, %(embedding_model)s, %(embedding)s::vector,
  setweight(to_tsvector('english', %(title)s || ' ' || array_to_string(%(section_path)s::text[], ' ')), 'A')
  || setweight(to_tsvector('simple', array_to_string(%(error_codes)s::text[], ' ')), 'A')
  || setweight(to_tsvector('english', %(body)s), 'D'),
  %(doc_version)s, %(last_updated)s::date)
ON CONFLICT (chunk_id) DO UPDATE SET
  article_slug = EXCLUDED.article_slug, ordinal = EXCLUDED.ordinal, section_path = EXCLUDED.section_path,
  heading = EXCLUDED.heading, header = EXCLUDED.header, body = EXCLUDED.body, source_url = EXCLUDED.source_url,
  tiers = EXCLUDED.tiers, error_codes = EXCLUDED.error_codes, policy_keys = EXCLUDED.policy_keys,
  injection_suspect = EXCLUDED.injection_suspect, token_count = EXCLUDED.token_count,
  content_hash = EXCLUDED.content_hash, embedding_model = EXCLUDED.embedding_model,
  embedding = EXCLUDED.embedding, tsv = EXCLUDED.tsv, doc_version = EXCLUDED.doc_version,
  last_updated = EXCLUDED.last_updated, ingested_at = now()
"""


def vector_text(vec: list[float]) -> str:
    """What: format a vector for a ::vector cast. Use when: inserting. Inputs: floats. Output: '[a,b,...]'.
    Side effects: none. Role: pgvector text input format."""
    return "[" + ",".join(repr(float(x)) for x in vec) + "]"


def parse_vector(text: str) -> list[float]:
    """What: parse pgvector's text output. Use when: reusing stored embeddings. Inputs: '[a,b,...]'.
    Output: floats. Side effects: none. Role: inverse of vector_text."""
    return [float(x) for x in text.strip("[]").split(",")]


class PgStore:
    """What: Postgres implementation of the ingest Store. Use when: the real job runs. Inputs: url.
    Output: instance. Side effects: opens a connection per call. Role: see module docstring."""

    def __init__(self, url: str):
        """What: keep the url. Use when: construction. Inputs: url. Output: none. Side effects: none. Role: init."""
        self.url = url

    def _conn(self) -> psycopg.Connection[dict[str, Any]]:
        """What: open a connection. Use when: every method. Inputs: none. Output: connection (dict rows).
        Side effects: network. Role: shared settings."""
        return psycopg.connect(self.url, row_factory=dict_row)  # pyright: ignore[reportArgumentType, reportReturnType]

    def start_run(self, run_id: str, model: str) -> None:
        """What: insert a running ingest_runs row. Use when: job start. Inputs: id, model. Output: none.
        Side effects: INSERT, committed at once. Role: a failed run is still visible."""
        with self._conn() as c:
            c.execute("INSERT INTO ingest_runs (run_id, started_at, status, embedding_model) "
                      "VALUES (%s, now(), 'running', %s)", (run_id, model))

    def finish_run(self, run_id: str, status: str, corpus_version: str | None, counts: dict, error: str | None) -> None:
        """What: close the run row. Use when: job end. Inputs: id, status ok or failed, version, counts, error.
        Output: none. Side effects: UPDATE. Role: audit of every run."""
        with self._conn() as c:
            c.execute("UPDATE ingest_runs SET finished_at = now(), status = %s, corpus_version = %s, "
                      "articles_seen = %s, articles_changed = %s, chunks_embedded = %s, chunks_deleted = %s, "
                      "error = %s WHERE run_id = %s",
                      (status, corpus_version, counts.get("articles_seen"), counts.get("articles_changed"),
                       counts.get("chunks_embedded"), counts.get("chunks_deleted"), error, run_id))

    def existing_article(self, slug: str) -> tuple[str, str] | None:
        """What: stored (rendered_hash, embedding_model) for an article. Use when: the skip check.
        Inputs: slug. Output: tuple or None. Side effects: SELECT. Role: skip unchanged."""
        with self._conn() as c:
            r = c.execute("SELECT rendered_hash, embedding_model FROM articles WHERE article_slug = %s",
                          (slug,)).fetchone()
        return (r["rendered_hash"].strip(), r["embedding_model"]) if r else None

    def existing_vectors(self, hashes: list[str], model: str) -> dict[str, list[float]]:
        """What: stored vectors for content hashes and a model. Use when: reusing embeddings. Inputs: hashes,
        model. Output: {hash: vector}. Side effects: SELECT. Role: 'embed only new hashes'."""
        if not hashes:
            return {}
        with self._conn() as c:
            rows = c.execute("SELECT DISTINCT ON (content_hash) content_hash, embedding::text AS e FROM chunks "
                             "WHERE content_hash = ANY(%s) AND embedding_model = %s", (hashes, model)).fetchall()
        return {r["content_hash"].strip(): parse_vector(r["e"]) for r in rows}

    def swap_article(self, article: dict, chunk_rows: list[dict]) -> int:
        """What: replace one article and its chunks in one transaction. Use when: an article changed.
        Inputs: article row dict, chunk row dicts (with embedding as floats). Output: chunks deleted.
        Side effects: upsert article, upsert chunks, delete chunks no longer produced; rolls back on any error.
        Role: per-article swap (draft A step 6)."""
        with self._conn() as c, c.transaction():
            c.execute(
                "INSERT INTO articles (article_slug, title, source_path, source_url, tiers, doc_version, last_updated, "
                "source_hash, rendered_hash, embedding_model) VALUES (%(article_slug)s, %(title)s, %(source_path)s, "
                "%(source_url)s, %(tiers)s::text[], %(doc_version)s, %(last_updated)s, %(source_hash)s, %(rendered_hash)s, "
                "%(embedding_model)s) ON CONFLICT (article_slug) DO UPDATE SET title = EXCLUDED.title, "
                "source_path = EXCLUDED.source_path, source_url = EXCLUDED.source_url, tiers = EXCLUDED.tiers, "
                "doc_version = EXCLUDED.doc_version, last_updated = EXCLUDED.last_updated, "
                "source_hash = EXCLUDED.source_hash, rendered_hash = EXCLUDED.rendered_hash, "
                "embedding_model = EXCLUDED.embedding_model, ingested_at = now()", article)
            for row in chunk_rows:
                c.execute(_INSERT_CHUNK, {**row, "embedding": vector_text(row["embedding"]), "title": article["title"]})
            cur = c.execute("DELETE FROM chunks WHERE article_slug = %s AND NOT (chunk_id = ANY(%s))",
                            (article["article_slug"], [r["chunk_id"] for r in chunk_rows]))
            return cur.rowcount

    def delete_missing_articles(self, keep: set[str]) -> int:
        """What: delete articles not in keep (chunks cascade). Use when: end of run. Inputs: slugs to keep.
        Output: number of chunks removed. Side effects: DELETE in one transaction. Role: removed pages disappear."""
        with self._conn() as c, c.transaction():
            n = c.execute("SELECT count(*) AS n FROM chunks WHERE NOT (article_slug = ANY(%s))",
                          (sorted(keep),)).fetchone()["n"]  # pyright: ignore[reportOptionalSubscript]
            c.execute("DELETE FROM articles WHERE NOT (article_slug = ANY(%s))", (sorted(keep),))
        return n

    def corpus_pairs(self) -> list[tuple[str, str]]:
        """What: (chunk_id, content_hash) for every stored chunk. Use when: computing corpus_version.
        Inputs: none. Output: list. Side effects: SELECT. Role: corpus_version input."""
        with self._conn() as c:
            rows = c.execute("SELECT chunk_id, content_hash FROM chunks").fetchall()
        return [(r["chunk_id"], r["content_hash"].strip()) for r in rows]
