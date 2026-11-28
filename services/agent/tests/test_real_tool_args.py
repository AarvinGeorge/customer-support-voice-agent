"""What: the CS-E-02.F15 real-model check of the Gate 2 verifier on tool call args: the S1 follow-up draft "one charge
ending 0412 in the last 14 days" against a list_charges result whose call passed since_days 14 (supported, the
rca-pin-overmask.md finding 1 case), and the positive control: a call that looked back since_days 7 cannot support
"the only charge ending 0412 in the last 30 days" (30 was never passed, and the result cannot show days 8 to 30).
A control without since_days is not used: the result alone (one charge on Nov 21, default window 90 days) makes a
14-day claim derivable, and the real verifier supported it (first run, report tool-args-evidence.md).
Use when: `uv run pytest -m real -s -o addopts="" tests/test_real_tool_args.py` (or `-k control`) only; deselected in
CI by the default addopts.
Inputs: ANTHROPIC_API_KEY from the repo .env (value never printed). Output: one line per case; pass/fail.
Side effects: one paid small-model call per case (grounding_verifier in data/models.yaml).
Role: real-model proof that the verifier reads <query_args> as the query scope and nothing more.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest
from contracts.stubs import examples as ex
from dotenv import load_dotenv
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from agent.gates import GroundingResult, GroundingVerifier, enforce_code_rules, session_evidence
from agent.models import ModelRegistry, load_models_config
from agent.turn_control import TurnControl
from agent.untrusted import wrap_result

ROOT = Path(__file__).resolve().parents[3]
VERIFIED = {"verified": True, "claims_snapshot": ex.CLAIMS.model_dump(mode="json"),
            "context_snapshot": ex.ME_CONTEXT.model_dump(mode="json")}
KEY = f"{ex.CLAIMS.user_id}|{ex.ACCOUNT_ID}"
DRAFT = ("I checked your charges: in the last 14 days there is one charge ending 0412, $96.00 on November 21 for "
         "Quillstack Pro, 12 seats.")


def _turn(args: dict[str, Any]) -> list[Any]:
    """What: the invoice-ending-0412 turn: question, list_charges call with args, its result (one charge).
    Use when: these checks. Inputs: the call's args. Output: messages. Side effects: none. Role: fixture."""
    result = {"charges": [ex.CHARGES[0].model_dump(mode="json")]}
    return [HumanMessage("Can you tell me about the invoice ending 0412?"),
            AIMessage("", tool_calls=[{"name": "list_charges", "args": args, "id": "c1", "type": "tool_call"}]),
            ToolMessage(wrap_result("list_charges", result), tool_call_id="c1", name="list_charges",
                        artifact={"bound_to": KEY})]


CONTROL_DRAFT = "That is the only charge ending 0412 on your account in the last 30 days: $96.00 on November 21."


async def _judge(args: dict[str, Any], draft: str = DRAFT) -> GroundingResult:
    """What: one real verifier call plus the code rules on a draft; prints the verdict and claims.
    Use when: each check. Inputs: the call's args, the draft. Output: the result after the code rules. Side effects: one model call.
    Role: helper."""
    load_dotenv(ROOT / ".env", override=False)
    if not os.environ.get("ANTHROPIC_API_KEY"):
        pytest.fail("ANTHROPIC_API_KEY missing: this check must not pass without making the call")
    ver = GroundingVerifier(ModelRegistry(load_models_config(ROOT / "data" / "models.yaml")).build("grounding_verifier"))
    ev = session_evidence(VERIFIED, _turn(args))
    raw = await ver.verify(TurnControl(session_id="eval", turn_id="eval"), draft, ev)
    res = enforce_code_rules(raw, draft, ev.chunks, tool_names=ev.ids)
    print(f"args={args} verdict={res.verdict} reason={raw.reason} claims={res.claims}")
    return res


@pytest.mark.real
async def test_real_since_days_in_the_call_supports_the_14_day_scope():
    assert (await _judge({"ending": "0412", "since_days": 14})).passed


@pytest.mark.real
async def test_real_control_a_scope_never_passed_stays_unsupported():
    res = await _judge({"ending": "0412", "since_days": 7}, CONTROL_DRAFT)
    assert res.verdict == "fail"  # judged, not an error or timeout (a failed call must not pass this control)
    assert any(not c["supported"] for c in res.claims)
