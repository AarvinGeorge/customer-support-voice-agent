"""What: Gate 1 counts: send each labelled retrieval query to knowledge POST /v1/search and tabulate the verdict against the label
(raw confusion counts per split), recall@k for k = 3 to 8, and any forbidden (tier-filtered) chunk that came back.
Use when: `python -m evals run` (doc 07 s4.1 last paragraph, s13.3). Inputs: retrieval.yaml queries and a search function.
Output: a dict of raw counts. Side effects: one /v1/search call per query (each costs one embedding call on the knowledge side).
Role: the evidence for tau_low and tau_high; it only counts, it does not move thresholds."""
from __future__ import annotations

from collections.abc import Callable
from typing import Any

import httpx
from contracts.knowledge_api import SearchRequest

from .graders import base_id

KS = range(3, 9)


class SearchFailed(Exception):
    """What: raised when a search call fails. Use when: the HTTP call or its body is bad. Inputs: message. Output: exception.
    Side effects: none. Role: counted as request_failed, never as a verdict."""


class KnowledgeClient:
    """What: a client for knowledge POST /v1/search with the agent-scope token (KNOWLEDGE_TOKEN_AGENT). Use when: Gate 1 counts.
    Inputs: an httpx.Client with base_url, the token. Output: the response dict. Side effects: HTTP. Role: doc 07 s4.1 caller."""

    def __init__(self, http: httpx.Client, token: str) -> None:
        """What: remember client and token. Use when: the CLI builds it. Inputs: client, token. Output: none.
        Side effects: none. Role: init."""
        self.http, self.token = http, token

    def search(self, q: dict[str, Any]) -> dict[str, Any]:
        """What: search one labelled query at k = 8 (the verdict does not depend on k; recall is read by prefix).
        Use when: run_gate1. Inputs: a retrieval query dict. Output: SearchResponse as a dict; raises SearchFailed.
        Side effects: one HTTP call. Role: request is built with contracts SearchRequest so a bad query fails before sending."""
        req = SearchRequest(query=q["query"], error_code=q.get("error_code"), tier=q["tier"], verified=q["verified"], k=8)
        try:
            r = self.http.post("/v1/search", json=req.model_dump(exclude_none=True), headers={"Authorization": f"Bearer {self.token}"})
        except httpx.HTTPError as e:
            raise SearchFailed(f"transport error: {type(e).__name__}") from e
        if r.status_code != 200:
            raise SearchFailed(f"HTTP {r.status_code}")
        try:
            body = dict(r.json())
            body["confidence"]["verdict"]
            body["chunks"]
        except (ValueError, KeyError, TypeError) as e:
            raise SearchFailed(f"malformed search response: {type(e).__name__}") from e
        return body


def run_gate1(queries: list[dict[str, Any]], search: Callable[[dict[str, Any]], dict[str, Any]]) -> dict[str, Any]:
    """What: tabulate Gate 1 over the labelled queries. Use when: the run includes Gate 1. Inputs: queries, a search function.
    Output: {n, k, confusion{split: label: verdict: count}, recall_at_k{split: k: {hits, n}}, tier_leaks, failures, queries}.
    Side effects: calls search once per query. Role: raw counts only; a failed call is the verdict request_failed."""
    confusion: dict[str, dict[str, dict[str, int]]] = {"train": {}, "test": {}}
    recall: dict[str, dict[str, dict[str, int]]] = {s: {str(k): {"hits": 0, "n": 0} for k in KS} for s in ("train", "test")}
    leaks: list[dict[str, Any]] = []
    failures: list[dict[str, str]] = []
    rows: list[dict[str, Any]] = []
    for q in queries:
        split, label = q["split"], q["label"]
        try:
            r = search(q)
            verdict = r["confidence"]["verdict"]
            ids = [c["chunk_id"] for c in r["chunks"]]
            top = r["confidence"].get("top_cosine")
        except (SearchFailed, KeyError, TypeError, ValueError) as e:
            verdict, ids, top = "request_failed", [], None
            failures.append({"id": q["id"], "error": str(e) or type(e).__name__})
        cell = confusion[split].setdefault(label, {})
        cell[verdict] = cell.get(verdict, 0) + 1
        if label == "answerable":
            gold = {base_id(g) for g in q["gold_chunk_ids"]}
            ranks = [i + 1 for i, c in enumerate(ids) if base_id(c) in gold]
            for k in KS:
                recall[split][str(k)]["n"] += 1
                if ranks and ranks[0] <= k:
                    recall[split][str(k)]["hits"] += 1
        banned = {base_id(b) for b in q.get("forbidden_chunk_ids", [])}
        leaked = [c for c in ids if base_id(c) in banned]
        if leaked:
            leaks.append({"id": q["id"], "chunk_ids": leaked})
        rows.append({"id": q["id"], "split": split, "label": label, "verdict": verdict, "top_cosine": top, "chunk_ids": ids})
    return {"n": len(queries), "k": 8, "confusion": confusion, "recall_at_k": recall, "tier_leaks": leaks, "failures": failures, "queries": rows}
