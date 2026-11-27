"""What: OpenAI embeddings client: sync batch calls for ingest, an async single call with a hard timeout for search.
Use when: ingest embeds new chunks (embed_batch) and search embeds a query (aembed_one).
Inputs: OPENAI_API_KEY; model from knowledge.config. Output: lists of 1536 floats.
Side effects: HTTPS calls to api.openai.com; `calls` counts every HTTP request and `inputs` every text sent.
Role: the only module that spends embedding money. Plain httpx, no SDK; the counter is what the 'second run makes zero
calls' check reads."""
from __future__ import annotations

import asyncio
import os
import time

import httpx

from knowledge import config

URL = "https://api.openai.com/v1/embeddings"


class EmbeddingError(Exception):
    """What: an embedding call failed or returned bad data. Use when: caught by ingest (abort) or search
    (keyword-only fallback). Inputs: message. Output: exception. Side effects: none. Role: one error type."""


class OpenAIEmbedder:
    """What: embeddings client. Use when: ingest or search needs vectors. Inputs: api_key (default env),
    model, batch_size, retries, retry_sleep, transport (tests). Output: instance with embed_batch, aembed_one,
    and the counters calls and inputs. Side effects: network. Role: batching, retries on 429 and 5xx, vector
    length check."""

    def __init__(self, api_key: str | None = None, model: str | None = None, batch_size: int = 100,
                 retries: int = 2, retry_sleep: float = 1.0, transport: httpx.BaseTransport | None = None):
        """What: set up clients and counters. Use when: construction. Inputs: see class. Output: none.
        Side effects: none (no request). Role: init; raises EmbeddingError when no key is available."""
        key = api_key or os.environ.get("OPENAI_API_KEY", "")
        if not key:
            raise EmbeddingError("OPENAI_API_KEY is not set")
        self.model = model or config.embedding_model()
        self.batch_size, self.retries, self.retry_sleep = batch_size, retries, retry_sleep
        headers = {"Authorization": f"Bearer {key}"}
        self.client = httpx.Client(headers=headers, transport=transport, timeout=30)
        self._atransport = transport if isinstance(transport, httpx.AsyncBaseTransport) else None
        self._aheaders = headers
        self.calls = 0
        self.inputs = 0

    def _payload(self, texts: list[str]) -> dict:
        """What: request body. Use when: sending. Inputs: texts. Output: dict. Side effects: none. Role: one shape."""
        return {"model": self.model, "input": texts, "encoding_format": "float"}

    @staticmethod
    def _vectors(response: httpx.Response, n: int) -> list[list[float]]:
        """What: parse and validate a 200 body. Use when: after a response. Inputs: response, expected count.
        Output: vectors in input order. Side effects: none. Role: raises EmbeddingError for a wrong count or length."""
        data = sorted(response.json()["data"], key=lambda d: d["index"])
        if len(data) != n:
            raise EmbeddingError(f"asked for {n} embeddings, got {len(data)}")
        vecs = [d["embedding"] for d in data]
        for v in vecs:
            if len(v) != config.EMBEDDING_DIMENSIONS:
                raise EmbeddingError(f"embedding has {len(v)} floats, expected {config.EMBEDDING_DIMENSIONS}")
        return vecs

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        """What: embed many texts, up to batch_size per request. Use when: ingest. Inputs: texts.
        Output: one vector per text, same order; [] and no request for no texts. Side effects: network; counters.
        Role: retries 429 and 5xx up to `retries` times, then raises EmbeddingError naming the status."""
        out: list[list[float]] = []
        for i in range(0, len(texts), self.batch_size):
            chunk = texts[i:i + self.batch_size]
            self.inputs += len(chunk)
            for attempt in range(self.retries + 1):
                self.calls += 1
                try:
                    resp = self.client.post(URL, json=self._payload(chunk))
                except httpx.HTTPError as exc:
                    if attempt == self.retries:
                        raise EmbeddingError(f"embedding request failed: {type(exc).__name__}") from exc
                    time.sleep(self.retry_sleep)
                    continue
                if resp.status_code == 200:
                    out.extend(self._vectors(resp, len(chunk)))
                    break
                if resp.status_code in (429, 500, 502, 503, 504) and attempt < self.retries:
                    time.sleep(self.retry_sleep)
                    continue
                raise EmbeddingError(f"embedding request failed with status {resp.status_code}")
        return out

    async def aembed_one(self, text: str, timeout_s: float) -> list[float]:
        """What: embed one text with a hard timeout. Use when: search needs the query vector.
        Inputs: text, timeout in seconds. Output: one vector. Side effects: one request; counters.
        Role: no retry here (the caller owns the retry budget, doc 07 s4.1); raises EmbeddingError on timeout,
        transport error or a non-200 status."""
        self.calls += 1
        self.inputs += 1
        try:
            async with httpx.AsyncClient(headers=self._aheaders, transport=self._atransport) as client:
                resp = await asyncio.wait_for(client.post(URL, json=self._payload([text])), timeout=timeout_s)
        except asyncio.TimeoutError as exc:
            raise EmbeddingError("embedding timeout") from exc
        except httpx.HTTPError as exc:
            raise EmbeddingError(f"embedding request failed: {type(exc).__name__}") from exc
        if resp.status_code != 200:
            raise EmbeddingError(f"embedding request failed with status {resp.status_code}")
        return self._vectors(resp, 1)[0]
