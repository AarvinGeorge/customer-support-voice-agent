"""What: tests for the token rule on every role that returns a structured verdict (root-cause audit 2026-11-27 open
item 5): the gate roles and the offline `judge`. On an OpenAI reasoning model, reasoning tokens count against the
output cap and the default effort is medium (developers.openai.com/api/docs/guides/reasoning, "Allocating space for
reasoning"; API reference reasoning_effort, default "medium"), so a judge with no effort key and a 1024 cap can end
with no verdict, the same fault as the verifier in browser pass 2.
Use when: pytest (no network). Inputs: data/models.yaml. Output: pass/fail. Side effects: fake env keys only.
Role: the judge cannot be configured into the token-cap fault."""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml
from langchain_core.messages import HumanMessage

from agent.models.config import (
    OPENAI_REASONING_RESERVE_TOKENS,
    VERDICT_REPLY_MIN_TOKENS,
    ModelsConfig,
    load_models_config,
    verdict_roles,
)
from agent.models.registry import ModelRegistry

REPO_YAML = Path(__file__).resolve().parents[3] / "data" / "models.yaml"


def _with_judge(**over: Any) -> dict[str, Any]:
    """What: models.yaml as data with judge fields overridden (None removes the key). Use when: validator tests.
    Inputs: overrides. Output: dict. Side effects: reads the file. Role: helper."""
    data = yaml.safe_load(REPO_YAML.read_text())
    judge = data["roles"]["judge"]
    for k, v in over.items():
        if v is None:
            judge.pop(k, None)
        else:
            judge[k] = v
    return data


def test_verdict_roles_cover_the_gates_and_the_judge():
    roles = verdict_roles(load_models_config(REPO_YAML))
    assert {"grounding_verifier", "scope_classifier", "judge"} <= set(roles)
    assert set(VERDICT_REPLY_MIN_TOKENS) >= {"grounding_verifier", "scope_classifier", "judge"}


def test_the_openai_reserve_is_the_documented_figure():
    assert OPENAI_REASONING_RESERVE_TOKENS == 25000  # "reserving at least 25,000 tokens for reasoning and outputs"


@pytest.mark.parametrize(("over", "accepted"), [
    ({"effort": None, "max_tokens": 1024}, False),   # main today: default effort medium, cap holds reasoning too
    ({"effort": None, "max_tokens": 30000}, False),  # effort must be explicit: the default is the model's choice
    ({"effort": "low", "max_tokens": 1024}, False),  # reasoning on, no reserve
    ({"effort": "low", "max_tokens": 24999}, False),
    ({"effort": "low", "max_tokens": 25000}, True),
    ({"effort": "none", "max_tokens": 1024}, True),  # no reasoning: 2x the verdict is enough
    ({"effort": "none", "max_tokens": 512}, False),
    ({"effort": "low", "max_tokens": None}, False),
], ids=["main", "no-effort-big-cap", "low-1024", "low-just-under", "low-reserve", "none-1024", "none-512", "no-cap"])
def test_config_applies_the_verdict_token_rule_to_the_judge(over: dict[str, Any], accepted: bool):
    data = _with_judge(**over)
    if accepted:
        ModelsConfig.model_validate(data)
    else:
        with pytest.raises(ValueError, match="judge"):
            ModelsConfig.model_validate(data)


def test_a_judge_fallback_gets_the_same_rule():
    data = _with_judge()
    data["roles"]["judge_fallback"] = {**data["roles"]["judge"], "max_tokens": 1024}
    data["roles"]["judge_fallback"].pop("effort", None)
    with pytest.raises(ValueError, match="judge_fallback"):
        ModelsConfig.model_validate(data)


def test_the_judge_request_sends_an_explicit_effort_and_a_cap_that_holds_reasoning(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-not-real")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-not-real")
    cfg = load_models_config(REPO_YAML)
    spec = cfg.roles["judge"]
    body = ModelRegistry(cfg).build("judge")._get_request_payload([HumanMessage("hi")])
    effort = body.get("reasoning_effort")
    cap = body.get("max_completion_tokens") or body.get("max_tokens")
    assert spec.provider == "openai" and effort == spec.effort and effort is not None
    need = 2 * VERDICT_REPLY_MIN_TOKENS["judge"] if effort == "none" else OPENAI_REASONING_RESERVE_TOKENS
    assert cap is not None and cap >= need, (cap, need)
