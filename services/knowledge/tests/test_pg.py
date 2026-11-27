"""What: Postgres-backed tests (marker `pg`): the tier filter in both legs, the HNSW iterative scan, known error
codes surfacing their chunk with a healthy vector leg, and the corpus_version reader after a failed run.
Use when: SQL in db.py, ingest/store.py or the migrations changes. Run: `scripts/pg_tests.sh` (starts a throwaway
Compose Postgres, sets KNOWLEDGE_PG_TEST_URL, removes the stack). CI job "pg tests" runs them.
Inputs: the real article corpus, ingested with a feature-hashing fake embedder (no OpenAI calls). Output: pass or fail.
Side effects: DELETEs every article, chunk and ingest run in the target database, so it refuses to run unless
KNOWLEDGE_PG_TEST_URL is set. Role: closes review-pr15 Important 3 (the access filter had no automated test)."""
import asyncio
import hashlib
import math
import os
import re
from pathlib import Path

import pytest
from contracts.knowledge_api import SearchRequest

from knowledge import search
from knowledge.db import Database
from knowledge.ingest.job import corpus_version, run_ingest
from knowledge.ingest.store import PgStore, vector_text

pytestmark = pytest.mark.pg

ROOT = Path(__file__).resolve().parents[3]
URL = os.environ.get("KNOWLEDGE_PG_TEST_URL", "")
TIERS = ["free", "pro", "business", "enterprise"]


def hashed(text: str) -> list[float]:
    """What: a deterministic 1536-float unit vector from the words of a text (feature hashing). Use when: fixture
    embeddings. Inputs: text. Output: list of floats. Side effects: none. Role: stand-in for a real embedding so
    related texts are near each other without any network call."""
    v = [0.0] * 1536
    for w in re.findall(r"[a-z0-9]+", text.lower()):
        v[int(hashlib.sha256(w.encode()).hexdigest()[:8], 16) % 1536] += 1.0
    n = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / n for x in v]


class HashEmbedder:
    """What: embedder with the interface of OpenAIEmbedder, backed by `hashed`. Use when: ingest and search in
    these tests. Inputs: none. Output: vectors; counters. Side effects: none. Role: zero real calls."""

    def __init__(self):
        """What: zero the counters. Use when: construction. Inputs: none. Output: none. Side effects: none. Role: init."""
        self.calls = 0
        self.inputs = 0

    def embed_batch(self, texts):
        """What: vectors for texts. Use when: ingest. Inputs: texts. Output: list. Side effects: counts. Role: fake."""
        if texts:
            self.calls += 1
        self.inputs += len(texts)
        return [hashed(t) for t in texts]

    async def aembed_one(self, text, timeout_s):
        """What: one vector. Use when: search. Inputs: text, timeout. Output: vector. Side effects: counts. Role: fake."""
        self.calls += 1
        self.inputs += 1
        return hashed(text)


@pytest.fixture(scope="module")
def db():
    """What: a Database on a freshly ingested real corpus. Use when: every test here. Inputs: env
    KNOWLEDGE_PG_TEST_URL. Output: Database. Side effects: wipes and refills the knowledge tables once per module.
    Role: real SQL under test; skips with a clear reason when no test database is configured."""
    if not URL:
        pytest.skip("KNOWLEDGE_PG_TEST_URL not set; run scripts/pg_tests.sh")
    store = PgStore(URL)
    with store._conn() as c:
        c.execute("DELETE FROM articles")
        c.execute("DELETE FROM ingest_runs")
    run_ingest(ROOT / "data" / "articles", ROOT / "data" / "policy.yaml", store, HashEmbedder())
    return Database(URL)


def query_all(db: Database, sql: str, *args):
    """What: run one SELECT and return its rows. Use when: a test needs ground truth from the table. Inputs: db, sql,
    params. Output: list of dict rows. Side effects: one SELECT. Role: test helper."""
    with db._connect() as conn:
        return conn.execute(sql, args).fetchall()  # pyright: ignore[reportArgumentType]


def run(db: Database, **kw):
    """What: run one search against the real database and the hash embedder. Use when: search-level tests.
    Inputs: SearchRequest fields. Output: SearchResponse. Side effects: SQL. Role: test helper."""
    req = SearchRequest(**{"query": "help", "tier": "public", "verified": False, "k": 5, **kw})
    return asyncio.run(search.run_search(req, db, HashEmbedder(), "req", "trace"))


def test_corpus_has_restricted_chunks(db):
    """What: the corpus holds chunks that are not tier 'all'. Use when: CI of this suite. Inputs: table.
    Output: assertion. Side effects: none. Role: the filter tests below are meaningless without restricted rows."""
    rows = query_all(db, "SELECT count(*)::int AS n FROM chunks WHERE NOT ('all' = ANY(tiers))")
    assert rows[0]["n"] >= 3


@pytest.mark.parametrize("tier", ["public", *TIERS])
@pytest.mark.parametrize("query", ["how do refunds work", "set up SSO for my team", "business plan audit logs", "help"])
def test_no_leg_returns_a_chunk_outside_the_allowed_tiers(db, tier, query):
    """What: for every caller tier and several queries, each returned chunk's tiers intersect allowed_tiers, from
    both legs. Use when: CI of this suite. Inputs: tiers and queries. Output: assertion. Side effects: SQL.
    Role: the access filter on paid-tier content (review-pr15 Important 3; removing `c.tiers && ...` from either leg fails)."""
    resp = run(db, query=query, tier=tier, verified=tier != "public")
    allowed = set(resp.allowed_tiers)
    assert resp.chunks
    for ch in resp.chunks:
        assert set(ch.tiers) & allowed, (tier, query, ch.chunk_id, ch.tiers)
    vec = db.vector_leg(hashed(query), resp.allowed_tiers, 20)
    kw = db.keyword_leg(query, [], resp.allowed_tiers, hashed(query), 20)
    for r in [*vec, *kw]:
        assert set(r["tiers"]) & allowed, (tier, query, r["chunk_id"])


def test_public_never_sees_a_business_chunk_even_when_named(db):
    """What: a public caller that types a restricted chunk's id and title still gets none of its tier.
    Use when: CI of this suite. Inputs: one restricted chunk. Output: assertion. Side effects: SQL.
    Role: the query cannot widen the filter."""
    row = query_all(db, "SELECT chunk_id, article_slug FROM chunks WHERE NOT ('all' = ANY(tiers)) LIMIT 1")[0]
    resp = run(db, query=f"{row['chunk_id']} {row['article_slug']}", k=8)
    assert all(set(c.tiers) <= {"all"} for c in resp.chunks)


class ForcedIndexDb(Database):
    """What: a Database whose connections force the HNSW index and a tiny ef_search. Use when: the iterative-scan
    test. Inputs: url. Output: Database. Side effects: connection options. Role: with 74 rows the planner would
    scan exactly and hide a missing `hnsw.iterative_scan`; this makes the index the only path."""

    def _connect(self):  # pyright: ignore[reportIncompatibleMethodOverride]
        """What: connect with enable_seqscan off, bitmap scans off and hnsw.ef_search 10. Use when: any method.
        Inputs: none. Output: connection. Side effects: network. Role: test double."""
        import psycopg
        from psycopg.rows import dict_row
        return psycopg.connect(self.url, row_factory=dict_row, connect_timeout=1, options=(  # pyright: ignore[reportArgumentType]
            "-c statement_timeout=1000 -c enable_seqscan=off -c enable_bitmapscan=off -c hnsw.ef_search=10"))


def test_restricted_only_filter_returns_its_rows_through_the_hnsw_index(db):
    """What: a business-only filter on a query far from business chunks still returns every business-only chunk (up to
    20) when the index is the only path. Use when: CI of this suite. Inputs: forced-index Database. Output: assertion.
    Side effects: SQL. Role: `hnsw.iterative_scan = strict_order` (spike 13): off, the filtered scan returns 0 rows."""
    n = query_all(db, "SELECT count(*)::int AS n FROM chunks WHERE tiers && ARRAY['business']")[0]["n"]
    assert n >= 5
    rows = ForcedIndexDb(URL).vector_leg(hashed("refund policy money back"), ["business"], 20)
    assert len(rows) == min(n, 20)
    assert all("business" in r["tiers"] for r in rows)
    assert [r["v_rank"] for r in rows] == list(range(1, len(rows) + 1))
    assert all(a["cosine"] >= b["cosine"] for a, b in zip(rows, rows[1:]))


def test_every_known_error_code_surfaces_its_chunk_with_a_healthy_vector_leg(db):
    """What: for each of the corpus's error codes, requests (query 'help'; a login-trouble sentence; the code typed in
    the query; each with error_code where the code is not typed) put a chunk carrying the code in the top 5 and never
    list the code as unknown. Use when: CI of this suite. Inputs: the real vocabulary. Output: assertion. Side
    effects: SQL. Role: review-pr15 Important 1 (the reviewer's 12 codes by the cases that failed: 0 of 12, 1 of 12)."""
    codes = sorted(db.vocabulary())
    assert len(codes) >= 12
    cases = [("help", True), ("I get an error when I try to log in to my account", True), ("what does {c} mean", False)]
    for code in codes:
        for text, use_field in cases:
            resp = run(db, query=text.format(c=code), **({"error_code": code} if use_field else {}))
            assert any(code in ch.error_codes for ch in resp.chunks), (code, text, [c.chunk_id for c in resp.chunks])
            assert code in resp.query.codes and code not in resp.query.unknown_codes, (code, text)
            assert resp.confidence.code_match is True


def test_corpus_version_reader_follows_the_latest_finished_run(db):
    """What: status(), corpus() and corpus_version() return the version of the newest finished run even when it
    failed, ignoring running rows. Use when: CI of this suite. Inputs: three run rows. Output: assertion. Side
    effects: INSERTs run rows. Role: review-pr9-t6 Important 1, reader SQL against real Postgres."""
    store = PgStore(URL)
    live = corpus_version(store.corpus_pairs())
    store.start_run("11111111-1111-4111-8111-111111111111", "m")
    store.finish_run("11111111-1111-4111-8111-111111111111", "failed", "f" * 64, {}, "boom")
    store.start_run("22222222-2222-4222-8222-222222222222", "m")  # still running: no version, must be ignored
    fresh = Database(URL)
    assert (fresh.status() or {})["corpus_version"] == "f" * 64
    assert fresh.corpus()["corpus_version"] == "f" * 64
    assert fresh.corpus_version() == "f" * 64
    assert live != "f" * 64 and len(live) == 64
    assert vector_text([0.5]) == "[0.5]"
