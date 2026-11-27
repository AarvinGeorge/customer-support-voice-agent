"""What: provider-neutral token usage and price math.
Use when: turning a model response's usage into one shape and a USD estimate.
Inputs: LangChain usage_metadata or raw provider usage dicts.
Output: contracts Usage values (token_usage, add_usage, normalize_usage), cost_usd_est floats and reply_truncated.
Side effects: none.
Role: single normalizer for both providers' cached-token fields.
"""
from __future__ import annotations

from typing import Any, Mapping

from contracts.events import Usage

from agent.models.config import Price


def token_usage(input_tokens: int = 0, output_tokens: int = 0, cache_read_tokens: int = 0,
                cache_write_tokens: int = 0) -> Usage:
    """What: a contracts Usage holding only token counts (cost, latency and counts zero, no models).
    Use when: one model call's usage before the turn adds timings, counts and cost.
    Inputs: four ints; input_tokens is the TOTAL prompt size including cache read and write.
    Output: contracts.events.Usage.
    Side effects: none.
    Role: per-call shape of the one Usage model (doc 07 s6.2).
    """
    return Usage(input_tokens=input_tokens, output_tokens=output_tokens,
                 cache_read_tokens=cache_read_tokens, cache_write_tokens=cache_write_tokens,
                 cost_usd_est=0.0, latency_ms=0, model_calls=0, tool_calls=0, models_used=[],
                 fallback_used=False)


def add_usage(a: Usage, b: Usage) -> Usage:
    """What: sum two Usage values.
    Use when: accumulating per-call usage into the turn total.
    Inputs: two contracts Usage values.
    Output: new Usage: tokens, cost, latency and counts summed; models_used merged in first-seen
    order; fallback_used OR-ed; first_status_ms and ttft_ms keep the first non-None value.
    Side effects: none.
    Role: turn accumulator.
    """
    models = list(dict.fromkeys([*a.models_used, *b.models_used]))
    return Usage(input_tokens=a.input_tokens + b.input_tokens,
                 output_tokens=a.output_tokens + b.output_tokens,
                 cache_read_tokens=a.cache_read_tokens + b.cache_read_tokens,
                 cache_write_tokens=a.cache_write_tokens + b.cache_write_tokens,
                 cost_usd_est=a.cost_usd_est + b.cost_usd_est,
                 first_status_ms=a.first_status_ms if a.first_status_ms is not None else b.first_status_ms,
                 ttft_ms=a.ttft_ms if a.ttft_ms is not None else b.ttft_ms,
                 latency_ms=a.latency_ms + b.latency_ms, model_calls=a.model_calls + b.model_calls,
                 tool_calls=a.tool_calls + b.tool_calls, models_used=models,
                 fallback_used=a.fallback_used or b.fallback_used)


def _i(v: Any) -> int:
    """What: a usage count as int (0 when absent or not a number). Use when: normalize_usage. Inputs: value.
    Output: int. Side effects: none. Role: helper."""
    return int(v) if isinstance(v, (int, float)) else 0


def normalize_usage(raw: Mapping[str, Any] | None, provider: str | None = None) -> Usage:
    """What: map any supported usage dict to Usage.
    Use when: reading AIMessage.usage_metadata (default) or a raw provider usage object (pass provider).
    Inputs: dict or None; provider "anthropic" or "openai" only for raw shapes.
    Output: contracts Usage (token fields only); unknown or empty input gives all zeros.
    Side effects: none.
    Role: adapter for Anthropic (cache_read_input_tokens, cache_creation_input_tokens; raw input excludes
    cache; LangChain shape may carry writes as ephemeral_5m/1h_input_tokens with cache_creation 0) and OpenAI (prompt_tokens_details.cached_tokens; prompt_tokens includes cache).
    """
    if not raw:
        return token_usage()
    if "input_token_details" in raw or (provider is None and "input_tokens" in raw):
        d = raw.get("input_token_details") or {}
        # langchain-anthropic zeroes cache_creation and reports the TTL split instead when present.
        write = _i(d.get("cache_creation")) + _i(d.get("ephemeral_5m_input_tokens")) \
            + _i(d.get("ephemeral_1h_input_tokens"))
        return token_usage(_i(raw.get("input_tokens")), _i(raw.get("output_tokens")),
                           _i(d.get("cache_read")), write)
    if provider == "anthropic":
        read, write = _i(raw.get("cache_read_input_tokens")), _i(raw.get("cache_creation_input_tokens"))
        return token_usage(_i(raw.get("input_tokens")) + read + write, _i(raw.get("output_tokens")), read, write)
    if provider == "openai" or "prompt_tokens" in raw:
        d = raw.get("prompt_tokens_details") or {}
        return token_usage(_i(raw.get("prompt_tokens")), _i(raw.get("completion_tokens")),
                           _i(d.get("cached_tokens")), 0)
    return token_usage()


def cost_usd_est(usage: Usage, price: Price) -> float:
    """What: estimate USD cost of one usage under a role's price block.
    Use when: filling Usage.cost_usd_est in turn.completed and the step events.
    Inputs: Usage; Price (a missing cache price bills those tokens at the input rate).
    Output: float USD, never negative.
    Side effects: none.
    Role: price math.
    """
    read, write = usage.cache_read_tokens, usage.cache_write_tokens
    fresh = max(usage.input_tokens - read - write, 0)
    read_rate = price.cache_read_per_mtok if price.cache_read_per_mtok is not None else price.input_per_mtok
    write_rate = price.cache_write_per_mtok if price.cache_write_per_mtok is not None else price.input_per_mtok
    total = fresh * price.input_per_mtok + read * read_rate + write * write_rate \
        + usage.output_tokens * price.output_per_mtok
    return max(total, 0.0) / 1_000_000


TRUNCATED_STOPS = frozenset({"max_tokens", "length"})  # Anthropic stop_reason, OpenAI finish_reason


def reply_truncated(reply: Any) -> bool:
    """What: true when the provider stopped a reply at its token cap: Anthropic `stop_reason` max_tokens (ChatAnthropic
    also sets the shared `finish_reason`), OpenAI `finish_reason` length, or LangChain's `normalized_finish_reason`
    length. Use when: the gates and DeadlineAndFaultMiddleware, right after a model call.
    Inputs: the reply (an AIMessage or anything with response_metadata). Output: bool. Side effects: none.
    Role: a cut reply holds no complete answer (with thinking on, a cap can be spent before any text,
    platform.claude.com/docs/en/build-with-claude/thinking), so it is never read as one (review PR #68 I-1, M-1)."""
    meta = getattr(reply, "response_metadata", None) or {}
    return any(meta.get(k) in TRUNCATED_STOPS for k in ("stop_reason", "finish_reason", "normalized_finish_reason"))
