"""What: run the Gate 1 probe set through the real search code and print each probe's top cosine, then the provisional
tau_low and tau_high by the doc 07 s4.1 method, plus the Gate 1 verdict each probe would get with those numbers.
Use when: setting or re-checking provisional thresholds (spike 14, D-052). Inputs: env KNOWLEDGE_DB_URL and
OPENAI_API_KEY; run inside the knowledge container: `docker compose exec -T knowledge python - < services/knowledge/scripts/probe_retrieval.py`
after the ingest. Output: JSON on stdout with corpus_version (read from the database, never from a cached search
response), model, chunk count, k, UTC time, per-probe rows and thresholds.
Side effects: ONE real embedding request for the whole probe set (all query texts in one batch), SQL reads.
Role: searches run through knowledge.search.run_search with calibration=true and a cache of the batched vectors, so the
scores are exactly what /v1/search returns (top cosine among the k=5 returned chunks) at 1 request instead of 1 per probe.
Replaced by evals/calibrate_retrieval.py once the golden set exists."""
import asyncio
import datetime
import json
import os
import statistics

from contracts.knowledge_api import SearchRequest

from knowledge import config, search
from knowledge.db import Database
from knowledge.embeddings import OpenAIEmbedder

# (label, scenario, tier, query). Tier "public" means unverified. Scenario questions are the S1 to S5 customer
# questions of L1 s4 and the showcase Loom order (doc 04), phrased as the customer would say them; tiers are the
# personas' (Natasha Pro, Peter and Tony Business).
PROBES = [
    ("answerable", "generic", "public", "How do I request a refund for a duplicate charge?"),
    ("answerable", "generic", "public", "How long does a refund take to reach my card?"),
    ("answerable", "generic", "public", "How do I change the billing email on my account?"),
    ("answerable", "generic", "public", "Who can update billing contact details?"),
    ("answerable", "generic", "public", "What can a member do compared with an admin?"),
    ("answerable", "generic", "public", "How do I make someone an admin on my team?"),
    ("answerable", "generic", "public", "Single sign-on login fails with an invalid assertion, what should I check?"),
    ("answerable", "generic", "public", "My SSO login says the identity provider certificate has expired"),
    ("answerable", "generic", "public", "How do I escalate my problem to a human support agent?"),
    ("answerable", "generic", "public", "What are the support response times for my plan?"),
    ("not_in_docs", "generic", "public", "Do you have an Android mobile app?"),
    ("not_in_docs", "generic", "public", "Can I host Quillstack on my own servers?"),
    ("not_in_docs", "generic", "public", "How do I set up a custom domain for my workspace?"),
    ("not_in_docs", "generic", "public", "Which data centre regions do you operate in?"),
    ("not_in_docs", "generic", "public", "Is there a Zapier integration?"),
    ("off_topic", "generic", "public", "what's the weather"),
    ("off_topic", "generic", "public", "write me a poem"),
    ("off_topic", "generic", "public", "who won the match"),
    ("off_topic", "generic", "public", "recommend a laptop"),
    ("off_topic", "generic", "public", "translate hello"),
    ("answerable", "S1", "pro", "I was charged $96 twice on 21 November. Can I get a refund for the duplicate charge?"),
    ("answerable", "S1", "pro", "What is the refund policy for my plan?"),
    ("answerable", "S2", "pro", "Did my refund go through, and when will I see it on my statement?"),
    ("answerable", "S2", "pro", "Can you tell me about the invoice ending 0412?"),
    ("answerable", "S3", "business", "I'm a member. Can I request a refund for our last charge?"),
    ("answerable", "S3", "business", "Why does my refund need to be approved by your team?"),
    ("answerable", "S3", "business", "Who on my account is allowed to ask for a refund?"),
    ("answerable", "S5", "pro", "My refund failed. Was my account charged or changed?"),
    ("not_in_docs", "S4", "pro", "Does Quillstack have a dark mode?"),
    ("off_topic", "S4", "pro", "Can you recommend a good restaurant nearby?"),
]


class Recorder:
    """What: stand-in embedder for the first pass. Use when: collecting the query texts search will embed.
    Inputs: none. Output: a fixed unit vector; records every text. Side effects: none (no network).
    Role: lets run_search tell us the exact normalized texts so they can be embedded in one batch."""

    def __init__(self):
        """What: start empty. Use when: construction. Inputs: none. Output: none. Side effects: none. Role: init."""
        self.texts: list[str] = []

    async def aembed_one(self, text, timeout_s):
        """What: record the text. Use when: search embeds a query. Inputs: text, timeout. Output: unit vector.
        Side effects: records. Role: fake."""
        self.texts.append(text)
        return [1.0] + [0.0] * (config.EMBEDDING_DIMENSIONS - 1)


class Cached:
    """What: embedder that serves precomputed vectors. Use when: the second pass. Inputs: text to vector map.
    Output: the vector for the text. Side effects: none. Role: the same vector the live path would have fetched."""

    def __init__(self, vectors: dict[str, list[float]]):
        """What: keep the map. Use when: construction. Inputs: vectors by text. Output: none. Side effects: none.
        Role: init."""
        self.vectors = vectors

    async def aembed_one(self, text, timeout_s):
        """What: look up the vector. Use when: search embeds a query. Inputs: text, timeout. Output: vector.
        Side effects: none. Role: fake."""
        return self.vectors[text]


async def run_all(db, embedder) -> list[dict]:
    """What: search every probe twice (record, then cached). Use when: main. Inputs: db, real embedder.
    Output: one row per probe. Side effects: one real embedding request. Role: see module docstring."""
    def req(tier, q):
        """What: build the request. Use when: a probe runs. Inputs: tier, query. Output: SearchRequest.
        Side effects: none. Role: calibration=true, k=5, verified for any real tier."""
        return SearchRequest(query=q, tier=tier, verified=tier != "public", k=5, calibration=True)

    rec = Recorder()
    for _, _, tier, q in PROBES:
        await search.run_search(req(tier, q), db, rec, "probe", "probe")
    texts = sorted(set(rec.texts))
    vecs = dict(zip(texts, embedder.embed_batch(texts)))
    rows = []
    for label, scenario, tier, q in PROBES:
        j = await search.run_search(req(tier, q), db, Cached(vecs), "probe", "probe")
        top = j.chunks[0] if j.chunks else None
        rows.append({"label": label, "scenario": scenario, "tier": tier, "query": q,
                     "top_cosine": j.confidence.top_cosine, "top_chunk": top.chunk_id if top else None})
    return rows


def thresholds(rows: list[dict]) -> dict:
    """What: tau_low and tau_high by doc 07 s4.1 steps 2 and 3, over the GENERIC probes only (D-053). Use when:
    after the probes. Inputs: rows. Output: dict with taus and overlap flag. Side effects: none. Role: tau_low =
    midpoint of (highest off_topic, lowest answerable), or the highest off_topic plus 0.01 when the ranges overlap;
    tau_high = inclusive lower quartile of answerable top cosines, at least tau_low. Scenario probes (S1 to S5) are
    checks and never move the numbers they are tested against."""
    gen = [r for r in rows if r["scenario"] == "generic"]
    ans = [r["top_cosine"] for r in gen if r["label"] == "answerable"]
    off = [r["top_cosine"] for r in gen if r["label"] == "off_topic"]
    low = (max(off) + min(ans)) / 2 if max(off) < min(ans) else max(off) + 0.01
    high = max(statistics.quantiles(ans, n=4, method="inclusive")[0], low)
    return {"overlap": max(off) >= min(ans), "tau_low": round(low, 3), "tau_high": round(high, 3)}


def add_margins(rows: list[dict], tau_low: float, tau_high: float) -> None:
    """What: record each scenario probe's margin to the bar it is tested against and flag thin ones. Use when: after
    thresholds. Inputs: rows (edited in place), tau_low, tau_high. Output: none. Side effects: adds `margin` and `risk`
    to scenario rows only. Role: D-053: answerable margin = top cosine minus tau_high; off_topic = tau_low minus
    top cosine; not_in_docs = tau_high minus top cosine; risk is True under 0.02 (inside run-to-run noise of
    about 0.0003 and thin for a recorded demo)."""
    for r in rows:
        if r["scenario"] == "generic":
            continue
        c = r["top_cosine"]
        r["margin"] = round({"answerable": c - tau_high, "off_topic": tau_low - c, "not_in_docs": tau_high - c}[r["label"]], 4)
        r["risk"] = r["margin"] < 0.02


def main() -> None:
    """What: run, then print JSON. Use when: script start. Inputs: none. Output: JSON on stdout. Side effects: see
    module docstring. Role: adds a verdict per probe and the scenario margins using the computed taus (cosine only: no code probes)."""
    db = Database(os.environ["KNOWLEDGE_DB_URL"])
    embedder = OpenAIEmbedder()
    rows = asyncio.run(run_all(db, embedder))
    t = thresholds(rows)
    add_margins(rows, t["tau_low"], t["tau_high"])
    for r in rows:
        c = r["top_cosine"]
        r["verdict"] = "none" if c < t["tau_low"] else ("weak" if c < t["tau_high"] else "pass")
    st = db.status() or {}
    print(json.dumps({"corpus_version": st.get("corpus_version"), "chunks": st.get("chunks"),
                      "embedding_model": config.embedding_model(), "k": 5, "embedding_requests": embedder.calls,
                      "run_at": datetime.datetime.now(datetime.UTC).isoformat(), **t, "rows": rows}, indent=1))


if __name__ == "__main__":
    main()
