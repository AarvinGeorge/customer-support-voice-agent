"""What: tests for the usage normalizer and price math. Use when: pytest. Inputs: none. Output: pass/fail. Side effects: none. Role: test."""
import pytest

from agent.models.config import Price
from contracts.events import Usage

from agent.models.usage import add_usage, cost_usd_est, normalize_usage, token_usage

PRICE = Price(input_per_mtok=2.0, output_per_mtok=10.0, cache_read_per_mtok=0.1,
              cache_write_per_mtok=2.5, as_of="2026-11-27")


def test_langchain_shape_anthropic_cache_fields():
    # langchain-anthropic: input_tokens already includes cache read and creation.
    u = normalize_usage({"input_tokens": 1000, "output_tokens": 50, "total_tokens": 1050,
                         "input_token_details": {"cache_read": 700, "cache_creation": 200}})
    assert u == token_usage(input_tokens=1000, output_tokens=50, cache_read_tokens=700, cache_write_tokens=200)


def test_langchain_shape_openai_cached_tokens():
    u = normalize_usage({"input_tokens": 400, "output_tokens": 20,
                         "input_token_details": {"cache_read": 128}})
    assert u == token_usage(input_tokens=400, output_tokens=20, cache_read_tokens=128, cache_write_tokens=0)


def test_raw_anthropic_response_usage():
    # Raw Anthropic: input_tokens EXCLUDES cached; the normalizer adds them back.
    u = normalize_usage({"input_tokens": 100, "output_tokens": 5,
                         "cache_read_input_tokens": 700, "cache_creation_input_tokens": 200},
                        provider="anthropic")
    assert u == token_usage(input_tokens=1000, output_tokens=5, cache_read_tokens=700, cache_write_tokens=200)


def test_raw_openai_usage_details():
    u = normalize_usage({"prompt_tokens": 400, "completion_tokens": 20,
                         "prompt_tokens_details": {"cached_tokens": 128}}, provider="openai")
    assert u == token_usage(input_tokens=400, output_tokens=20, cache_read_tokens=128, cache_write_tokens=0)


@pytest.mark.parametrize("bad", [None, {}, {"foo": 1}])
def test_missing_or_unknown_usage_is_zero_not_error(bad):
    assert normalize_usage(bad) == token_usage(0, 0, 0, 0)


def test_normalized_usage_is_the_contracts_model():
    u = normalize_usage({"input_tokens": 3, "output_tokens": 1})
    assert type(u) is Usage
    assert (u.model_calls, u.tool_calls, u.latency_ms, u.cost_usd_est) == (0, 0, 0, 0.0)
    assert u.models_used == [] and u.fallback_used is False


def test_usage_adds():
    a, b = token_usage(10, 2, 4, 1), token_usage(5, 1, 0, 0)
    assert add_usage(a, b) == token_usage(15, 3, 4, 1)


def test_add_usage_sums_counts_and_merges_models_in_order():
    a = token_usage(1, 1, 0, 0).model_copy(update={"model_calls": 1, "models_used": ["m1"],
                                                    "cost_usd_est": 0.5, "latency_ms": 10})
    b = token_usage(1, 1, 0, 0).model_copy(update={"model_calls": 1, "tool_calls": 2,
                                                    "models_used": ["m2", "m1"], "fallback_used": True})
    s = add_usage(a, b)
    assert (s.model_calls, s.tool_calls, s.latency_ms) == (2, 2, 10)
    assert s.models_used == ["m1", "m2"] and s.fallback_used is True
    assert s.cost_usd_est == pytest.approx(0.5)


def test_cost_splits_cached_from_fresh_input():
    # fresh = 1000 - 700 - 200 = 100
    u = token_usage(1000, 50, 700, 200)
    expected = (100 * 2.0 + 700 * 0.1 + 200 * 2.5 + 50 * 10.0) / 1e6
    assert cost_usd_est(u, PRICE) == pytest.approx(expected)


def test_cost_without_cache_prices_bills_cache_at_input_rate():
    p = Price(input_per_mtok=1.0, output_per_mtok=2.0, as_of="2026-11-27")
    assert cost_usd_est(token_usage(1000, 100, 600, 0), p) == pytest.approx((1000 * 1.0 + 100 * 2.0) / 1e6)


def test_cost_never_negative_when_cache_exceeds_input():
    assert cost_usd_est(token_usage(10, 0, 50, 0), PRICE) >= 0


def test_langchain_anthropic_ttl_breakdown_counts_as_cache_write():
    # Measured in the T2 real run: langchain-anthropic 1.7.13 sets cache_creation to 0 and moves the
    # tokens to ephemeral_5m_input_tokens / ephemeral_1h_input_tokens when the TTL breakdown exists.
    u = normalize_usage({"input_tokens": 1432, "output_tokens": 68, "total_tokens": 1500,
                         "input_token_details": {"cache_read": 0, "cache_creation": 0,
                                                 "ephemeral_5m_input_tokens": 1428,
                                                 "ephemeral_1h_input_tokens": 0}})
    assert u == token_usage(1432, 68, 0, 1428)
    u2 = normalize_usage({"input_tokens": 1560, "output_tokens": 528,
                          "input_token_details": {"cache_read": 1428, "cache_creation": 0,
                                                  "ephemeral_5m_input_tokens": 128,
                                                  "ephemeral_1h_input_tokens": None}})
    assert u2 == token_usage(1560, 528, 1428, 128)
