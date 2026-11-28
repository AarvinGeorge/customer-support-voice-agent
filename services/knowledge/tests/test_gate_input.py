"""What: tests that Gate 1 scores the code-held customer message as well as the model's query (CS-E-02.F11).
Use when: CI. Inputs: fake database and embedder, a Calibration with the production thresholds. Output: pass or fail.
Side effects: none (no Postgres, no network). Role: rca-gate1-weak-band.md: the same customer message scored 0.180 and
0.420 on two model wordings, and G-21's query scored 0.570 against 0.622 for the message itself; the gate score must not
depend on how the model happened to word its search."""
import asyncio
import time

from contracts.knowledge_api import SearchRequest

from knowledge import gate, search
from tests.test_search import FakeDb, row

CAL = gate.Calibration(calibration_id="cal-test", status="provisional", embedding_model="text-embedding-3-small",
                       corpus_version="cv123", tau_low=0.355, tau_high=0.612, k_default=5)
QVEC, MVEC = [0.1] * 1536, [0.2] * 1536


class BatchEmbedder:
    """What: embedder fake with the batched call. Use when: these tests. Inputs: optional delay in seconds.
    Output: one vector per text (the first text gets QVEC, the second MVEC); records each request's texts.
    Side effects: may sleep. Role: fake."""

    def __init__(self, delay=0.0):
        """What: start empty. Use when: construction. Inputs: delay. Output: none. Side effects: none. Role: init."""
        self.requests: list[list[str]] = []
        self.delay = delay

    async def aembed_one(self, text, timeout_s):
        """What: one text, one request. Use when: a search without a customer message. Inputs: text, timeout.
        Output: QVEC. Side effects: records. Role: fake."""
        self.requests.append([text])
        return QVEC

    async def aembed_many(self, texts, timeout_s):
        """What: several texts, one request. Use when: a search with a customer message. Inputs: texts, timeout.
        Output: vectors in order. Side effects: records. Role: fake."""
        self.requests.append(list(texts))
        await asyncio.sleep(self.delay)
        return [QVEC, MVEC][: len(texts)]


class MessageDb(FakeDb):
    """What: FakeDb plus the per-chunk cosine read for a second vector. Use when: these tests. Inputs: leg rows and
    the message cosine per chunk id (or fail=True for the read). Output: canned rows. Side effects: records calls.
    Role: fake of Database.cosines."""

    def __init__(self, vec, msg_cos, cos_fail=False, cos_delay=0.0, leg_delay=0.0):
        """What: keep canned data. Use when: construction. Inputs: vector rows, {chunk_id: cosine}, fail flag, seconds
        the cosines read and each leg block for. Output: none. Side effects: none. Role: init."""
        super().__init__(vec, [])
        self.msg_cos, self.cos_fail, self.cos_delay, self.leg_delay = msg_cos, cos_fail, cos_delay, leg_delay

    def vector_leg(self, vec, tiers, limit):
        """What: the canned vector leg after leg_delay. Use when: search. Inputs: as FakeDb. Output: rows.
        Side effects: sleeps. Role: fake."""
        time.sleep(self.leg_delay)
        return super().vector_leg(vec, tiers, limit)

    def cosines(self, vec, chunk_ids):
        """What: canned message cosines. Use when: search scores the customer message. Inputs: vector, chunk ids.
        Output: {chunk_id: cosine}. Side effects: records. Role: fake (raises when cos_fail)."""
        self.calls.append(("cos", vec is MVEC, sorted(chunk_ids)))
        time.sleep(self.cos_delay)
        if self.cos_fail:
            raise RuntimeError("db down")
        return {c: self.msg_cos[c] for c in chunk_ids if c in self.msg_cos}


def run(db, emb, **kw):
    """What: one search through the real run_search with the production thresholds. Use when: every test.
    Inputs: db, embedder, request overrides. Output: SearchResponse. Side effects: none. Role: helper."""
    return timed(db, emb, **kw)[0]


def timed(db, emb, **kw):
    """What: one search, with the seconds run_search itself took (measured inside the loop, because asyncio.run
    then waits for any thread still sleeping). Use when: budget tests. Inputs: as run. Output: (SearchResponse,
    seconds). Side effects: none. Role: helper."""
    req = SearchRequest(**{"query": "Android app for Quillstack", "tier": "public", "verified": False, **kw})

    async def go():
        """What: run and time the search. Use when: timed. Inputs: none. Output: (response, seconds).
        Side effects: none. Role: helper."""
        t0 = time.monotonic()
        resp = await search.run_search(req, db, emb, "r", "t", CAL)
        return resp, time.monotonic() - t0
    return asyncio.run(go())


VEC = [row("a#1", v_rank=1, cosine=0.57), row("b#1", v_rank=2, cosine=0.40)]


def test_message_score_lifts_the_gate_when_the_query_wording_is_worse():
    """G-21 shape: query 0.57 (weak), message 0.62 on the same chunk: the gate scores the max, so pass."""
    emb = BatchEmbedder()
    resp = run(MessageDb(VEC, {"a#1": 0.62, "b#1": 0.30}), emb, customer_message="How long does a refund take?")
    assert resp.confidence.top_cosine == 0.62 and resp.confidence.verdict == "pass"
    assert emb.requests == [["Android app for Quillstack", "How long does a refund take?"]]  # one request, two texts
    assert resp.chunks[0].scores.cosine == 0.62 and resp.chunks[1].scores.cosine == 0.40


def test_message_score_never_lowers_the_query_score():
    """A follow-up message with no context ("yes, that one") scores low; the query's score stands."""
    resp = run(MessageDb(VEC, {"a#1": 0.10, "b#1": 0.05}), BatchEmbedder(), customer_message="yes, that one")
    assert resp.confidence.top_cosine == 0.57 and resp.confidence.verdict == "weak"


def test_relevant_flag_uses_the_gate_score():
    """A chunk under tau_low for the query but above it for the message is relevant (the model may see it)."""
    vec = [row("a#1", v_rank=1, cosine=0.30)]
    resp = run(MessageDb(vec, {"a#1": 0.50}), BatchEmbedder(), customer_message="How do I export my workspace?")
    assert resp.chunks[0].relevant and resp.confidence.support_count == 1


def test_no_customer_message_keeps_one_text_and_no_extra_read():
    """Backward compatible: the calibration script and older callers send only the query."""
    db, emb = MessageDb(VEC, {"a#1": 0.9}), BatchEmbedder()
    resp = run(db, emb)
    assert emb.requests == [["Android app for Quillstack"]] and not [c for c in db.calls if c[0] == "cos"]
    assert resp.confidence.top_cosine == 0.57


def test_message_equal_to_the_query_is_not_embedded_twice():
    """Same text after normalization: one text, no extra read."""
    db, emb = MessageDb(VEC, {"a#1": 0.9}), BatchEmbedder()
    run(db, emb, customer_message="  Android app for Quillstack ")
    assert emb.requests == [["Android app for Quillstack"]] and not [c for c in db.calls if c[0] == "cos"]


def test_message_read_failure_falls_back_to_the_query_score():
    """The extra read is best effort: a failure logs and the query's score is the gate score."""
    resp = run(MessageDb(VEC, {}, cos_fail=True), BatchEmbedder(), customer_message="How long does a refund take?")
    assert resp.confidence.top_cosine == 0.57 and resp.confidence.verdict == "weak"


def test_message_reads_only_the_returned_chunks():
    """The message is scored against the k chunks the model will see, not every vector row (review PR #86 m3: more
    rows than k, so "returned" and "all" differ)."""
    vec = [row(f"c#{i}", v_rank=i + 1, cosine=0.6 - i / 100) for i in range(8)]
    db = MessageDb(vec, {f"c#{i}": 0.3 for i in range(8)} | {"c#7": 0.95})
    resp = run(db, BatchEmbedder(), customer_message="How long does a refund take?", k=3)
    assert [c for c in db.calls if c[0] == "cos"] == [("cos", True, ["c#0", "c#1", "c#2"])]
    assert resp.confidence.top_cosine == 0.6  # c#7 scores 0.95 for the message but was not returned


def test_a_slow_message_read_falls_back_to_the_query_score_quickly():
    """Review PR #86 I2: the read is capped (about 0.3 s); a late read never delays the answer."""
    resp, took = timed(MessageDb(VEC, {"a#1": 0.9}, cos_delay=1.0), BatchEmbedder(), customer_message="How long?")
    assert took < 0.6
    assert resp.confidence.top_cosine == 0.57 and resp.confidence.verdict == "weak"


def test_a_slow_message_read_late_in_the_budget_is_never_a_503():
    """Review PR #86 I2 reproduction: embed 1.15 s plus legs 0.9 s answered in about 2.1 s without a message, and a 1.0 s
    message read pushed it past 3.0 s into RETRIEVAL_UNAVAILABLE. Now it answers on the query score inside 3.0 s."""
    resp, took = timed(MessageDb(VEC, {"a#1": 0.9}, cos_delay=1.0, leg_delay=0.9), BatchEmbedder(delay=1.15),
                       customer_message="How long?")
    assert took < 3.0
    assert resp.confidence.top_cosine == 0.57 and resp.confidence.verdict == "weak"
