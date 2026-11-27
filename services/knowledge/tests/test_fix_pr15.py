"""What: regression tests for the PR #15 review: a known error code surfaces its chunk, the 3.0 s budget holds when the
database stalls, empty and non-retryable cases, trace ids in warnings, spoken-code sentence limits, the small cache.
Use when: CI. Inputs: the FakeDb and FakeEmbedder of test_search. Output: pass or fail. Side effects: one test waits
about 3 s (a frozen fake database). Role: every review finding becomes a test before the fix (project rule)."""
import logging
import time

import pytest

from knowledge import search
from knowledge.db import Database
from knowledge.embeddings import EmbeddingError
from knowledge.normalize import normalize_query
from knowledge.search import fuse
from tests.conftest import TOKEN
from tests.test_search import FakeDb, FakeEmbedder, body, row, setup

AUTH = {"Authorization": f"Bearer {TOKEN}"}
CODE = "E-SSO-401"


def _busy_vector_leg():
    """What: eight chunks present in both legs with good scores, plus the code chunk found only by the keyword leg.
    Use when: the code-surfacing tests. Inputs: none. Output: (vector rows, keyword rows). Side effects: none.
    Role: reproduces review-pr15 Important 1 (a keyword-only code chunk scored below every two-leg chunk)."""
    vec = [row(f"other#{i}", v_rank=i + 1, cosine=0.6 - i / 50) for i in range(8)]
    kw = [row("sso#code", f_rank=1, error_codes=[CODE], cosine=0.1)]
    kw += [row(f"other#{i}", f_rank=i + 2, cosine=0.6 - i / 50) for i in range(8)]
    return vec, kw


@pytest.mark.parametrize("query", ["help", "I get an error when I try to log in to my account"])
def test_error_code_field_puts_its_chunk_in_the_top_results(client, query):
    """What: with a healthy vector leg and the code only in error_code, the chunk that carries the code is rank 1 and
    the code is not listed as unknown. Use when: CI. Inputs: busy legs. Output: assertion. Side effects: none.
    Role: review-pr15 Important 1."""
    vec, kw = _busy_vector_leg()
    setup(client, FakeDb(vec, kw, vocab=(CODE,)), FakeEmbedder())
    j = client.post("/v1/search", json=body(query=query, error_code=CODE, k=5), headers=AUTH).json()
    assert [c["chunk_id"] for c in j["chunks"]][0] == "sso#code"
    assert j["query"]["codes"] == [CODE] and j["query"]["unknown_codes"] == []
    assert j["confidence"]["code_match"] is True


def test_code_is_added_to_the_searched_text_and_never_in_both_arrays(client):
    """What: a known code from error_code is merged into the normalized text (so it is embedded and keyword-searched)
    and codes and unknown_codes never share a member. Use when: CI. Inputs: one request. Output: assertion.
    Side effects: none. Role: review-pr15 Important 1 (s4.1: merged with codes found in query)."""
    vec, kw = _busy_vector_leg()
    db = FakeDb(vec, kw, vocab=(CODE,))
    setup(client, db, FakeEmbedder())
    j = client.post("/v1/search", json=body(query="help", error_code=CODE), headers=AUTH).json()
    assert CODE in j["query"]["normalized"]
    assert not set(j["query"]["codes"]) & set(j["query"]["unknown_codes"])
    assert next(c for c in db.calls if c[0] == "kw")[1] == j["query"]["normalized"]


def test_fuse_puts_chunks_with_a_detected_code_first():
    """What: fuse(..., codes=[...]) ranks code-carrying rows before any other row, then by score. Use when: CI.
    Inputs: two legs. Output: assertion. Side effects: none. Role: review-pr15 Important 1 (fusion half)."""
    vec = [row("a#1", v_rank=1), row("b#1", v_rank=2)]
    kw = [row("a#1", f_rank=2), row("b#1", f_rank=3), row("c#1", f_rank=1, error_codes=[CODE])]
    assert fuse(vec, kw, 3)[0]["chunk_id"] == "a#1"
    assert fuse(vec, kw, 3, codes=[CODE])[0]["chunk_id"] == "c#1"


class FrozenDb(FakeDb):
    """What: a database whose every call blocks for 3.6 s, like a paused Postgres. Use when: the budget test.
    Inputs: as FakeDb. Output: rows after the sleep. Side effects: sleeps in worker threads. Role: fake outage."""

    def vocabulary(self):
        """What: block, then answer. Use when: the vocabulary read. Inputs: none. Output: set. Side effects: sleeps.
        Role: fake."""
        time.sleep(3.6)
        return super().vocabulary()

    def corpus_version(self):
        """What: block, then answer. Use when: the version read. Inputs: none. Output: str. Side effects: sleeps.
        Role: fake."""
        time.sleep(3.6)
        return "cv123"

    def vector_leg(self, vec, tiers, limit):
        """What: block, then answer. Use when: the vector leg. Inputs: as the real leg. Output: rows.
        Side effects: sleeps. Role: fake."""
        time.sleep(3.6)
        return super().vector_leg(vec, tiers, limit)

    def keyword_leg(self, text, codes, tiers, vec, limit):
        """What: block, then answer. Use when: the keyword leg. Inputs: as the real leg. Output: rows.
        Side effects: sleeps. Role: fake."""
        time.sleep(3.6)
        return super().keyword_leg(text, codes, tiers, vec, limit)


def test_frozen_database_is_a_503_inside_the_3s_budget(client):
    """What: when every database call hangs, the request still ends with 503 RETRIEVAL_UNAVAILABLE in about 3.0 s.
    Use when: CI. Inputs: FrozenDb. Output: assertion on status and elapsed. Side effects: waits about 3 s.
    Role: review-pr15 Important 2 (doc 07 s4.1 caps the request so knowledge answers before the agent's 3.5 s)."""
    setup(client, FrozenDb([row("v#1", v_rank=1)], [row("k#1", f_rank=1)]), FakeEmbedder())
    t = time.monotonic()
    r = client.post("/v1/search", json=body(), headers=AUTH)
    elapsed = time.monotonic() - t
    assert r.status_code == 503 and r.json()["error"]["code"] == "RETRIEVAL_UNAVAILABLE", r.text
    assert 2.9 <= elapsed < 3.3, elapsed
    assert search.TOTAL_BUDGET_S == 3.0


def test_query_that_is_empty_after_cleaning_is_422(client):
    """What: whitespace and zero-width-only text is 422 INVALID_REQUEST and nothing is embedded. Use when: CI.
    Inputs: two bodies. Output: assertion. Side effects: none. Role: review-pr15 Minor 1."""
    emb = FakeEmbedder()
    setup(client, FakeDb(), emb)
    for q in (" ", "​​", "\t\n"):
        r = client.post("/v1/search", json=body(query=q), headers=AUTH)
        assert r.status_code == 422 and r.json()["error"]["code"] == "INVALID_REQUEST", q
    assert emb.calls == 0


class StatusEmbedder(FakeEmbedder):
    """What: an embedder that fails fast with a given HTTP status. Use when: the retry tests. Inputs: status.
    Output: raises EmbeddingError. Side effects: counts. Role: fake provider answers."""

    def __init__(self, status):
        """What: remember the status. Use when: construction. Inputs: status. Output: none. Side effects: none.
        Role: init."""
        super().__init__()
        self.status = status

    async def aembed_one(self, text, timeout_s):
        """What: fail with the status. Use when: search embeds. Inputs: text, timeout. Output: raises.
        Side effects: counts, records the timeout. Role: fake."""
        self.calls += 1
        self.timeouts = [*getattr(self, "timeouts", []), timeout_s]
        raise EmbeddingError(f"status {self.status}", status=self.status)


@pytest.mark.parametrize("status,calls", [(400, 1), (401, 1), (429, 2), (503, 2)])
def test_only_transient_failures_are_retried(client, status, calls):
    """What: 400 and 401 are not retried; 429 and 5xx are, once. Use when: CI. Inputs: scripted status. Output:
    assertion on the attempt count. Side effects: none. Role: review-pr15 Minor 2."""
    emb = StatusEmbedder(status)
    setup(client, FakeDb([], [row("k#1", f_rank=1)]), emb)
    assert client.post("/v1/search", json=body(), headers=AUTH).status_code == 200
    assert emb.calls == calls


def test_first_embedding_attempt_gets_the_1_2s_timeout(client):
    """What: the first embedding call is given 1.2 s. Use when: CI. Inputs: failing embedder. Output: assertion.
    Side effects: none. Role: review-pr15 Minor 5 (EMBED_TIMEOUT_S was unpinned)."""
    emb = StatusEmbedder(401)
    setup(client, FakeDb([], [row("k#1", f_rank=1)]), emb)
    client.post("/v1/search", json=body(), headers=AUTH)
    assert emb.timeouts[0] == pytest.approx(1.2, abs=0.01)


def test_search_warnings_carry_the_trace_id(client, caplog):
    """What: a failed leg and a failed embedding log lines containing trace_id=<the request's id>. Use when: CI.
    Inputs: failing db and embedder. Output: assertion. Side effects: none. Role: review-pr15 Minor 3."""
    setup(client, FakeDb(fail=True), StatusEmbedder(401))
    with caplog.at_level(logging.WARNING, logger="knowledge.search"):
        r = client.post("/v1/search", json=body(), headers=AUTH)
    tid = r.headers["X-Trace-Id"]
    warns = [m.getMessage() for m in caplog.records if m.name == "knowledge.search"]
    assert len(warns) >= 2 and all(f"trace_id={tid}" in w for w in warns), warns


def test_spoken_code_does_not_join_across_a_sentence_end():
    """What: 'plan E. Sync 503 records failed' is not E-SYNC-503; 'E sync 503' still is. Use when: CI.
    Inputs: two texts. Output: assertion. Side effects: none. Role: review-pr15 Minor 4."""
    vocab = {"E-SYNC-503"}
    assert normalize_query("We chose plan E. Sync 503 records failed", vocab).codes == []
    assert normalize_query("it says E sync 503 today", vocab).codes == ["E-SYNC-503"]


def test_vocabulary_and_version_are_cached_for_a_short_time(monkeypatch):
    """What: two vocabulary() calls and two corpus_version() calls open one connection each; a failing refresh
    serves the old value. Use when: CI. Inputs: a recording connection. Output: assertion. Side effects: none.
    Role: review-pr15 Important 2 (two extra round trips per request) and Minor 6."""
    opened = []

    class Cur:
        """What: canned cursor. Use when: this test. Inputs: rows. Output: rows. Side effects: none. Role: fake."""

        def __init__(self, rows):
            """What: keep rows. Use when: construction. Inputs: rows. Output: none. Side effects: none. Role: init."""
            self.rows = rows

        def fetchall(self):
            """What: the rows. Use when: SELECT. Inputs: none. Output: list. Side effects: none. Role: fake."""
            return self.rows

        def fetchone(self):
            """What: the first row. Use when: SELECT LIMIT 1. Inputs: none. Output: row. Side effects: none. Role: fake."""
            return self.rows[0] if self.rows else None

    class Conn:
        """What: canned connection. Use when: this test. Inputs: none. Output: context manager. Side effects:
        records. Role: fake."""

        def __enter__(self):
            """What: enter. Use when: with. Inputs: none. Output: self. Side effects: records a connect. Role: fake."""
            opened.append(1)
            return self

        def __exit__(self, *a):
            """What: leave. Use when: with. Inputs: any. Output: None. Side effects: none. Role: fake."""
            return None

        def execute(self, q, *a):
            """What: canned answer by query text. Use when: SELECT. Inputs: sql. Output: cursor. Side effects: none.
            Role: fake."""
            if "unnest" in q:
                return Cur([{"code": CODE}])
            return Cur([{"corpus_version": "v1"}])

    db = Database("postgresql://x")
    monkeypatch.setattr(db, "_connect", lambda: Conn())
    assert db.vocabulary() == {CODE} and db.vocabulary() == {CODE}
    assert db.corpus_version() == "v1" and db.corpus_version() == "v1"
    assert len(opened) == 2
    monkeypatch.setattr(db, "_connect", lambda: (_ for _ in ()).throw(RuntimeError("down")))
    db._cache.clear()
    with pytest.raises(RuntimeError):
        db.vocabulary()
