"""What: tests for the model registry. Use when: pytest. Inputs: none. Output: pass/fail. Side effects: fake env keys. Role: test."""
from pathlib import Path

import pytest

from agent.models import registry as reg
from agent.models.config import load_models_config
from agent.models.registry import ModelRegistry
from agent.models.usage import token_usage

REPO_YAML = Path(__file__).resolve().parents[3] / "data" / "models.yaml"


@pytest.fixture
def registry():
    return ModelRegistry(load_models_config(REPO_YAML))


def test_build_passes_retries_zero_timeout_and_provider(monkeypatch, registry):
    seen = {}

    def fake_init(model, **kw):
        seen[model] = kw
        return object()

    monkeypatch.setattr(reg, "init_chat_model", fake_init)
    registry.build("agent")
    registry.build("agent_fallback")
    registry.build("scope_classifier")
    a = seen["claude-sonnet-5-5"]
    assert a["model_provider"] == "anthropic" and a["max_retries"] == 0 and a["timeout"] == 10
    assert seen[registry.config.roles["agent_fallback"].model]["model_provider"] == "openai"
    assert seen["claude-haiku-5-5"]["timeout"] == 2


def test_effort_maps_to_provider_kwarg(monkeypatch):
    cfg = load_models_config(REPO_YAML)
    cfg.roles["agent"].effort = "low"
    cfg.roles["agent_fallback"].effort = "low"
    seen = {}
    monkeypatch.setattr(reg, "init_chat_model", lambda model, **kw: seen.setdefault(model, kw))
    r = ModelRegistry(cfg)
    r.build("agent")
    r.build("agent_fallback")
    assert seen["claude-sonnet-5-5"]["effort"] == "low"
    assert seen[cfg.roles["agent_fallback"].model]["reasoning_effort"] == "low"


def test_real_clients_carry_zero_retries_and_timeout(monkeypatch, registry):
    # Real LangChain classes, fake keys, no network call is made.
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-not-real")
    monkeypatch.setenv("OPENAI_API_KEY", "test-not-real")
    a = registry.build("agent")
    f = registry.build("agent_fallback")
    assert a.max_retries == 0 and a.default_request_timeout == 10
    assert f.max_retries == 0 and f.request_timeout == 10


def test_unknown_role_and_non_chat_role_fail(registry):
    with pytest.raises(KeyError):
        registry.build("nope")
    with pytest.raises(ValueError):
        registry.build("embeddings")
    with pytest.raises(ValueError):
        registry.build("voice")


def test_cost_for_role_uses_role_price(registry):
    price = registry.config.roles["agent"].price
    u = token_usage(input_tokens=1_000_000, output_tokens=1_000_000, cache_read_tokens=0, cache_write_tokens=0)
    assert registry.cost("agent", u) == pytest.approx(price.input_per_mtok + price.output_per_mtok)


def test_usage_from_message_reads_usage_metadata(registry):
    from langchain_core.messages import AIMessage
    m = AIMessage(content="x", usage_metadata={"input_tokens": 10, "output_tokens": 2, "total_tokens": 12,
                                               "input_token_details": {"cache_read": 4}})
    assert registry.usage_of(m) == token_usage(10, 2, 4, 0)


def test_openai_tool_roles_send_reasoning_effort_none(monkeypatch, registry):
    # Live bug (wiring part 1, llm_429 run): OpenAI chat completions answers 400 "Function tools with
    # reasoning_effort are not supported for gpt-5.4-mini ... set reasoning_effort to 'none'" when the
    # fallback carries the agent tools. The fallback is the only OpenAI role that gets tools.
    seen = {}
    monkeypatch.setattr(reg, "init_chat_model", lambda model, **kw: seen.setdefault(model, kw))
    registry.build("agent_fallback")
    assert seen[registry.config.roles["agent_fallback"].model].get("reasoning_effort") == "none"
