"""What: run the Gate 1 probe set through POST /v1/search with calibration=true and print each query's top cosine, then
the suggested tau_low and tau_high by the doc 07 s4.1 method. Use when: setting or re-checking provisional thresholds
(spike 14). Inputs: env KNOWLEDGE_URL (default http://127.0.0.1:8000) and KNOWLEDGE_TOKEN_AGENT; run it inside the
knowledge container: `docker compose exec -T knowledge python - < scripts/probe_retrieval.py`. Output: JSON on stdout.
Side effects: 20 search calls, each one real embedding call. Role: replaced by evals/calibrate_retrieval.py (golden set)."""
import json
import os
import statistics
import urllib.request

URL = os.environ.get("KNOWLEDGE_URL", "http://127.0.0.1:8000")
TOKEN = os.environ["KNOWLEDGE_TOKEN_AGENT"]
PROBES = [
    ("answerable", "How do I request a refund for a duplicate charge?"),
    ("answerable", "How long does a refund take to reach my card?"),
    ("answerable", "How do I change the billing email on my account?"),
    ("answerable", "Who can update billing contact details?"),
    ("answerable", "What can a member do compared with an admin?"),
    ("answerable", "How do I make someone an admin on my team?"),
    ("answerable", "Single sign-on login fails with an invalid assertion, what should I check?"),
    ("answerable", "My SSO login says the identity provider certificate has expired"),
    ("answerable", "How do I escalate my problem to a human support agent?"),
    ("answerable", "What are the support response times for my plan?"),
    ("not_in_docs", "Do you have an Android mobile app?"),
    ("not_in_docs", "Can I host Quillstack on my own servers?"),
    ("not_in_docs", "How do I set up a custom domain for my workspace?"),
    ("not_in_docs", "Which data centre regions do you operate in?"),
    ("not_in_docs", "Is there a Zapier integration?"),
    ("off_topic", "what's the weather"),
    ("off_topic", "write me a poem"),
    ("off_topic", "who won the match"),
    ("off_topic", "recommend a laptop"),
    ("off_topic", "translate hello"),
]


def search(query: str) -> dict:
    """What: one search call. Use when: probing. Inputs: query text. Output: the parsed response. Side effects: one
    HTTP call and one embedding call. Role: calibration=true switches gating off so every score is visible."""
    req = urllib.request.Request(
        URL + "/v1/search", method="POST", headers={"Authorization": "Bearer " + TOKEN, "Content-Type": "application/json"},
        data=json.dumps({"query": query, "tier": "public", "verified": False, "k": 5, "calibration": True}).encode())
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.load(r)


def main() -> None:
    """What: run the probes and print scores and thresholds. Use when: script start. Inputs: none. Output: JSON.
    Side effects: see module docstring. Role: tau_low = midpoint of (highest off_topic, lowest answerable), or the
    highest off_topic plus 0.01 when the ranges overlap; tau_high = lower quartile of answerable top cosines, at
    least tau_low (doc 07 s4.1 steps 2 and 3)."""
    rows = []
    for label, q in PROBES:
        j = search(q)
        top = j["chunks"][0] if j["chunks"] else None
        rows.append({"label": label, "query": q, "top_cosine": j["confidence"]["top_cosine"],
                     "top_chunk": top["chunk_id"] if top else None, "verdict": j["confidence"]["verdict"]})
    ans = [r["top_cosine"] for r in rows if r["label"] == "answerable"]
    off = [r["top_cosine"] for r in rows if r["label"] == "off_topic"]
    low = (max(off) + min(ans)) / 2 if max(off) < min(ans) else max(off) + 0.01
    high = max(statistics.quantiles(ans, n=4, method="inclusive")[0], low)
    print(json.dumps({"rows": rows, "overlap": max(off) >= min(ans), "tau_low": round(low, 3), "tau_high": round(high, 3)}, indent=1))


main()
