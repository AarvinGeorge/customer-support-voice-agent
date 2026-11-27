"""What: hybrid search: query normalization, embedding with a budget, vector and keyword legs, RRF fusion, response.
Use when: POST /v1/search runs (doc 07 s4.1). Inputs: a SearchRequest, a database handle, an embedder.
Output: a contract SearchResponse, or an ApiError 503 RETRIEVAL_UNAVAILABLE when no leg can answer.
Side effects: one embedding call (a second only for a fast failure with at least 2.5 s left), two SQL queries in parallel.
Role: whole request hard-capped at 3.0 s (asyncio.wait_for in run_search): embedding 1.2 s (no retry after a timeout), SQL statement timeout 1 s per leg. When
the embedding is unavailable the keyword leg still runs, so a degraded answer exists inside the budget (worst case about
2.2 s). Gate 1 (verdicts, thresholds) is CS-E-02.S3.T3; here confidence is 'uncalibrated' except an embedding outage."""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Literal

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
READ_TIMEOUT_S = 0.6  # vocabulary and corpus_version reads (cached 30 s in Database)
LEG_LIMIT = 20
RRF_K = 60
DEFAULT_TAU_LOW = 0.30  # provisional default (doc 07 s4.1); replaced by the calibration file in CS-E-02.S3.T3


def fuse(vec_rows: list[dict], kw_rows: list[dict], k: int, codes: list[str] | tuple = ()) -> list[dict]:
    """What: reciprocal rank fusion of the two legs. Use when: both legs returned. Inputs: vector rows (with
    v_rank), keyword rows (with f_rank), k. Output: up to k merged rows with rrf_score, v_rank, f_rank and fused_rank
    from 1. Side effects: none. Role: score = sum of 1/(60+rank) over the legs a chunk appears in; ties go to the
    better vector rank, then chunk id (deterministic). Rows carrying a detected error code (`codes`) come first:
    a code the caller named is the strongest signal there is, and a keyword-only hit would otherwise score below
    every two-leg chunk (review-pr15 Important 1)."""
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
    wanted = set(codes)
    ordered = sorted(merged.values(), key=lambda r: (not (wanted & set(r["error_codes"])), -r["rrf_score"],
                                                      r["v_rank"] or 10**9, r["chunk_id"]))[:k]
    for i, r in enumerate(ordered, 1):
        r["fused_rank"] = i
    return ordered


def _status(rows: list[dict] | None) -> Literal["ok", "empty", "failed"]:
    """What: leg status word. Use when: building Legs. Inputs: rows or None for a failed leg.
    Output: failed, empty or ok. Side effects: none. Role: doc 07 s4.1 legs."""
    return "failed" if rows is None else ("ok" if rows else "empty")


def _transient(exc: Exception) -> bool:
    """What: can a retry succeed. Use when: an embedding attempt failed fast. Inputs: the error. Output: True for a
    timeout, a transport error, 429 or 5xx; False for other statuses (400, 401, ...). Side effects: none.
    Role: review-pr15 Minor 2."""
    status = getattr(exc, "status", None)
    return status is None or status == 429 or status >= 500


async def _embed(embedder, text: str, t0: float, trace_id: str) -> list[float] | None:
    """What: embed the query within the budget. Use when: search starts. Inputs: embedder (or None), text, start time.
    Output: vector, or None when unavailable. Side effects: up to two embedding calls. Role: 1.2 s timeout; retry once
    only if at least 2.5 s of the 3.0 s budget remain when the first attempt fails (a timeout never leaves that much)
    and the failure is transient. Warnings carry trace_id."""
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
            log.warning("trace_id=%s embedding attempt %d failed: %s", trace_id, attempt, type(exc).__name__)
            if attempt == 2 or not _transient(exc) or TOTAL_BUDGET_S - (time.monotonic() - t0) < RETRY_MIN_REMAINING_S:
                return None
    return None


async def _leg(fn, trace_id: str, *args) -> list[dict] | None:
    """What: run one blocking SQL leg in a thread. Use when: legs run in parallel. Inputs: callable, trace id, args.
    Output: rows, or None when it raised. Side effects: SQL; logs a warning on failure. Role: a failed leg degrades,
    it does not crash the request."""
    try:
        return await asyncio.to_thread(fn, *args)
    except Exception as exc:  # psycopg errors and timeouts; the type is logged, never returned to the caller
        log.warning("trace_id=%s leg %s failed: %s", trace_id, getattr(fn, "__name__", "leg"), type(exc).__name__)
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


async def _cached_read(fn, fallback, trace_id: str, what: str):
    """What: one short database read (vocabulary or corpus_version) that can never eat the search budget.
    Use when: run_search needs them. Inputs: blocking callable, value to use on failure, trace id, label.
    Output: the value or the fallback. Side effects: a thread call; logs a warning on failure. Role: review-pr15
    Important 2: each read is capped at READ_TIMEOUT_S and served from Database's 30 s cache when warm."""
    try:
        return await asyncio.wait_for(asyncio.to_thread(fn), READ_TIMEOUT_S)
    except Exception as exc:  # includes the timeout; the type is logged, never returned
        log.warning("trace_id=%s %s read failed: %s", trace_id, what, type(exc).__name__)
        return fallback


async def run_search(req: SearchRequest, db, embedder, request_id: str, trace_id: str) -> SearchResponse:
    """What: answer one search within TOTAL_BUDGET_S. Use when: the route calls it. Inputs: request, db (vocabulary,
    vector_leg, keyword_leg, corpus_version), embedder or None, ids. Output: SearchResponse. Side effects: see module
    docstring. Role: the 3.0 s cap covers every step, so a stalled database ends in 503 RETRIEVAL_UNAVAILABLE at about
    3.0 s (doc 07 s4.1); also 503 when the database fails in every leg that ran; 422 for a query empty after cleaning."""
    try:
        return await asyncio.wait_for(_run_search(req, db, embedder, request_id, trace_id), TOTAL_BUDGET_S)
    except asyncio.TimeoutError:
        log.warning("trace_id=%s search exceeded the %.1f s budget", trace_id, TOTAL_BUDGET_S)
        raise ApiError(503, "RETRIEVAL_UNAVAILABLE", "retrieval_failed", "Search is unavailable right now.",
                       retryable=True, retry_after_s=2)


async def _run_search(req: SearchRequest, db, embedder, request_id: str, trace_id: str) -> SearchResponse:
    """What: the search steps without the outer time cap. Use when: run_search wraps it. Inputs: as run_search.
    Output: SearchResponse. Side effects: see module docstring. Role: normalize, embed, two legs, fuse, respond."""
    t0 = time.monotonic()
    vocab = await _cached_read(db.vocabulary, set(), trace_id, "vocabulary")
    norm = normalize_query(req.query, vocab)
    if not norm.text:
        raise ApiError(422, "INVALID_REQUEST", "validation", "The query is empty.")
    codes, unknown = list(norm.codes), list(norm.unknown_codes)
    if req.error_code:
        extra = normalize_query(req.error_code, vocab)
        codes += [c for c in extra.codes if c not in codes]
        if req.error_code.upper() in vocab and req.error_code.upper() not in codes:
            codes.append(req.error_code.upper())
        unknown += [c for c in extra.unknown_codes if c not in unknown]
        if req.error_code.upper() not in vocab and req.error_code.upper() not in unknown:
            unknown.append(req.error_code.upper())
    unknown = [c for c in unknown if c not in codes]  # a code is known or unknown, never both
    text = norm.text
    for c in codes:  # a code named only in error_code must still reach the embedding and the keyword leg
        if c not in text:
            text = f"{text} {c}"
    tiers = ["all"] if req.tier == "public" else ["all", req.tier]

    qvec = await _embed(embedder, text, t0, trace_id)
    t_embed = time.monotonic()
    vec_task = _leg(db.vector_leg, trace_id, qvec, tiers, LEG_LIMIT) if qvec is not None else None
    kw_task = _leg(db.keyword_leg, trace_id, text, codes, tiers, qvec, LEG_LIMIT)
    results = await asyncio.gather(*([vec_task] if vec_task else []), kw_task)
    kw_rows = results[-1]
    vec_rows = results[0] if vec_task else None
    t_sql = time.monotonic()
    if kw_rows is None and (vec_rows is None):
        raise ApiError(503, "RETRIEVAL_UNAVAILABLE", "retrieval_failed", "Search is unavailable right now.",
                       retryable=True, retry_after_s=2)

    fused = fuse(vec_rows or [], kw_rows or [], req.k, codes)
    chunks = [_chunk(r, vec_rows is not None, codes) for r in fused]
    carried = {c for ch in chunks for c in ch.error_codes}
    code_match = bool(set(codes) & carried)
    vector_failed = qvec is None or vec_rows is None
    version = await _cached_read(db.corpus_version, "none", trace_id, "corpus_version")
    confidence = Confidence(
        verdict="error" if vector_failed else "uncalibrated",
        reason="embedding_unavailable" if vector_failed else "no_calibration",
        top_cosine=max((c.scores.cosine for c in chunks), default=None) if not vector_failed else None,
        support_count=sum(c.relevant for c in chunks), code_match=code_match,
        legs=Legs(vector=_status(vec_rows if qvec is not None else None), keyword=_status(kw_rows)),
        calibration_status="none", calibration_stale=False)
    return SearchResponse(
        request_id=request_id, trace_id=trace_id, corpus_version=version or "none",
        embedding_model=config.embedding_model(),
        query=QueryInfo(raw=req.query, normalized=text, codes=codes, unknown_codes=unknown),
        allowed_tiers=tiers, chunks=chunks, confidence=confidence,
        timing_ms=TimingMs(embed=int((t_embed - t0) * 1000), sql=int((t_sql - t_embed) * 1000),
                           total=int((time.monotonic() - t0) * 1000)))
