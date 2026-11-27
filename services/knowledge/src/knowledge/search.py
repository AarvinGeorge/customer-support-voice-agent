"""What: hybrid search: query normalization, embedding with a budget, vector and keyword legs, RRF fusion, response.
Use when: POST /v1/search runs (doc 07 s4.1). Inputs: a SearchRequest, a database handle, an embedder.
Output: a contract SearchResponse, or an ApiError 503 RETRIEVAL_UNAVAILABLE when no leg can answer.
Side effects: one embedding call (a second only for a fast failure with at least 2.5 s left), two SQL queries in parallel.
Role: whole request capped near 3.0 s: embedding 1.2 s (no retry after a timeout), SQL statement timeout 1 s per leg. When
the embedding is unavailable the keyword leg still runs, so a degraded answer exists inside the budget (worst case about
2.2 s). Gate 1 (verdicts, thresholds) is CS-E-02.S3.T3; here confidence is 'uncalibrated' except an embedding outage."""
from __future__ import annotations

import asyncio
import logging
import time

from contracts.knowledge_api import (Chunk, ChunkCitation, ChunkScores, Confidence, Legs, QueryInfo, SearchRequest,
                                     SearchResponse, TimingMs)

from knowledge import config
from knowledge.embeddings import EmbeddingError
from knowledge.errors import ApiError
from knowledge.normalize import normalize_query

log = logging.getLogger("knowledge.search")

TOTAL_BUDGET_S = 3.0
EMBED_TIMEOUT_S = 1.2
RETRY_MIN_REMAINING_S = 2.5
SQL_RESERVE_S = 1.0
LEG_LIMIT = 20
RRF_K = 60
DEFAULT_TAU_LOW = 0.30  # provisional default (doc 07 s4.1); replaced by the calibration file in CS-E-02.S3.T3


def fuse(vec_rows: list[dict], kw_rows: list[dict], k: int) -> list[dict]:
    """What: reciprocal rank fusion of the two legs. Use when: both legs returned. Inputs: vector rows (with
    v_rank), keyword rows (with f_rank), k. Output: up to k merged rows with rrf_score, v_rank, f_rank and fused_rank
    from 1. Side effects: none. Role: score = sum of 1/(60+rank) over the legs a chunk appears in; ties go to the
    better vector rank, then chunk id (deterministic)."""
    merged: dict[str, dict] = {}
    for r in vec_rows:
        merged[r["chunk_id"]] = {**r, "v_rank": r["v_rank"], "f_rank": None}
    for r in kw_rows:
        if r["chunk_id"] in merged:
            merged[r["chunk_id"]]["f_rank"] = r["f_rank"]
        else:
            merged[r["chunk_id"]] = {**r, "v_rank": None, "f_rank": r["f_rank"]}
    for r in merged.values():
        r["rrf_score"] = (1 / (RRF_K + r["v_rank"]) if r["v_rank"] else 0) + (1 / (RRF_K + r["f_rank"]) if r["f_rank"] else 0)
    ordered = sorted(merged.values(), key=lambda r: (-r["rrf_score"], r["v_rank"] or 10**9, r["chunk_id"]))[:k]
    for i, r in enumerate(ordered, 1):
        r["fused_rank"] = i
    return ordered


def _status(rows: list[dict] | None) -> str:
    """What: leg status word. Use when: building Legs. Inputs: rows or None for a failed leg.
    Output: failed, empty or ok. Side effects: none. Role: doc 07 s4.1 legs."""
    return "failed" if rows is None else ("ok" if rows else "empty")


async def _embed(embedder, text: str, t0: float) -> list[float] | None:
    """What: embed the query within the budget. Use when: search starts. Inputs: embedder (or None), text, start time.
    Output: vector, or None when unavailable. Side effects: up to two embedding calls. Role: 1.2 s timeout; retry once
    only if at least 2.5 s of the 3.0 s budget remain when the first attempt fails (a timeout never leaves that much)."""
    if embedder is None:
        return None
    for attempt in (1, 2):
        remaining = TOTAL_BUDGET_S - (time.monotonic() - t0)
        timeout = min(EMBED_TIMEOUT_S, remaining - SQL_RESERVE_S)
        if timeout <= 0:
            return None
        try:
            return await asyncio.wait_for(embedder.aembed_one(text, timeout), timeout + 0.05)
        except (EmbeddingError, asyncio.TimeoutError) as exc:
            log.warning("embedding attempt %d failed: %s", attempt, type(exc).__name__)
            if attempt == 2 or TOTAL_BUDGET_S - (time.monotonic() - t0) < RETRY_MIN_REMAINING_S:
                return None
    return None


async def _leg(fn, *args) -> list[dict] | None:
    """What: run one blocking SQL leg in a thread. Use when: legs run in parallel. Inputs: callable and args.
    Output: rows, or None when it raised. Side effects: SQL; logs a warning on failure. Role: a failed leg degrades,
    it does not crash the request."""
    try:
        return await asyncio.to_thread(fn, *args)
    except Exception as exc:  # psycopg errors and timeouts; the type is logged, never returned to the caller
        log.warning("leg %s failed: %s", getattr(fn, "__name__", "leg"), type(exc).__name__)
        return None


def _chunk(r: dict, vector_ok: bool, codes: list[str]) -> Chunk:
    """What: build a contract Chunk from a fused row. Use when: assembling the response. Inputs: row, whether the
    vector leg worked, detected codes. Output: Chunk with scores, relevant flag and a citation carrying source_url.
    Side effects: none. Role: relevant = cosine at or above tau_low, or it carries a detected code."""
    cosine = float(r["cosine"]) if vector_ok else 0.0
    carries = bool(set(codes) & set(r["error_codes"]))
    path = r["section_path"]
    return Chunk(
        chunk_id=r["chunk_id"], article_slug=r["article_slug"], article_title=r["article_title"], section_path=path,
        text=r["header"] + "\n\n" + r["body"], tiers=r["tiers"], error_codes=r["error_codes"],
        flags=["injection_suspect"] if r["injection_suspect"] else [], last_updated=str(r["last_updated"]),
        doc_version=r["doc_version"], content_hash=r["content_hash"].strip(), source_url=r["source_url"],
        citation=ChunkCitation(ref=r["chunk_id"], label=" > ".join([r["article_title"], *path]),
                               spoken=f"the {r['article_title']} article, {path[-1]} section"),
        scores=ChunkScores(cosine=cosine, vector_rank=r["v_rank"], keyword_rank=r["f_rank"],
                           rrf_score=r["rrf_score"], fused_rank=r["fused_rank"]),
        relevant=carries or cosine >= DEFAULT_TAU_LOW)


async def run_search(req: SearchRequest, db, embedder, request_id: str, trace_id: str) -> SearchResponse:
    """What: answer one search. Use when: the route calls it. Inputs: request, db (vocabulary, vector_leg,
    keyword_leg, status), embedder or None, ids. Output: SearchResponse. Side effects: see module docstring.
    Role: raises ApiError 503 RETRIEVAL_UNAVAILABLE when the database fails in every leg that ran."""
    t0 = time.monotonic()
    try:
        vocab = await asyncio.to_thread(db.vocabulary)
    except Exception:
        vocab = set()
    norm = normalize_query(req.query, vocab)
    codes, unknown = list(norm.codes), list(norm.unknown_codes)
    if req.error_code:
        extra = normalize_query(req.error_code, vocab)
        codes += [c for c in extra.codes if c not in codes]
        if req.error_code.upper() in vocab and req.error_code.upper() not in codes:
            codes.append(req.error_code.upper())
        unknown += [c for c in extra.unknown_codes if c not in unknown]
        if req.error_code.upper() not in vocab and req.error_code.upper() not in unknown:
            unknown.append(req.error_code.upper())
    tiers = ["all"] if req.tier == "public" else ["all", req.tier]

    qvec = await _embed(embedder, norm.text, t0)
    t_embed = time.monotonic()
    vec_task = _leg(db.vector_leg, qvec, tiers, LEG_LIMIT) if qvec is not None else None
    kw_task = _leg(db.keyword_leg, norm.text, codes, tiers, qvec, LEG_LIMIT)
    results = await asyncio.gather(*([vec_task] if vec_task else []), kw_task)
    kw_rows = results[-1]
    vec_rows = results[0] if vec_task else None
    t_sql = time.monotonic()
    if kw_rows is None and (vec_rows is None):
        raise ApiError(503, "RETRIEVAL_UNAVAILABLE", "retrieval_failed", "Search is unavailable right now.",
                       retryable=True, retry_after_s=2)

    fused = fuse(vec_rows or [], kw_rows or [], req.k)
    chunks = [_chunk(r, vec_rows is not None, codes) for r in fused]
    carried = {c for ch in chunks for c in ch.error_codes}
    unknown += [c for c in codes if c not in carried and c not in unknown]
    code_match = bool(set(codes) & carried)
    vector_failed = qvec is None or vec_rows is None
    try:
        status = await asyncio.to_thread(db.status)
    except Exception:
        status = None
    confidence = Confidence(
        verdict="error" if vector_failed else "uncalibrated",
        reason="embedding_unavailable" if vector_failed else "no_calibration",
        top_cosine=max((c.scores.cosine for c in chunks), default=None) if not vector_failed else None,
        support_count=sum(c.relevant for c in chunks), code_match=code_match,
        legs=Legs(vector=_status(vec_rows if qvec is not None else None), keyword=_status(kw_rows)),
        calibration_status="none", calibration_stale=False)
    return SearchResponse(
        request_id=request_id, trace_id=trace_id, corpus_version=(status or {}).get("corpus_version") or "none",
        embedding_model=config.embedding_model(),
        query=QueryInfo(raw=req.query, normalized=norm.text, codes=codes, unknown_codes=unknown),
        allowed_tiers=tiers, chunks=chunks, confidence=confidence,
        timing_ms=TimingMs(embed=int((t_embed - t0) * 1000), sql=int((t_sql - t_embed) * 1000),
                           total=int((time.monotonic() - t0) * 1000)))
