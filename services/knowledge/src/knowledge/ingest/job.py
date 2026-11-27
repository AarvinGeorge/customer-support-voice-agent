"""What: the ingest job: data/articles in, embedded chunks and a corpus_version out. Run as `python -m knowledge.ingest.job`.
Use when: every deploy after migrations and on demand (Compose service ingest_knowledge); idempotent.
Inputs: --articles dir (default /data/articles), --policy file (default /data/policy.yaml); env KNOWLEDGE_DB_URL,
OPENAI_API_KEY. Output: one line "INGEST ok ... embedding_calls=N corpus_version=..." and exit 0; on failure the error
on stderr and exit 1. Side effects: Postgres writes (one transaction per changed article), OpenAI embedding calls only
for chunks whose content hash has no stored vector for the model. Role: draft A s2.2 steps 1 to 8."""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from contracts.policy import load_policy

from knowledge import config
from knowledge.ingest.chunker import Chunk, chunk_article
from knowledge.ingest.errors import IngestError
from knowledge.ingest.parse import Article, parse_article


@dataclass
class RunSummary:
    """What: result of one ingest run. Use when: printing and tests. Inputs: counters. Output: record.
    Side effects: none. Role: the numbers the Done-when quotes (embedding_calls, chunks_embedded)."""

    run_id: str = ""
    articles_seen: int = 0
    articles_changed: int = 0
    articles_skipped: int = 0
    chunks_total: int = 0
    chunks_embedded: int = 0
    chunks_deleted: int = 0
    embedding_calls: int = 0
    corpus_version: str = ""
    error_codes: list[str] = field(default_factory=list)


def fingerprint(article: Article, chunks: list[Chunk], model: str) -> str:
    """What: rendered_hash of an article: everything that ends up in its rows except the vectors.
    Use when: deciding to skip. Inputs: article, its chunks, model. Output: sha256 hex. Side effects: none.
    Role: changes when the article text, a quoted policy value, source_url, tiers or the chunker output changes."""
    payload = [model, article.title, article.source_url, article.last_updated, article.tiers,
               [[c.chunk_id, c.content_hash, c.ordinal, c.heading, c.section_path, c.source_url, c.tiers,
                 c.error_codes, c.policy_keys, c.injection_suspect, c.doc_version, c.last_updated] for c in chunks]]
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def corpus_version(pairs: list[tuple[str, str]]) -> str:
    """What: sha256 of the sorted 'chunk_id:content_hash' lines. Use when: end of run. Inputs: pairs.
    Output: hex. Side effects: none. Role: draft A step 7; stored on the run row and returned by search."""
    return hashlib.sha256("\n".join(sorted(f"{k}:{h}" for k, h in pairs)).encode()).hexdigest()


def _load(articles_dir: Path, policy) -> list[tuple[Article, list[Chunk], bytes]]:
    """What: parse and chunk every file, failing before any write. Use when: step 1 to 4. Inputs: dir, policy.
    Output: [(article, chunks, raw bytes)]. Side effects: reads files. Role: loud-fail checks: no articles,
    unknown placeholder, bad tier, oversized block, zero chunks, duplicate article_id."""
    files = sorted(articles_dir.glob("*.md"))
    if not files:
        raise IngestError(f"no articles found in {articles_dir}")
    out, seen = [], {}
    for path in files:
        raw = path.read_bytes()
        article = parse_article(raw.decode(), path.name)
        if article.article_id in seen:
            raise IngestError(f"duplicate article_id {article.article_id} in {path.name} and {seen[article.article_id]}")
        seen[article.article_id] = path.name
        out.append((article, chunk_article(article, policy), raw))
    return out


def run_ingest(articles_dir: Path, policy_path: Path, store, embedder) -> RunSummary:
    """What: run the whole ingest. Use when: the CLI or tests call it. Inputs: article dir, policy path, a Store
    (PgStore or a fake) and an embedder with embed_batch and calls. Output: RunSummary. Side effects: store writes,
    embedding calls. Role: skip unchanged articles, reuse stored vectors, embed the rest in one batched call, swap
    each changed article in one transaction, delete removed articles, set corpus_version; any failure marks the run
    failed and re-raises."""
    model = config.embedding_model()
    s = RunSummary(run_id=str(uuid.uuid4()))
    store.start_run(s.run_id, model)
    calls_before = embedder.calls
    try:
        loaded = _load(Path(articles_dir), load_policy(policy_path))
        s.articles_seen = len(loaded)
        changed = []
        for article, chunks, raw in loaded:
            rendered = fingerprint(article, chunks, model)
            if store.existing_article(article.article_id) == (rendered, model):
                s.articles_skipped += 1
            else:
                changed.append((article, chunks, raw, rendered))
        s.articles_changed = len(changed)
        need = sorted({c.content_hash for _, chunks, _, _ in changed for c in chunks})
        vectors = store.existing_vectors(need, model)
        missing = [h for h in need if h not in vectors]
        text_of = {c.content_hash: c.embed_text for _, chunks, _, _ in changed for c in chunks}
        for h, vec in zip(missing, embedder.embed_batch([text_of[h] for h in missing])):
            if len(vec) != config.EMBEDDING_DIMENSIONS:
                raise IngestError(f"embedding has {len(vec)} floats, expected {config.EMBEDDING_DIMENSIONS}")
            vectors[h] = vec
        s.chunks_embedded = len(missing)
        for article, chunks, raw, rendered in changed:
            row = {"article_slug": article.article_id, "title": article.title,
                   "source_path": f"data/articles/{article.filename}", "source_url": article.source_url,
                   "tiers": article.tiers, "doc_version": 1, "last_updated": article.last_updated,
                   "source_hash": hashlib.sha256(raw).hexdigest(), "rendered_hash": rendered, "embedding_model": model}
            rows = [{"chunk_id": c.chunk_id, "article_slug": c.article_slug, "ordinal": c.ordinal,
                     "section_path": c.section_path, "heading": c.heading, "header": c.header, "body": c.body,
                     "source_url": c.source_url, "tiers": c.tiers, "error_codes": c.error_codes,
                     "policy_keys": c.policy_keys, "injection_suspect": c.injection_suspect,
                     "token_count": c.token_count, "content_hash": c.content_hash, "embedding_model": model,
                     "embedding": vectors[c.content_hash], "doc_version": c.doc_version,
                     "last_updated": c.last_updated} for c in chunks]
            s.chunks_deleted += store.swap_article(row, rows)
        s.chunks_deleted += store.delete_missing_articles({a.article_id for a, _, _ in loaded})
        s.chunks_total = sum(len(c) for _, c, _ in loaded)
        s.error_codes = sorted({code for _, chunks, _ in loaded for c in chunks for code in c.error_codes})
        s.corpus_version = corpus_version(store.corpus_pairs())
        s.embedding_calls = embedder.calls - calls_before
        store.finish_run(s.run_id, "ok", s.corpus_version,
                         {"articles_seen": s.articles_seen, "articles_changed": s.articles_changed,
                          "chunks_embedded": s.chunks_embedded, "chunks_deleted": s.chunks_deleted}, None)
        return s
    except Exception as exc:
        store.finish_run(s.run_id, "failed", None, {}, f"{type(exc).__name__}: {exc}"[:500])
        raise


def _connect_store():
    """What: build the real store from env. Use when: the CLI runs. Inputs: env KNOWLEDGE_DB_URL.
    Output: PgStore. Side effects: none. Role: seam the tests replace."""
    import os

    from knowledge.ingest.store import PgStore
    url = os.environ.get("KNOWLEDGE_DB_URL", "")
    if not url:
        raise IngestError("KNOWLEDGE_DB_URL is not set")
    return PgStore(url)


def _make_embedder():
    """What: build the real embedder from env. Use when: the CLI runs. Inputs: env OPENAI_API_KEY.
    Output: OpenAIEmbedder. Side effects: none. Role: seam the tests replace."""
    from knowledge.embeddings import OpenAIEmbedder
    return OpenAIEmbedder()


def main(argv: list[str] | None = None) -> int:
    """What: CLI entry. Use when: `python -m knowledge.ingest.job`. Inputs: --articles, --policy.
    Output: exit code 0 with a summary line, or 1 with the error on stderr. Side effects: see run_ingest.
    Role: the deploy job; non-zero exit fails the deploy loudly."""
    ap = argparse.ArgumentParser(description="Ingest Help Center articles into the knowledge schema.")
    ap.add_argument("--articles", default="/data/articles")
    ap.add_argument("--policy", default="/data/policy.yaml")
    args = ap.parse_args(argv)
    try:
        s = run_ingest(Path(args.articles), Path(args.policy), _connect_store(), _make_embedder())
    except Exception as exc:  # any failure is a failed deploy step
        print(f"INGEST failed: {exc}", file=sys.stderr)
        return 1
    print(f"INGEST ok run={s.run_id} articles={s.articles_seen} changed={s.articles_changed} "
          f"skipped={s.articles_skipped} chunks={s.chunks_total} embedded={s.chunks_embedded} "
          f"deleted={s.chunks_deleted} embedding_calls={s.embedding_calls} "
          f"error_codes={','.join(s.error_codes)} corpus_version={s.corpus_version}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
