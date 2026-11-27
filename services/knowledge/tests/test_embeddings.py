"""What: tests for the OpenAI embeddings client (batching, counter, retries, vector checks, async timeout).
Use when: CI. Inputs: httpx MockTransport (no network). Output: pass or fail. Side effects: none.
Role: CS-E-02.S1.T6 'batch embeddings' and the call counter the Done-when quotes; also the search-time budget."""
import asyncio
import json

import httpx
import pytest

from knowledge import config
from knowledge.embeddings import EmbeddingError, OpenAIEmbedder


def make(handler, **kw):
    """What: build an embedder on a mock transport. Use when: every test. Inputs: handler(request)->Response.
    Output: OpenAIEmbedder. Side effects: none. Role: test helper."""
    return OpenAIEmbedder(api_key="test-key", transport=httpx.MockTransport(handler), **kw)


def ok(request: httpx.Request) -> httpx.Response:
    """What: echo a vector of 1536 floats per input. Use when: happy path. Inputs: request.
    Output: response. Side effects: none. Role: fake OpenAI."""
    n = len(json.loads(request.content)["input"])
    return httpx.Response(200, json={"data": [{"index": i, "embedding": [0.1] * 1536} for i in range(n)]})


def test_config_defaults():
    """What: model and dimensions are the doc 07 values. Use when: CI. Inputs: none. Output: assertion.
    Side effects: none. Role: s13.2 embeddings role."""
    assert config.embedding_model() == "text-embedding-3-small" and config.EMBEDDING_DIMENSIONS == 1536


def test_batch_is_one_call_and_counted():
    """What: 5 texts make one request carrying the array; the counter shows 1 call and 5 inputs.
    Use when: CI. Inputs: five texts. Output: assertion. Side effects: none. Role: Done-when counter."""
    seen = []

    def handler(req):
        seen.append(json.loads(req.content))
        return ok(req)
    e = make(handler)
    vecs = e.embed_batch([f"t{i}" for i in range(5)])
    assert len(vecs) == 5 and all(len(v) == 1536 for v in vecs)
    assert (e.calls, e.inputs) == (1, 5) and len(seen) == 1
    assert seen[0]["model"] == "text-embedding-3-small" and seen[0]["input"][0] == "t0"
    assert req_auth(e) == "Bearer test-key"


def req_auth(e) -> str:
    """What: the Authorization header the client sends. Use when: asserting auth. Inputs: embedder.
    Output: header text. Side effects: none. Role: test helper."""
    return e.client.headers["Authorization"]


def test_large_input_is_split_into_batches():
    """What: 250 texts with a batch size of 100 make 3 calls and keep order. Use when: CI.
    Inputs: 250 texts. Output: assertion. Side effects: none. Role: batch limit."""
    e = make(ok, batch_size=100)
    assert len(e.embed_batch([str(i) for i in range(250)])) == 250
    assert (e.calls, e.inputs) == (3, 250)


def test_empty_input_makes_no_call():
    """What: no texts means no request. Use when: CI. Inputs: []. Output: assertion. Side effects: none.
    Role: the zero-call path of a second run."""
    e = make(ok)
    assert e.embed_batch([]) == [] and e.calls == 0


def test_wrong_vector_length_fails():
    """What: a 1024-float vector raises EmbeddingError naming the length. Use when: CI. Inputs: bad response.
    Output: assertion. Side effects: none. Role: draft A step 8 loud check."""
    def bad(req):
        return httpx.Response(200, json={"data": [{"index": 0, "embedding": [0.1] * 1024}]})
    with pytest.raises(EmbeddingError, match="1024"):
        make(bad).embed_batch(["x"])


def test_retries_429_then_succeeds_and_counts_each_attempt():
    """What: a 429 then a 200 returns vectors; calls counts both requests. Use when: CI. Inputs: scripted
    responses. Output: assertion. Side effects: none. Role: ingest retry (sleep patched out by retry_sleep=0)."""
    state = {"n": 0}

    def handler(req):
        state["n"] += 1
        return httpx.Response(429, json={}) if state["n"] == 1 else ok(req)
    e = make(handler, retry_sleep=0)
    assert len(e.embed_batch(["x"])) == 1 and e.calls == 2


def test_gives_up_after_retries_with_error():
    """What: persistent 500 raises EmbeddingError. Use when: CI. Inputs: always 500. Output: assertion.
    Side effects: none. Role: loud failure."""
    e = make(lambda r: httpx.Response(500, json={}), retry_sleep=0, retries=2)
    with pytest.raises(EmbeddingError, match="500"):
        e.embed_batch(["x"])
    assert e.calls == 3


def test_async_embed_one_times_out():
    """What: aembed_one raises EmbeddingError when the server hangs longer than the timeout. Use when: CI.
    Inputs: a handler that sleeps. Output: assertion. Side effects: sleeps 0.3 s. Role: search budget."""
    async def slow(req):
        await asyncio.sleep(0.5)
        return ok(req)
    e = make(slow)
    with pytest.raises(EmbeddingError, match="timeout"):
        asyncio.run(e.aembed_one("x", timeout_s=0.1))
