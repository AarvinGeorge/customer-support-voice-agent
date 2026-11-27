"""What: tests for hybrid search: RRF fusion, the POST /v1/search endpoint with fake DB and embedder, budgets,
keyword-only fallback, tier handling. Use when: CI. Inputs: fakes (no Postgres, no network). Output: pass or fail.
Side effects: one test waits about 1.3 s for the embedding timeout. Role: CS-E-02.S2.T2 Done-when (hanging embedding
returns keyword-only results within 3.0 s; tier-filtered query returns k rows)."""
import asyncio
import time

import pytest
from contracts.errors import ErrorEnvelope
from contracts.knowledge_api import SearchResponse

from knowledge.embeddings import EmbeddingError
from knowledge.search import fuse
from tests.conftest import TOKEN

AUTH = {"Authorization": f"Bearer {TOKEN}"}


def row(cid, **kw):
    """What: a fake leg row. Use when: building fake DB output. Inputs: chunk id and overrides. Output: dict
    with every column search reads. Side effects: none. Role: test helper."""
    base = {"chunk_id": cid, "article_slug": cid.split("#")[0], "article_title": "Title " + cid, "section_path": ["S"],
            "heading": "S", "header": "Article: T > S\nApplies to: all\nUpdated: 2026-11-20", "body": "Body of " + cid,
            "tiers": ["all"], "error_codes": [], "policy_keys": [], "injection_suspect": False, "doc_version": 1,
            "last_updated": "2026-11-20", "content_hash": "h" * 64, "source_url": "https://app.notion.com/p/" + "a" * 32,
            "cosine": 0.5}
    base.update(kw)
    return base


# ---------- fusion ----------
def test_fuse_rrf_arithmetic_and_order():
    """What: RRF k=60 sums 1/(60+rank) over both legs; a chunk in both legs beats single-leg chunks; fused_rank
    counts from 1. Use when: CI. Inputs: two ranked legs. Output: assertion. Side effects: none. Role: doc 07 s4.1."""
    vec = [row("a#1", v_rank=1), row("b#1", v_rank=2), row("c#1", v_rank=3)]
    kw = [row("b#1", f_rank=1), row("d#1", f_rank=2)]
    out = fuse(vec, kw, k=5)
    assert [r["chunk_id"] for r in out] == ["b#1", "a#1", "d#1", "c#1"]
    by = {r["chunk_id"]: r for r in out}
    assert by["b#1"]["rrf_score"] == pytest.approx(1 / 62 + 1 / 61)
    assert by["a#1"]["rrf_score"] == pytest.approx(1 / 61) and by["d#1"]["rrf_score"] == pytest.approx(1 / 62)
    assert [r["fused_rank"] for r in out] == [1, 2, 3, 4]
    assert by["b#1"]["v_rank"] == 2 and by["b#1"]["f_rank"] == 1 and by["a#1"]["f_rank"] is None


def test_fuse_cuts_to_k_and_handles_empty_legs():
    """What: only k rows come back; empty inputs give []; a keyword-only hit keeps its row data.
    Use when: CI. Inputs: legs. Output: assertion. Side effects: none. Role: edge cases."""
    vec = [row(f"x#{i}", v_rank=i + 1) for i in range(8)]
    assert len(fuse(vec, [], k=5)) == 5
    assert fuse([], [], k=5) == []
    only_kw = fuse([], [row("k#1", f_rank=1)], k=3)
    assert only_kw[0]["chunk_id"] == "k#1" and only_kw[0]["v_rank"] is None


def test_fuse_ties_break_by_vector_rank_then_id():
    """What: equal RRF scores order by better vector rank, then chunk id. Use when: CI. Inputs: tied rows.
    Output: assertion. Side effects: none. Role: deterministic output."""
    out = fuse([row("z#1", v_rank=1)], [row("a#1", f_rank=1)], k=2)
    assert [r["chunk_id"] for r in out] == ["z#1", "a#1"]


# ---------- endpoint ----------
class FakeDb:
    """What: stand-in for the search side of knowledge.db.Database. Use when: endpoint tests. Inputs: legs to return.
    Output: canned rows; records calls. Side effects: none. Role: no Postgres needed."""

    def __init__(self, vec=None, kw=None, vocab=("E-SSO-401",), fail=False):
        """What: set canned data. Use when: construction. Inputs: leg rows, vocabulary, fail flag. Output: none.
        Side effects: none. Role: init."""
        self.vec, self.kw, self.vocab, self.fail, self.calls = vec or [], kw or [], set(vocab), fail, []

    def status(self):
        """What: readiness facts. Use when: readyz. Inputs: none. Output: dict. Side effects: none. Role: fake."""
        return {"chunks": 3, "corpus_version": "cv123"}

    def corpus_version(self):
        """What: the version the search response reports. Use when: search builds the response. Inputs: none.
        Output: str. Side effects: none. Role: fake."""
        return "cv123"

    def corpus(self):
        """What: empty corpus. Use when: corpus route. Inputs: none. Output: dict. Side effects: none. Role: fake."""
        return {"corpus_version": "cv123", "articles": [], "last_ingest_at": None}

    def vocabulary(self):
        """What: known codes. Use when: normalization. Inputs: none. Output: set. Side effects: none. Role: fake."""
        return self.vocab

    def vector_leg(self, vec, tiers, limit):
        """What: canned vector leg; records args. Use when: search. Inputs: vector, tiers, limit. Output: rows.
        Side effects: records. Role: fake (raises when fail)."""
        self.calls.append(("vec", tiers, limit))
        if self.fail:
            raise RuntimeError("db down")
        return self.vec

    def keyword_leg(self, text, codes, tiers, vec, limit):
        """What: canned keyword leg; records args. Use when: search. Inputs: text, codes, tiers, vector or None,
        limit. Output: rows. Side effects: records. Role: fake (raises when fail)."""
        self.calls.append(("kw", text, list(codes), tiers, vec is not None, limit))
        if self.fail:
            raise RuntimeError("db down")
        return self.kw


class FakeEmbedder:
    """What: scripted embedder. Use when: endpoint tests. Inputs: behaviour list ('ok', 'hang', 'fail').
    Output: vector or exception; counts calls. Side effects: sleeps when hanging. Role: stand-in for OpenAIEmbedder."""

    def __init__(self, *script):
        """What: store the script. Use when: construction. Inputs: behaviours. Output: none. Side effects: none.
        Role: init."""
        self.script, self.calls, self.inputs = list(script) or ["ok"], 0, 0

    async def aembed_one(self, text, timeout_s):
        """What: act out the next behaviour. Use when: search embeds the query. Inputs: text, timeout.
        Output: a vector. Side effects: counts, may sleep to the timeout. Role: fake."""
        mode = self.script[min(self.calls, len(self.script) - 1)]
        self.calls += 1
        if mode == "hang":
            await asyncio.sleep(timeout_s + 5)
        if mode == "fail":
            raise EmbeddingError("boom")
        return [0.1] * 1536


def setup(client, db, emb):
    """What: install fakes on the running app. Use when: every endpoint test. Inputs: client, db, embedder.
    Output: none. Side effects: patches app.state. Role: test helper."""
    client.app.state.db = db
    client.app.state.embedder = emb


def body(**kw):
    """What: a valid search request body. Use when: endpoint tests. Inputs: overrides. Output: dict.
    Side effects: none. Role: test helper."""
    b = {"query": "how do refunds work", "tier": "public", "verified": False, "k": 5}
    b.update(kw)
    return b


def test_search_happy_path_validates_against_contract(client):
    """What: a normal search returns a SearchResponse with citations carrying source_url, k rows, trace ids,
    corpus version. Use when: CI. Inputs: 6 vector rows, 2 keyword rows. Output: assertion. Side effects: none.
    Role: Done-when 'tier-filtered query returns k rows' and 'every chunk carries source_url'."""
    vec = [row(f"refund-policy#s{i}", v_rank=i + 1, cosine=0.6 - i / 20) for i in range(6)]
    kw = [row("refund-policy#s2", f_rank=1, cosine=0.5)]
    setup(client, FakeDb(vec, kw), FakeEmbedder())
    r = client.post("/v1/search", json=body(), headers=AUTH)
    assert r.status_code == 200, r.text
    resp = SearchResponse.model_validate(r.json())
    assert len(resp.chunks) == 5 and resp.chunks[0].chunk_id == "refund-policy#s2"
    assert all(c.source_url.startswith("https://app.notion.com/p/") for c in resp.chunks)
    assert resp.corpus_version == "cv123" and resp.embedding_model == "text-embedding-3-small"
    assert resp.chunks[0].scores.vector_rank == 3 and resp.chunks[0].scores.keyword_rank == 1
    assert resp.chunks[0].citation.ref and resp.trace_id == r.headers["X-Trace-Id"]
    assert resp.confidence.legs.vector == "ok" and resp.confidence.legs.keyword == "ok"
    assert resp.confidence.calibration_status == "none"


def test_tier_filter_is_computed_from_claims(client):
    """What: public sees ['all']; a pro caller sees ['all','pro']; both legs get the same list and limit 20.
    Use when: CI. Inputs: two requests. Output: assertion. Side effects: none. Role: doc 07 s4.1 allowed_tiers."""
    db = FakeDb([row("a#1", v_rank=1)], [])
    setup(client, db, FakeEmbedder())
    r = client.post("/v1/search", json=body(), headers=AUTH)
    assert r.json()["allowed_tiers"] == ["all"]
    r = client.post("/v1/search", json=body(tier="pro", verified=True), headers=AUTH)
    assert r.json()["allowed_tiers"] == ["all", "pro"]
    vec_call = [c for c in db.calls if c[0] == "vec"][-1]
    kw_call = [c for c in db.calls if c[0] == "kw"][-1]
    assert vec_call[1] == ["all", "pro"] and vec_call[2] == 20 and kw_call[3] == ["all", "pro"] and kw_call[-1] == 20


def test_invalid_tier_pairing_and_bad_k_are_422_envelopes(client):
    """What: verified=false with tier pro, k=9 and an empty query are 422 INVALID_REQUEST. Use when: CI.
    Inputs: three bodies. Output: assertion. Side effects: none. Role: doc 07 s4.1 validation."""
    setup(client, FakeDb(), FakeEmbedder())
    for b in (body(tier="pro"), body(k=9), body(query="")):
        r = client.post("/v1/search", json=b, headers=AUTH)
        assert r.status_code == 422 and ErrorEnvelope.model_validate(r.json()).error.code == "INVALID_REQUEST"


def test_search_needs_the_search_scope(client):
    """What: no token is 401 UNAUTHORIZED. Use when: CI. Inputs: request without auth. Output: assertion.
    Side effects: none. Role: scope check on the new route."""
    setup(client, FakeDb(), FakeEmbedder())
    r = client.post("/v1/search", json=body())
    assert r.status_code == 401 and r.json()["error"]["code"] == "UNAUTHORIZED"


def test_spoken_code_is_normalized_and_sent_to_keyword_leg(client):
    """What: 'E S S O four oh one' becomes E-SSO-401 in the response query and in the keyword leg's code list;
    the chunk carrying the code makes code_match true. Use when: CI. Inputs: spoken query. Output: assertion.
    Side effects: none. Role: error-code path of doc 07 s4.1."""
    hit = row("sso#1", f_rank=1, error_codes=["E-SSO-401"], cosine=0.2)
    db = FakeDb([], [hit])
    setup(client, db, FakeEmbedder())
    r = client.post("/v1/search", json=body(query="what does E S S O four oh one mean"), headers=AUTH)
    j = r.json()
    assert j["query"]["codes"] == ["E-SSO-401"] and "E-SSO-401" in j["query"]["normalized"]
    kw_call = next(c for c in db.calls if c[0] == "kw")
    assert kw_call[2] == ["E-SSO-401"] and j["confidence"]["code_match"] is True
    assert j["chunks"][0]["relevant"] is True and j["query"]["unknown_codes"] == []


def test_unknown_code_and_error_code_param(client):
    """What: a code-shaped string outside the vocabulary lands in unknown_codes; the error_code field is merged
    like a typed code. Use when: CI. Inputs: two requests. Output: assertion. Side effects: none. Role: s4.1 text."""
    setup(client, FakeDb([row("a#1", v_rank=1)], []), FakeEmbedder())
    j = client.post("/v1/search", json=body(query="what is E-SSO-999"), headers=AUTH).json()
    assert j["query"]["unknown_codes"] == ["E-SSO-999"] and j["query"]["codes"] == []
    j = client.post("/v1/search", json=body(query="login fails", error_code="E-SSO-401"), headers=AUTH).json()
    assert j["query"]["codes"] == ["E-SSO-401"]
    assert j["query"]["unknown_codes"] == []  # in the vocabulary, so never unknown even when no returned chunk carries it


def test_hanging_embedding_returns_keyword_only_within_3s(client):
    """What: an embedding call that never returns still gets keyword-only results in under 3.0 s with
    legs.vector failed. Use when: CI. Inputs: hanging fake. Output: assertion on status, legs and elapsed.
    Side effects: waits about 1.3 s. Role: Done-when 'hanging embedding stub returns keyword-only within 3.0 s'."""
    emb = FakeEmbedder("hang")
    db = FakeDb([row("v#1", v_rank=1)], [row("k#1", f_rank=1)])
    setup(client, db, emb)
    t = time.monotonic()
    r = client.post("/v1/search", json=body(), headers=AUTH)
    elapsed = time.monotonic() - t
    j = r.json()
    assert r.status_code == 200 and elapsed < 3.0, elapsed
    assert j["confidence"]["legs"]["vector"] == "failed" and j["confidence"]["verdict"] == "error"
    assert j["confidence"]["reason"] == "embedding_unavailable"
    assert [c["chunk_id"] for c in j["chunks"]] == ["k#1"] and j["chunks"][0]["scores"]["cosine"] == 0.0
    assert emb.calls == 1  # a timeout leaves no budget for a retry
    assert all(c[0] != "vec" for c in db.calls) and [c for c in db.calls if c[0] == "kw"][-1][4] is False


def test_fast_embedding_failure_is_retried_once(client):
    """What: a quick failure with at least 2.5 s of budget left is retried once and then succeeds.
    Use when: CI. Inputs: fail then ok. Output: assertion. Side effects: none. Role: doc 07 s4.1 retry rule."""
    emb = FakeEmbedder("fail", "ok")
    setup(client, FakeDb([row("v#1", v_rank=1)], []), emb)
    j = client.post("/v1/search", json=body(), headers=AUTH).json()
    assert emb.calls == 2 and j["confidence"]["legs"]["vector"] == "ok"
    emb = FakeEmbedder("fail", "fail", "ok")
    setup(client, FakeDb([row("v#1", v_rank=1)], []), emb)
    j = client.post("/v1/search", json=body(), headers=AUTH).json()
    assert emb.calls == 2 and j["confidence"]["legs"]["vector"] == "failed"


def test_missing_embedder_degrades_like_an_outage(client):
    """What: no OpenAI key (embedder None) gives the keyword-only path, not a 500. Use when: CI.
    Inputs: embedder None. Output: assertion. Side effects: none. Role: failure containment."""
    setup(client, FakeDb([], [row("k#1", f_rank=1)]), None)
    j = client.post("/v1/search", json=body(), headers=AUTH).json()
    assert j["confidence"]["legs"]["vector"] == "failed" and len(j["chunks"]) == 1


def test_both_legs_failing_is_503_retrieval_unavailable(client):
    """What: a database failure in both legs is a 503 envelope, retryable. Use when: CI. Inputs: failing db.
    Output: assertion. Side effects: none. Role: doc 07 s4.1 errors."""
    setup(client, FakeDb(fail=True), FakeEmbedder())
    r = client.post("/v1/search", json=body(), headers=AUTH)
    err = ErrorEnvelope.model_validate(r.json()).error
    assert r.status_code == 503 and err.code == "RETRIEVAL_UNAVAILABLE" and err.retryable


def test_empty_results_are_a_normal_200(client):
    """What: no rows from either leg is 200 with chunks [] and legs empty. Use when: CI. Inputs: empty fakes.
    Output: assertion. Side effects: none. Role: draft A s4 'both legs empty'."""
    setup(client, FakeDb([], []), FakeEmbedder())
    j = client.post("/v1/search", json=body(), headers=AUTH).json()
    assert j["chunks"] == [] and j["confidence"]["legs"] == {"vector": "empty", "keyword": "empty"}
