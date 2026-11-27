"""What: tests for the ingest job with an in-memory store and a fake embedder (no Postgres, no network).
Use when: CI. Inputs: temp article directories and the real policy. Output: pass or fail. Side effects: temp files.
Role: CS-E-02.S1.T6 Done-when: second run makes zero embedding calls; every chunk carries source_url; loud failures;
per-article swap; corpus_version; vocabulary."""
import hashlib
from pathlib import Path

import pytest

from knowledge.ingest.errors import IngestError
from knowledge.ingest.job import main, run_ingest

ROOT = Path(__file__).resolve().parents[3]
POLICY = ROOT / "data" / "policy.yaml"
REAL = ROOT / "data" / "articles"


class FakeEmbedder:
    """What: counts calls and returns deterministic 1536-float vectors. Use when: every test.
    Inputs: none. Output: vectors. Side effects: none. Role: stand-in for OpenAIEmbedder."""

    def __init__(self, dims=1536):
        """What: set counters. Use when: construction. Inputs: dims. Output: none. Side effects: none. Role: init."""
        self.calls = 0
        self.inputs = 0
        self.dims = dims

    def embed_batch(self, texts):
        """What: fake batch call. Use when: ingest embeds. Inputs: texts. Output: vectors. Side effects: counts.
        Role: fake."""
        if texts:
            self.calls += 1
            self.inputs += len(texts)
        return [[(int(hashlib.sha256(t.encode()).hexdigest()[:4], 16) % 100) / 100] * self.dims for t in texts]


class FakeStore:
    """What: in-memory Store with the same interface as PgStore. Use when: unit tests. Inputs: none.
    Output: recorded rows. Side effects: none. Role: lets the job logic run without Postgres."""

    def __init__(self):
        """What: empty tables. Use when: construction. Inputs: none. Output: none. Side effects: none. Role: init."""
        self.articles, self.chunks, self.runs, self.swaps = {}, {}, {}, []
        self.fail_swap_for: str | None = None

    def start_run(self, run_id, model):
        """What: record a running row. Use when: job start. Inputs: id, model. Output: none. Side effects: state.
        Role: fake."""
        self.runs[run_id] = {"status": "running", "model": model}

    def finish_run(self, run_id, status, corpus_version, counts, error):
        """What: record the final row. Use when: job end. Inputs: id, status, version, counts, error.
        Output: none. Side effects: state. Role: fake."""
        self.runs[run_id].update(status=status, corpus_version=corpus_version, counts=counts, error=error)

    def existing_article(self, slug):
        """What: stored hash and model or None. Use when: skip check. Inputs: slug. Output: tuple or None.
        Side effects: none. Role: fake."""
        a = self.articles.get(slug)
        return (a["rendered_hash"], a["embedding_model"]) if a else None

    def existing_vectors(self, hashes, model):
        """What: vectors already stored for hashes. Use when: reuse. Inputs: hashes, model.
        Output: dict. Side effects: none. Role: fake."""
        return {c["content_hash"]: c["embedding"] for c in self.chunks.values()
                if c["content_hash"] in set(hashes) and c["embedding_model"] == model}

    def swap_article(self, article, chunk_rows):
        """What: replace an article and its chunks atomically. Use when: per-article swap. Inputs: row, rows.
        Output: chunks deleted. Side effects: state. Role: fake (raises for fail_swap_for to test rollback)."""
        if article["article_slug"] == self.fail_swap_for:
            raise RuntimeError("boom")
        old = {k for k, c in self.chunks.items() if c["article_slug"] == article["article_slug"]}
        new = {r["chunk_id"] for r in chunk_rows}
        for k in old - new:
            del self.chunks[k]
        for r in chunk_rows:
            self.chunks[r["chunk_id"]] = r
        self.articles[article["article_slug"]] = article
        self.swaps.append(article["article_slug"])
        return len(old - new)

    def delete_missing_articles(self, keep):
        """What: remove articles not in keep. Use when: end of run. Inputs: set of slugs.
        Output: chunks deleted. Side effects: state. Role: fake."""
        gone = set(self.articles) - set(keep)
        n = 0
        for s in gone:
            del self.articles[s]
            for k in [k for k, c in self.chunks.items() if c["article_slug"] == s]:
                del self.chunks[k]
                n += 1
        return n

    def corpus_pairs(self):
        """What: (chunk_id, content_hash) for every chunk. Use when: corpus_version. Inputs: none.
        Output: list. Side effects: none. Role: fake."""
        return [(k, c["content_hash"]) for k, c in self.chunks.items()]


def go(corpus, store, emb):
    """What: run the job. Use when: tests. Inputs: dir, store, embedder. Output: RunSummary. Side effects: state.
    Role: test helper."""
    return run_ingest(corpus, POLICY, store, emb)


def test_first_run_embeds_everything_in_one_batch_and_every_chunk_has_source_url(corpus):
    """What: first run writes all chunks with one embedding call and every chunk row carries source_url.
    Use when: CI. Inputs: 3 articles. Output: assertion. Side effects: none. Role: Done-when item 2."""
    store, emb = FakeStore(), FakeEmbedder()
    s = go(corpus, store, emb)
    assert s.articles_seen == 3 and s.articles_changed == 3 and s.articles_skipped == 0
    assert emb.calls == 1 and emb.inputs == s.chunks_embedded == len(store.chunks) > 10
    assert all(c["source_url"].startswith("https://app.notion.com/p/") for c in store.chunks.values())
    assert all(len(c["embedding"]) == 1536 for c in store.chunks.values())
    assert store.runs[s.run_id]["status"] == "ok"


def test_second_run_makes_zero_embedding_calls_and_no_swaps(corpus):
    """What: a re-run of unchanged files embeds nothing and rewrites nothing. Use when: CI.
    Inputs: two runs. Output: assertion on the counter. Side effects: none. Role: Done-when item 1."""
    store, emb = FakeStore(), FakeEmbedder()
    first = go(corpus, store, emb)
    calls_after_first, swaps = emb.calls, len(store.swaps)
    second = go(corpus, store, emb)
    assert emb.calls == calls_after_first and second.embedding_calls == 0 and second.chunks_embedded == 0
    assert second.articles_skipped == 3 and second.articles_changed == 0 and len(store.swaps) == swaps
    assert second.corpus_version == first.corpus_version


def test_changed_article_reembeds_only_changed_chunks(corpus):
    """What: editing one sentence re-embeds only the chunk that holds it; the others reuse vectors.
    Use when: CI. Inputs: edited file. Output: assertion. Side effects: edits temp file. Role: draft A step 5."""
    store, emb = FakeStore(), FakeEmbedder()
    first = go(corpus, store, emb)
    p = corpus / "refund-policy.md"
    p.write_text(p.read_text().replace("within 14 days of the charge", "within 14 days of the payment", 1))
    inputs_before = emb.inputs
    second = go(corpus, store, emb)
    assert second.articles_changed == 1 and second.articles_skipped == 2
    assert 1 <= emb.inputs - inputs_before <= 2 and second.corpus_version != first.corpus_version


def test_metadata_change_rewrites_rows_without_embedding(corpus):
    """What: changing source_url (the owner publishes the pages) updates the rows with zero embedding calls.
    Use when: CI. Inputs: edited front matter. Output: assertion. Side effects: edits temp file.
    Role: 'source_url on chunks' stays true after the URL swap."""
    store, emb = FakeStore(), FakeEmbedder()
    go(corpus, store, emb)
    p = corpus / "refund-policy.md"
    p.write_text(p.read_text().replace("https://app.notion.com/p/", "https://quillstack.notion.site/", 1))
    before = emb.inputs
    s = go(corpus, store, emb)
    assert s.articles_changed == 1 and emb.inputs == before
    assert any(c["source_url"].startswith("https://quillstack.notion.site/") for c in store.chunks.values())


def test_removed_article_is_deleted(corpus):
    """What: deleting a file removes the article and its chunks on the next run. Use when: CI.
    Inputs: removed file. Output: assertion. Side effects: deletes temp file. Role: draft A step 6."""
    store, emb = FakeStore(), FakeEmbedder()
    go(corpus, store, emb)
    (corpus / "sso-login-troubleshooting.md").unlink()
    s = go(corpus, store, emb)
    assert "sso-login-troubleshooting" not in store.articles and s.chunks_deleted > 0
    assert not any(c["article_slug"] == "sso-login-troubleshooting" for c in store.chunks.values())


def test_corpus_version_is_sha256_of_sorted_pairs(corpus):
    """What: corpus_version equals sha256 of the sorted 'chunk_id:content_hash' lines. Use when: CI.
    Inputs: one run. Output: assertion. Side effects: none. Role: draft A step 7."""
    store, emb = FakeStore(), FakeEmbedder()
    s = go(corpus, store, emb)
    want = hashlib.sha256("\n".join(sorted(f"{k}:{h}" for k, h in store.corpus_pairs())).encode()).hexdigest()
    assert s.corpus_version == want and store.runs[s.run_id]["corpus_version"] == want


def test_error_code_vocabulary(corpus):
    """What: the summary lists the distinct error codes found in the corpus. Use when: CI. Inputs: 3 articles.
    Output: assertion. Side effects: none. Role: vocabulary for query normalization."""
    s = go(corpus, FakeStore(), FakeEmbedder())
    assert "E-SSO-401" in s.error_codes and s.error_codes == sorted(set(s.error_codes))


def test_bad_article_fails_before_any_write(corpus):
    """What: an unknown placeholder in one article aborts the run with the key in the message and writes
    nothing; the run row is 'failed'. Use when: CI. Inputs: edited file. Output: assertion. Side effects: temp
    file. Role: loud-fail check, nothing half-ingested."""
    store, emb = FakeStore(), FakeEmbedder()
    p = corpus / "refund-policy.md"
    p.write_text(p.read_text().replace("## Free plan", "## Free plan\nSee {policy.nope}.", 1))
    with pytest.raises(IngestError, match="nope"):
        go(corpus, store, emb)
    assert not store.chunks and not store.articles and emb.calls == 0
    assert [r["status"] for r in store.runs.values()] == ["failed"]


def test_wrong_vector_length_aborts(corpus):
    """What: an embedder returning 1024 floats aborts with a length error and writes nothing. Use when: CI.
    Inputs: FakeEmbedder(dims=1024). Output: assertion. Side effects: none. Role: draft A step 8."""
    store = FakeStore()
    with pytest.raises(IngestError, match="1024"):
        go(corpus, store, FakeEmbedder(dims=1024))
    assert not store.chunks


def test_empty_directory_and_duplicate_ids_fail(tmp_path, corpus):
    """What: no articles, or two files with one article_id, fails. Use when: CI. Inputs: empty dir and a copy
    renamed. Output: assertion. Side effects: temp files. Role: zero-chunks and uniqueness checks."""
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(IngestError, match="no articles"):
        go(empty, FakeStore(), FakeEmbedder())
    (corpus / "copy.md").write_text((corpus / "refund-policy.md").read_text())
    with pytest.raises(IngestError, match="article_id"):
        go(corpus, FakeStore(), FakeEmbedder())


def test_failed_swap_keeps_old_version_and_marks_run_failed(corpus):
    """What: a database failure on one article rolls that article back (old rows stay), the run is 'failed'
    and the error propagates. Use when: CI. Inputs: store that raises on one slug. Output: assertion.
    Side effects: edits temp file. Role: per-article swap guarantee."""
    store, emb = FakeStore(), FakeEmbedder()
    go(corpus, store, emb)
    old = dict(store.chunks)
    p = corpus / "refund-policy.md"
    p.write_text(p.read_text().replace("within 14 days of the charge", "within 14 days of the payment", 1))
    store.fail_swap_for = "refund-policy"
    with pytest.raises(RuntimeError):
        go(corpus, store, emb)
    assert {k: v["content_hash"] for k, v in store.chunks.items() if v["article_slug"] == "refund-policy"} == \
           {k: v["content_hash"] for k, v in old.items() if v["article_slug"] == "refund-policy"}
    assert list(store.runs.values())[-1]["status"] == "failed"


def test_cli_exit_codes(corpus, monkeypatch, capsys):
    """What: main returns 1 and prints the error for an IngestError, 0 and a summary line otherwise.
    Use when: CI. Inputs: argv with a bad then good directory and injected fakes. Output: assertion.
    Side effects: patches. Role: 'non-zero exit' loud failure."""
    import knowledge.ingest.job as job
    monkeypatch.setattr(job, "_connect_store", lambda: FakeStore())
    monkeypatch.setattr(job, "_make_embedder", lambda: FakeEmbedder())
    p = corpus / "refund-policy.md"
    p.write_text(p.read_text().replace("## Free plan", "## Free plan\nSee {policy.nope}.", 1))
    assert main(["--articles", str(corpus), "--policy", str(POLICY)]) == 1
    assert "nope" in capsys.readouterr().err
    p.write_text((REAL / "refund-policy.md").read_text())
    assert main(["--articles", str(corpus), "--policy", str(POLICY)]) == 0
    out = capsys.readouterr().out
    assert "INGEST ok" in out and "embedding_calls=1" in out and "corpus_version=" in out
