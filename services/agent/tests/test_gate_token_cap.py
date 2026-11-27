"""What: tests for the gate models' thinking setting and token cap (fix after browser pass 2, 11:43: the Haiku
verifier spent its whole 512-token cap on reasoning, returned no verdict and failed closed on ordinary answers).
Use when: pytest (fakes and real LangChain clients with fake keys; no network).
Inputs: data/models.yaml. Output: pass/fail. Side effects: fake env keys.
Role: a truncated verifier reply is its own outcome ("truncated"): still fail closed, logged and shown in the
drawer step detail, never read as an unsupported claim; every gate model (and any gate fallback) is built with
thinking off and a token cap that fits its reply with margin.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import pytest
from langchain_core.messages import AIMessage, HumanMessage
from test_gate2 import DRAFT, Harness, grounding_gate

from agent.gates import (
    CLARIFY_LINE,
    GroundingVerifier,
    ScopeClassifier,
    reply_truncated,
)
from agent.models.config import GATE_REPLY_MIN_TOKENS, gate_roles, load_models_config
from agent.models.registry import ModelRegistry
from agent.turn_control import TurnControl

REPO_YAML = Path(__file__).resolve().parents[3] / "data" / "models.yaml"


class ScriptedModel:
    """What: a chat model fake whose ainvoke returns one fixed AIMessage. Use when: the verifier and classifier.
    Inputs: the reply. Output: ScriptedModel. Side effects: counts calls. Role: fake."""

    model = "fake-gate"

    def __init__(self, reply: AIMessage) -> None:
        """What: keep the reply. Use when: tests. Inputs: reply. Output: none. Side effects: none. Role: constructor."""
        self.reply, self.calls = reply, 0

    async def ainvoke(self, messages: list[Any], config: Any = None) -> AIMessage:
        """What: return the reply. Use when: a gate calls the model. Inputs: messages, config. Output: AIMessage.
        Side effects: counts. Role: fake call."""
        self.calls += 1
        return self.reply


def truncated(text: str = "", key: str = "stop_reason") -> AIMessage:
    """What: a reply cut by max_tokens, as ChatAnthropic reports it (stop_reason, or the shared finish_reason).
    Use when: tests. Inputs: visible text, metadata key. Output: AIMessage. Side effects: none. Role: fixture."""
    return AIMessage(text, response_metadata={key: "max_tokens", "model_name": "fake-gate"},
                     usage_metadata={"input_tokens": 900, "output_tokens": 512, "total_tokens": 1412,
                                     "output_token_details": {"reasoning": 512}})


PASS_JSON = json.dumps({"claims": [{"text": "Pro refunds are allowed within 30 days", "supported": True,
                                    "source_id": "refund-policy#2"}], "commitment": False})


@pytest.mark.parametrize("key", ["stop_reason", "finish_reason"])
def test_reply_truncated_reads_either_metadata_key(key: str):
    assert reply_truncated(truncated(key=key))
    assert not reply_truncated(AIMessage("{}", response_metadata={key: "end_turn"}))
    assert not reply_truncated(AIMessage("{}"))


async def test_a_truncated_verifier_reply_is_its_own_outcome_and_fails_closed(caplog: pytest.LogCaptureFixture):
    ver = GroundingVerifier(ScriptedModel(truncated()), timeout_s=1.0, min_s=0.0)
    control = TurnControl(session_id="s", turn_id="t")
    with caplog.at_level(logging.WARNING, logger="agent.gates"):
        res = await ver.verify(control, DRAFT, "<evidence/>")
    assert (res.verdict, res.reason, res.passed, res.claims) == ("error", "truncated", False, [])
    assert any("truncated" in r.getMessage() and "max_tokens" in r.getMessage() for r in caplog.records)
    assert control.gate_calls and control.gate_calls[0][1]["output_tokens"] == 512  # still counted in usage


async def test_a_truncated_reply_never_passes_even_when_its_text_parses():
    ver = GroundingVerifier(ScriptedModel(truncated(PASS_JSON)), timeout_s=1.0, min_s=0.0)
    res = await ver.verify(TurnControl(session_id="s", turn_id="t"), DRAFT, "<evidence/>")
    assert (res.verdict, res.reason) == ("error", "truncated")


async def test_other_verifier_errors_carry_their_own_reason():
    unreadable = GroundingVerifier(ScriptedModel(AIMessage("no json", response_metadata={"stop_reason": "end_turn"})),
                                   timeout_s=1.0, min_s=0.0)
    res = await unreadable.verify(TurnControl(session_id="s", turn_id="t"), DRAFT, "")
    assert (res.verdict, res.reason) == ("error", "unparseable")
    ok = GroundingVerifier(ScriptedModel(AIMessage(PASS_JSON, response_metadata={"stop_reason": "end_turn"})),
                           timeout_s=1.0, min_s=0.0)
    res = await ok.verify(TurnControl(session_id="s", turn_id="t"), DRAFT, "")
    assert (res.verdict, res.reason) == ("pass", None)


async def test_the_drawer_shows_why_a_truncated_check_failed_and_the_turn_still_clarifies():
    h = Harness(GroundingVerifier(ScriptedModel(truncated()), timeout_s=1.0, min_s=0.0))
    outcome, events, _ = await h.run()
    g = grounding_gate(events)
    assert (g.verdict, g.decision) == ("error", "clarify")
    assert outcome.final.text == CLARIFY_LINE
    [done] = [e for e in events if e.type == "step.completed" and e.step_id == g.step_id]
    assert done.status == "error"
    assert done.detail and "token cap" in done.detail and len(done.detail) <= 80
    assert done.summary.get("reason") == "truncated"
    assert g.claims_total == 0  # not shown as an unsupported claim


async def test_a_truncated_scope_reply_counts_as_on_topic_and_is_logged(caplog: pytest.LogCaptureFixture):
    clf = ScopeClassifier(ScriptedModel(truncated()), timeout_s=1.0, min_s=0.0)
    with caplog.at_level(logging.WARNING, logger="agent.gates"):
        assert await clf.on_topic(TurnControl(session_id="s", turn_id="t"), "what's the weather?") is True
    assert any("truncated" in r.getMessage() for r in caplog.records)


def test_gate_roles_include_every_gate_fallback_in_models_yaml():
    cfg = load_models_config(REPO_YAML)
    roles = gate_roles(cfg)
    assert {"grounding_verifier", "scope_classifier"} <= set(roles)
    assert all(r.split("_fallback")[0] in GATE_REPLY_MIN_TOKENS for r in roles)


def _payload(monkeypatch: pytest.MonkeyPatch, registry: ModelRegistry, role: str) -> dict[str, Any]:
    """What: the request body the real client would send for a role (no network). Use when: these tests.
    Inputs: monkeypatch, registry, role. Output: payload dict. Side effects: fake env keys. Role: helper."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-not-real")
    monkeypatch.setenv("OPENAI_API_KEY", "test-not-real")
    model = registry.build(role)
    return model._get_request_payload([HumanMessage("hi")])


def test_every_gate_request_has_thinking_off_and_a_cap_that_fits_the_reply(monkeypatch: pytest.MonkeyPatch):
    cfg = load_models_config(REPO_YAML)
    registry = ModelRegistry(cfg)
    for role in gate_roles(cfg):
        spec = cfg.roles[role]
        body = _payload(monkeypatch, registry, role)
        if spec.provider == "anthropic":
            assert body.get("thinking") == {"type": "disabled"}, role
            assert (body.get("output_config") or {}).get("effort") in (None, "low", "medium", "high"), role
        else:
            assert body.get("reasoning_effort") == "none", role
        cap = body.get("max_tokens") or body.get("max_completion_tokens")
        need = GATE_REPLY_MIN_TOKENS[role.split("_fallback")[0]]
        assert cap is not None and cap >= 2 * need, (role, cap, need)


def test_the_verifier_minimum_covers_a_long_verdict_with_margin():
    # Eight claims of 200 characters each is a long support draft; about 4 characters per token of JSON.
    reply = json.dumps({"claims": [{"text": "x" * 200, "supported": True, "source_id": "refund-policy#pro-plan-refunds"}
                                   for _ in range(8)], "commitment": False})
    assert GATE_REPLY_MIN_TOKENS["grounding_verifier"] >= len(reply) // 4


def _yaml_with(**over: Any) -> dict[str, Any]:
    """What: models.yaml as data with grounding_verifier fields overridden. Use when: validator tests.
    Inputs: field overrides. Output: dict. Side effects: reads the file. Role: helper."""
    import yaml

    data = yaml.safe_load(REPO_YAML.read_text())
    data["roles"]["grounding_verifier"].update(over)
    return data


@pytest.mark.parametrize("over", [{"thinking": None}, {"thinking": "adaptive"}, {"max_tokens": 512},
                                  {"effort": "high", "thinking": "disabled", "max_tokens": 64}],
                         ids=["no-thinking-key", "adaptive", "cap-512", "cap-64"])
def test_config_refuses_a_gate_role_with_thinking_on_or_a_small_cap(over: dict[str, Any]):
    from agent.models.config import ModelsConfig

    with pytest.raises(ValueError):
        ModelsConfig.model_validate(_yaml_with(**over))


def test_config_applies_the_gate_rules_to_a_gate_fallback_role():
    from agent.models.config import ModelsConfig

    data = _yaml_with()
    data["roles"]["grounding_verifier_fallback"] = {**data["roles"]["grounding_verifier"], "thinking": "adaptive"}
    with pytest.raises(ValueError, match="grounding_verifier_fallback"):
        ModelsConfig.model_validate(data)
    data["roles"]["grounding_verifier_fallback"]["thinking"] = "disabled"
    assert "grounding_verifier_fallback" in gate_roles(ModelsConfig.model_validate(data))


# --- Review PR #68 I-2: the thinking rule is per model (claude-api skill thinking table) -------------------------
def test_disabled_thinking_is_refused_on_a_model_that_rejects_it_in_any_role():
    from agent.models.config import ModelsConfig

    data = _yaml_with()
    data["roles"]["agent"]["thinking"] = "disabled"  # claude-sonnet-5-5: disabled returns a 400
    with pytest.raises(ValueError, match="agent"):
        ModelsConfig.model_validate(data)


@pytest.mark.parametrize(("model", "over", "accepted"), [
    ("claude-sonnet-5-5", {"thinking": "disabled", "max_tokens": 1536}, False),     # 400 at the API
    ("claude-sonnet-5-5", {"thinking": None, "effort": "low", "max_tokens": 1536}, False),  # no room to think
    ("claude-sonnet-5-5", {"thinking": None, "effort": "low", "max_tokens": 6144}, True),
    ("claude-sonnet-5-5", {"thinking": "adaptive", "effort": "medium", "max_tokens": 6144}, False),  # effort low only
    ("claude-opus-5-5", {"thinking": "adaptive", "effort": "low", "max_tokens": 8192}, True),
    ("claude-haiku-5-5", {"thinking": None, "effort": "low", "max_tokens": 8192}, False),  # disabled is supported
], ids=["sonnet-disabled", "sonnet-small-cap", "sonnet-room", "sonnet-medium", "opus-room", "haiku-adaptive"])
def test_a_gate_role_on_a_model_that_cannot_turn_thinking_off_needs_effort_low_and_room(
        model: str, over: dict[str, Any], accepted: bool):
    from agent.models.config import ModelsConfig

    data = _yaml_with(model=model, **over)
    if accepted:
        ModelsConfig.model_validate(data)
    else:
        with pytest.raises(ValueError, match="grounding_verifier"):
            ModelsConfig.model_validate(data)
