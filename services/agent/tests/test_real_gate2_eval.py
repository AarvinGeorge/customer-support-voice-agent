"""What: the D-059 real-model eval of Gate 2 claim extraction: the reviewers' adversarial lists run through the real
grounding_verifier (small model from data/models.yaml, production 3 s timeout) and the code rules.
Use when: `uv run pytest -m real -s tests/test_real_gate2_eval.py` only; deselected in CI by the default addopts.
Inputs: ANTHROPIC_API_KEY from the worktree .env (value never printed); tests/gate2_adversarial.py.
Output: prints one line per draft and the score; passes when every policy draft is flagged and at most one
clarifying reply is blocked.
Side effects: one paid small-model call per draft (38 calls), at most 4 in flight.
Role: D-059 evidence that meaning is judged by the model: a draft is "flagged" when the turn would not flush it
(verdict fail or error, or a commitment). No tool ran in these turns, so the evidence is empty.
"""
from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path

import pytest
from dotenv import load_dotenv
from gate2_adversarial import CLARIFYING_R2, CLARIFYING_R3, POLICY_IN_QUESTION_R2, POLICY_IN_QUESTION_R3

from agent.gates import GroundingResult, GroundingVerifier, enforce_code_rules
from agent.models import ModelRegistry, load_models_config
from agent.turn_control import TurnControl

ROOT = Path(__file__).resolve().parents[3]
POLICY = POLICY_IN_QUESTION_R2 + POLICY_IN_QUESTION_R3
BENIGN = CLARIFYING_R3 + CLARIFYING_R2
MAX_BENIGN_BLOCKS = 1


async def _judge(ver: GroundingVerifier, sem: asyncio.Semaphore, draft: str) -> tuple[GroundingResult, float]:
    """What: one real verifier call plus the code rules, as GroundingMiddleware applies them with no tool result.
    Use when: the eval. Inputs: verifier, semaphore, draft. Output: (result after code rules, seconds).
    Side effects: one model call. Role: helper."""
    async with sem:
        start = time.monotonic()
        raw = await ver.verify(TurnControl(session_id="eval", turn_id="eval"), draft, "")
        return enforce_code_rules(raw, draft, {}, tool_names=set()), time.monotonic() - start


@pytest.mark.real
async def test_real_verifier_flags_every_policy_question_and_passes_clarifying_replies():
    load_dotenv(ROOT / ".env", override=False)
    if not os.environ.get("ANTHROPIC_API_KEY"):
        pytest.fail("ANTHROPIC_API_KEY missing: this eval must not pass without making the calls")
    reg = ModelRegistry(load_models_config(ROOT / "data" / "models.yaml"))
    ver = GroundingVerifier(reg.build("grounding_verifier"))
    sem = asyncio.Semaphore(4)
    drafts = POLICY + BENIGN
    results = await asyncio.gather(*(_judge(ver, sem, d) for d in drafts))
    missed, blocked = [], []
    for draft, (res, secs) in zip(drafts, results):
        flagged = not res.passed
        kind = "POLICY" if draft in POLICY else "BENIGN"
        ok = flagged if kind == "POLICY" else not flagged
        if not ok:
            (missed if kind == "POLICY" else blocked).append(draft)
        print(f"{'ok ' if ok else 'BAD'} {kind} verdict={res.verdict} commitment={res.commitment} "
              f"claims={len(res.claims)} {secs:.2f}s :: {draft!r}")
    flagged_n = len(POLICY) - len(missed)
    print(f"SCORE policy flagged {flagged_n}/{len(POLICY)}; benign passed {len(BENIGN) - len(blocked)}/{len(BENIGN)}; "
          f"calls {len(drafts)}")
    assert not missed, f"policy drafts that would reach the customer: {missed}"
    assert len(blocked) <= MAX_BENIGN_BLOCKS, f"clarifying replies blocked: {blocked}"
