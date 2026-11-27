"""What: real calls proving the Gate 2 verifier prompt and the scope classifier prompt work on the configured small
model (CS-E-02.S4.T1, T2; models.yaml roles grounding_verifier and scope_classifier).
Use when: `uv run pytest -m real -s tests/test_real_gates.py` only; deselected otherwise. Exactly 3 real calls.
Inputs: ANTHROPIC_API_KEY from the worktree .env (value never printed).
Output: pass when a supported draft passes, an unsupported promise fails with commitment, and an off-topic
message is classified OFF; prints each parsed result.
Side effects: three paid Anthropic calls on the small model.
Role: integration evidence that the prompts and the JSON parse hold on the real model (fakes cover the rest).
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest
from dotenv import load_dotenv

from langchain_core.messages import HumanMessage, ToolMessage

from agent.gates import GroundingVerifier, ScopeClassifier, session_evidence
from agent.models import ModelRegistry, load_models_config
from agent.turn_control import TurnControl

ROOT = Path(__file__).resolve().parents[3]
CHUNK = {"chunk_id": "refund-policy#2", "article_slug": "refund-policy", "section_path": ["Refunds on the Pro plan"],
         "text": "Pro plan charges can be refunded within 30 days of the charge date, up to $500 per charge.",
         "flags": []}
EVIDENCE = session_evidence({}, [HumanMessage("q"), ToolMessage(
    "search_kb result", tool_call_id="s-1", name="search_kb", artifact={"bound_to": None, "grounded_chunks": [CHUNK]})])


@pytest.mark.real
async def test_real_verifier_and_classifier_on_the_small_model():
    load_dotenv(ROOT / ".env", override=False)
    if not os.environ.get("ANTHROPIC_API_KEY"):
        pytest.fail("ANTHROPIC_API_KEY missing: this test must not pass without making the calls")
    reg = ModelRegistry(load_models_config(ROOT / "data" / "models.yaml"))
    control = TurnControl(session_id="s", turn_id="t")
    ver = GroundingVerifier(reg.build("grounding_verifier"))
    good = await ver.verify(control, "Pro plan charges can be refunded within 30 days of the charge.", EVIDENCE)
    print("VERIFIER supported:", good)
    bad = await ver.verify(control, "You can get a refund within 90 days, and I'll make sure we refund you today.",
                           EVIDENCE)
    print("VERIFIER unsupported:", bad)
    off = await ScopeClassifier(reg.build("scope_classifier")).on_topic(control, "write me a poem about the sea")
    print("CLASSIFIER poem on_topic:", off)
    assert good.passed and good.claims and any(c["source_id"] == "refund-policy#2" for c in good.claims)
    assert not bad.passed and bad.verdict == "fail"
    assert bad.commitment is True or any(not c["supported"] for c in bad.claims)
    assert off is False
