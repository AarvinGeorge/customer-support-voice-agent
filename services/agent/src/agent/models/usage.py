"""What: provider-neutral token usage and price math.
Use when: turning a model response's usage into one shape and a USD estimate.
Inputs: LangChain usage_metadata or raw provider usage dicts.
Output: Usage and cost_usd_est floats.
Side effects: none.
Role: single normalizer for both providers' cached-token fields.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from agent.models.config import Price


@dataclass(frozen=True)
class Usage:
    """What: normalized usage. input_tokens is the TOTAL prompt size including cache read and write.
    Use when: accumulating per-turn usage and costing it.
    Inputs: four ints.
    Output: immutable value; supports +.
    Side effects: none.
    Role: shape of one model call's usage.
    TODO: swap for contracts.Usage once packages/contracts (PR #4) is on main; that model also
    carries latency fields, so this stays the per-call part.
    """

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0

    def __add__(self, other: "Usage") -> "Usage":
        return Usage(self.input_tokens + other.input_tokens, self.output_tokens + other.output_tokens,
                     self.cache_read_tokens + other.cache_read_tokens,
                     self.cache_write_tokens + other.cache_write_tokens)


def _i(v: Any) -> int:
    return int(v) if isinstance(v, (int, float)) else 0


def normalize_usage(raw: Mapping[str, Any] | None, provider: str | None = None) -> Usage:
    """What: map any supported usage dict to Usage.
    Use when: reading AIMessage.usage_metadata (default) or a raw provider usage object (pass provider).
    Inputs: dict or None; provider "anthropic" or "openai" only for raw shapes.
    Output: Usage; unknown or empty input gives all zeros.
    Side effects: none.
    Role: adapter for Anthropic (cache_read_input_tokens, cache_creation_input_tokens; raw input excludes
    cache) and OpenAI (prompt_tokens_details.cached_tokens; prompt_tokens includes cache).
    """
    if not raw:
        return Usage()
    if "input_token_details" in raw or (provider is None and "input_tokens" in raw):
        d = raw.get("input_token_details") or {}
        return Usage(_i(raw.get("input_tokens")), _i(raw.get("output_tokens")),
                     _i(d.get("cache_read")), _i(d.get("cache_creation")))
    if provider == "anthropic":
        read, write = _i(raw.get("cache_read_input_tokens")), _i(raw.get("cache_creation_input_tokens"))
        return Usage(_i(raw.get("input_tokens")) + read + write, _i(raw.get("output_tokens")), read, write)
    if provider == "openai" or "prompt_tokens" in raw:
        d = raw.get("prompt_tokens_details") or {}
        return Usage(_i(raw.get("prompt_tokens")), _i(raw.get("completion_tokens")),
                     _i(d.get("cached_tokens")), 0)
    return Usage()


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
