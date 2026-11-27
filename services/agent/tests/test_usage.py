"""What: tests for the usage normalizer and price math. Use when: pytest. Inputs: none. Output: pass/fail. Side effects: none. Role: test."""
import pytest

from agent.models.config import Price
from agent.models.usage import Usage, cost_usd_est, normalize_usage

PRICE = Price(input_per_mtok=2.0, output_per_mtok=10.0, cache_read_per_mtok=0.1,
              cache_write_per_mtok=2.5, as_of="2026-11-27")


def test_langchain_shape_anthropic_cache_fields():
    # langchain-anthropic: input_tokens already includes cache read and creation.
    u = normalize_usage({"input_tokens": 1000, "output_tokens": 50, "total_tokens": 1050,
                         "input_token_details": {"cache_read": 700, "cache_creation": 200}})
    assert u == Usage(input_tokens=1000, output_tokens=50, cache_read_tokens=700, cache_write_tokens=200)


def test_langchain_shape_openai_cached_tokens():
    u = normalize_usage({"input_tokens": 400, "output_tokens": 20,
                         "input_token_details": {"cache_read": 128}})
    assert u == Usage(input_tokens=400, output_tokens=20, cache_read_tokens=128, cache_write_tokens=0)


def test_raw_anthropic_response_usage():
    # Raw Anthropic: input_tokens EXCLUDES cached; the normalizer adds them back.
    u = normalize_usage({"input_tokens": 100, "output_tokens": 5,
                         "cache_read_input_tokens": 700, "cache_creation_input_tokens": 200},
                        provider="anthropic")
    assert u == Usage(input_tokens=1000, output_tokens=5, cache_read_tokens=700, cache_write_tokens=200)


def test_raw_openai_usage_details():
    u = normalize_usage({"prompt_tokens": 400, "completion_tokens": 20,
                         "prompt_tokens_details": {"cached_tokens": 128}}, provider="openai")
    assert u == Usage(input_tokens=400, output_tokens=20, cache_read_tokens=128, cache_write_tokens=0)


@pytest.mark.parametrize("bad", [None, {}, {"foo": 1}])
def test_missing_or_unknown_usage_is_zero_not_error(bad):
    assert normalize_usage(bad) == Usage(0, 0, 0, 0)


def test_usage_adds():
    a, b = Usage(10, 2, 4, 1), Usage(5, 1, 0, 0)
    assert a + b == Usage(15, 3, 4, 1)


def test_cost_splits_cached_from_fresh_input():
    # fresh = 1000 - 700 - 200 = 100
    u = Usage(1000, 50, 700, 200)
    expected = (100 * 2.0 + 700 * 0.1 + 200 * 2.5 + 50 * 10.0) / 1e6
    assert cost_usd_est(u, PRICE) == pytest.approx(expected)


def test_cost_without_cache_prices_bills_cache_at_input_rate():
    p = Price(input_per_mtok=1.0, output_per_mtok=2.0, as_of="2026-11-27")
    assert cost_usd_est(Usage(1000, 100, 600, 0), p) == pytest.approx((1000 * 1.0 + 100 * 2.0) / 1e6)


def test_cost_never_negative_when_cache_exceeds_input():
    assert cost_usd_est(Usage(10, 0, 50, 0), PRICE) >= 0
