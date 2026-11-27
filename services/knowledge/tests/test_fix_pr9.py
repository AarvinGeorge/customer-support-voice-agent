"""What: regression tests for the PR #9 re-review (review-pr9-t6): corpus_version after a partly failed run, date
validation before any write, fingerprint covers source_url, tier-like comments fail closed, run bookkeeping.
Use when: CI. Inputs: temp article copies, FakeStore, a recording fake connection. Output: pass or fail.
Side effects: temp files. Role: every review finding becomes a test before the fix (project rule)."""
from dataclasses import replace

import pytest

from knowledge.db import Database
from knowledge.ingest.chunker import chunk_article
from knowledge.ingest.errors import IngestError
from knowledge.ingest.job import corpus_version, fingerprint, run_ingest
from knowledge.ingest.parse import parse_article
from tests.test_ingest_chunker import art, words
from tests.test_ingest_job import POLICY, REAL, FakeEmbedder, FakeStore
from tests.test_ingest_review_fixes import POLICY as LOADED_POLICY


def _edit_first_and_break_last(corpus_dir):
    """What: change the first article (sorted) and make the last one fail in the store. Use when: the partial-failure
    test. Inputs: corpus dir. Output: (first slug, last slug). Side effects: rewrites one file. Role: test helper."""
    names = sorted(p.stem for p in corpus_dir.glob("*.md"))
    first, last = names[0], names[-1]
    p = corpus_dir / f"{first}.md"
    p.write_text(p.read_text() + "\n\nA new sentence added after the first run.\n")
    return first, last


def test_failed_run_records_the_version_actually_stored(corpus):
    """What: run 1 ok; run 2 changes article A (committed) and fails on article C. The failed run row must carry the
    hash of the chunks now stored, not the old one. Use when: CI. Inputs: three articles. Output: assertion.
    Side effects: none. Role: review-pr9-t6 Important 1."""
    store, emb = FakeStore(), FakeEmbedder()
    first = run_ingest(corpus, POLICY, store, emb)
    a, c = _edit_first_and_break_last(corpus)
    # make C change too so that it is swapped (and fails) in the same run
    pc = corpus / f"{c}.md"
    pc.write_text(pc.read_text() + "\n\nAnother new sentence for the last article.\n")
    store.fail_swap_for = c
    with pytest.raises(RuntimeError):
        run_ingest(corpus, POLICY, store, emb)
    failed = [r for r in store.runs.values() if r["status"] == "failed"]
    assert len(failed) == 1
    live = corpus_version(store.corpus_pairs())
    assert live != first.corpus_version  # A's new text is in the store
    assert failed[0]["corpus_version"] == live


def test_failed_run_with_unreadable_store_keeps_the_original_error(corpus):
    """What: when the store cannot answer in the except path (corpus_pairs and finish_run raise), the original
    exception still reaches the caller. Use when: CI. Inputs: a store that fails everywhere after start.
    Output: assertion. Side effects: none. Role: review-pr9-t6 Minor 7."""
    class Broken(FakeStore):
        """What: a store whose reads and run close fail. Use when: this test. Inputs: none. Output: store.
        Side effects: none. Role: fake database outage."""

        def existing_article(self, slug):
            """What: fail like a dead database. Use when: the skip check. Inputs: slug. Output: raises.
            Side effects: none. Role: fake."""
            raise RuntimeError("db down")

        def corpus_pairs(self):
            """What: fail. Use when: the except path. Inputs: none. Output: raises. Side effects: none. Role: fake."""
            raise RuntimeError("still down")

        def finish_run(self, *a, **k):
            """What: fail. Use when: the except path. Inputs: any. Output: raises. Side effects: none. Role: fake."""
            raise RuntimeError("cannot close run")
    with pytest.raises(RuntimeError, match="db down"):
        run_ingest(corpus, POLICY, Broken(), FakeEmbedder())


class _Cur:
    """What: a cursor returning canned rows. Use when: Database tests. Inputs: row. Output: fetchone/fetchall.
    Side effects: none. Role: fake."""

    def __init__(self, row):
        """What: keep the row. Use when: construction. Inputs: row. Output: none. Side effects: none. Role: init."""
        self.row = row

    def fetchone(self):
        """What: the canned row. Use when: SELECT ... LIMIT 1. Inputs: none. Output: row. Side effects: none.
        Role: fake."""
        return self.row

    def fetchall(self):
        """What: no rows. Use when: article list. Inputs: none. Output: []. Side effects: none. Role: fake."""
        return []


class _Conn:
    """What: a connection recording every SQL text. Use when: Database tests. Inputs: none. Output: context manager.
    Side effects: appends to sql. Role: fake."""

    def __init__(self, sql):
        """What: share the sql list. Use when: construction. Inputs: list. Output: none. Side effects: none. Role: init."""
        self.sql = sql

    def __enter__(self):
        """What: enter. Use when: with. Inputs: none. Output: self. Side effects: none. Role: fake."""
        return self

    def __exit__(self, *a):
        """What: leave. Use when: with. Inputs: any. Output: None. Side effects: none. Role: fake."""
        return None

    def execute(self, q, *a):
        """What: record the query and return a canned cursor. Use when: SELECT. Inputs: sql. Output: cursor.
        Side effects: records. Role: fake."""
        self.sql.append(q)
        return _Cur({"n": 3, "corpus_version": "v", "finished_at": None})


def test_readers_take_the_latest_finished_run_with_a_version_not_only_ok(monkeypatch):
    """What: status() and corpus() must not filter on status = 'ok'; they read the latest run that has a version.
    Use when: CI. Inputs: a recording connection. Output: assertion on the SQL. Side effects: none.
    Role: review-pr9-t6 Important 1 (the reader half)."""
    sql: list[str] = []
    db = Database("postgresql://x")
    monkeypatch.setattr(db, "_connect", lambda: _Conn(sql))
    db.status()
    try:
        db.corpus()
    except AttributeError:
        pass  # canned finished_at is None; only the SQL matters
    runs = [q for q in sql if "ingest_runs" in q]
    assert len(runs) == 2
    for q in runs:
        assert "status = 'ok'" not in q and "corpus_version IS NOT NULL" in q


def test_non_date_last_edited_time_fails_before_any_write(corpus):
    """What: 'last_edited_time: not-a-date' raises IngestError naming the file and writes nothing. Use when: CI.
    Inputs: one edited article. Output: assertion. Side effects: none. Role: review-pr9-t6 Minor 2."""
    p = corpus / "refund-policy.md"
    import re
    p.write_text(re.sub(r"last_edited_time:.*", "last_edited_time: not-a-date", p.read_text(), count=1))
    store = FakeStore()
    with pytest.raises(IngestError, match=r"refund-policy\.md.*last_edited_time"):
        run_ingest(corpus, POLICY, store, FakeEmbedder())
    assert store.swaps == [] and store.chunks == {}


def test_source_url_is_part_of_the_fingerprint():
    """What: changing only source_url changes the rendered hash, so the article is re-ingested. Use when: CI.
    Inputs: one article twice. Output: assertion. Side effects: none. Role: review-pr9-t6 Minor 6."""
    p = REAL / "refund-policy.md"
    a = parse_article(p.read_text(), p.name)
    chunks = chunk_article(a, LOADED_POLICY)
    b = replace(a, source_url=a.source_url + "x")
    assert fingerprint(a, chunks, "m") != fingerprint(b, chunks, "m")


@pytest.mark.parametrize("marker", [
    "<!-- tier: pro -->",
    "<!-- tiers: pro",
    "<!--tier pro -->",
])
def test_tier_like_comments_fail_closed(marker):
    """What: a singular 'tier' comment or an unclosed tiers comment raises instead of leaving the section at all.
    Use when: CI. Inputs: parametrized marker. Output: assertion. Side effects: none.
    Role: review-pr9-t6 Minor 8."""
    with pytest.raises(IngestError, match="tier"):
        chunk_article(art(f"## A\n{marker}\n{words(120)}\n"), LOADED_POLICY)
