"""What: knowledge internal API shapes (doc 07 s4) for search and chunk reads.
Use when: knowledge builds a response, the agent's search_kb wrapper or chunk proxy parses one.
Inputs: none.
Output: SearchRequest, SearchResponse, Chunk, ChunkPublic, Confidence and their parts.
Side effects: none.
Role: the agent-to-knowledge wire contract, including Gate 1's verdict fields."""
from typing import Literal

from pydantic import Field, model_validator

from contracts.base import StrictModel

SearchTier = Literal["public", "free", "pro", "business", "enterprise"]
Verdict = Literal["pass", "weak", "none", "error", "uncalibrated"]
ERROR_CODE_PATTERN = r"^E-[A-Z]+-[0-9]{3}$"


class SearchRequest(StrictModel):
    """What: body of POST /v1/search. Use when: search_kb wrapper or calibration script.
    Inputs: query, error_code, tier and verified from claims, k, calibration flag.
    Output: model; raises if tier and verified disagree. Side effects: none. Role: doc 07 s4.1."""

    query: str = Field(min_length=1, max_length=2000)
    error_code: str | None = Field(default=None, pattern=ERROR_CODE_PATTERN)
    tier: SearchTier
    verified: bool
    k: int = Field(default=5, ge=1, le=8)
    calibration: bool = False

    @model_validator(mode="after")
    def _tier_matches_verified(self) -> "SearchRequest":
        """What: enforce verified=false with tier public and verified=true with a real tier.
        Use when: validation. Inputs: self. Output: self or ValueError. Side effects: none.
        Role: doc 07 s4.1 pairing rule (else 422)."""
        if self.verified == (self.tier == "public"):
            raise ValueError("verified=false requires tier 'public'; verified=true requires a real tier")
        return self


class QueryInfo(StrictModel):
    """What: how knowledge read the query. Use when: SearchResponse.query.
    Inputs: raw, normalized, codes, unknown codes. Output: model. Side effects: none. Role: doc 07 s4.1."""

    raw: str
    normalized: str
    codes: list[str]
    unknown_codes: list[str]


class ChunkScores(StrictModel):
    """What: ranking scores of one chunk. Use when: Chunk.scores, drawer display.
    Inputs: cosine, ranks, rrf score, fused rank. Output: model. Side effects: none. Role: doc 07 s4.1."""

    cosine: float
    vector_rank: int | None = None
    keyword_rank: int | None = None
    rrf_score: float
    fused_rank: int


class ChunkCitation(StrictModel):
    """What: how to cite a chunk in text and speech. Use when: Chunk.citation.
    Inputs: ref, label, spoken. Output: model. Side effects: none. Role: doc 07 s4.1."""

    ref: str
    label: str
    spoken: str


class ChunkPublic(StrictModel):
    """What: one chunk without scores (GET /v1/chunks/{id}). Use when: the drawer chunk proxy.
    Inputs: chunk fields. Output: model. Side effects: none. Role: doc 07 s4.2."""

    chunk_id: str
    article_slug: str
    article_title: str
    section_path: list[str]
    text: str
    tiers: list[str]
    error_codes: list[str]
    flags: list[Literal["injection_suspect"]]
    last_updated: str
    doc_version: int
    content_hash: str
    source_url: str
    citation: ChunkCitation


class Chunk(ChunkPublic):
    """What: one search hit with scores and relevance. Use when: SearchResponse.chunks.
    Inputs: ChunkPublic fields plus scores, relevant. Output: model. Side effects: none.
    Role: doc 07 s4.1 Chunk; only relevant chunks reach the model."""

    scores: ChunkScores
    relevant: bool


class Legs(StrictModel):
    """What: status of the vector and keyword legs. Use when: Confidence.legs.
    Inputs: vector, keyword. Output: model. Side effects: none. Role: doc 07 s4.1."""

    vector: Literal["ok", "empty", "failed"]
    keyword: Literal["ok", "empty", "failed"]


class Confidence(StrictModel):
    """What: Gate 1 verdict and its evidence. Use when: SearchResponse.confidence, gate event.
    Inputs: verdict, reason, cosines, thresholds, counts, legs, calibration fields.
    Output: model. Side effects: none. Role: doc 07 s4.1 Confidence."""

    verdict: Verdict
    reason: Literal["exact_code_match", "above_threshold", "weak_match", "below_threshold",
                    "unknown_error_code", "embedding_unavailable", "retrieval_unavailable",
                    "no_calibration"]
    top_cosine: float | None = None
    tau_low: float | None = None
    tau_high: float | None = None
    support_count: int
    code_match: bool
    legs: Legs
    calibration_id: str | None = None
    calibration_status: Literal["calibrated", "provisional", "none"]
    calibration_stale: bool


class TimingMs(StrictModel):
    """What: search timings in milliseconds. Use when: SearchResponse.timing_ms.
    Inputs: embed, sql, total. Output: model. Side effects: none. Role: doc 07 s4.1."""

    embed: int
    sql: int
    total: int


class SearchResponse(StrictModel):
    """What: body of a 200 from POST /v1/search. Use when: search_kb wrapper, drawer.
    Inputs: ids, corpus and model versions, query info, tiers, chunks, confidence, timings.
    Output: model. Side effects: none. Role: doc 07 s4.1 SearchResponse."""

    request_id: str
    trace_id: str
    corpus_version: str
    embedding_model: str
    query: QueryInfo
    allowed_tiers: list[str]
    chunks: list[Chunk]
    confidence: Confidence
    timing_ms: TimingMs
