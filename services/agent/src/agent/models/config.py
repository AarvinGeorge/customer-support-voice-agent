"""What: pydantic schema and loader for data/models.yaml (doc 07 s13.2).
Use when: reading the model registry; never parse the YAML elsewhere.
Inputs: path to models.yaml.
Output: a validated ModelsConfig.
Side effects: reads one file.
Role: config contract; unknown keys fail loudly (extra=forbid).
"""
from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, model_validator

REQUIRED_ROLES = ("agent", "agent_fallback", "grounding_verifier", "scope_classifier",
                  "judge", "embeddings", "voice")
CHAT_ROLES = ("agent", "agent_fallback", "grounding_verifier", "scope_classifier", "judge")
STREAMING_ROLES = ("agent", "agent_fallback")
# Gate roles answer with a short fixed reply, so thinking is off and the cap is sized to the reply (fix after browser
# pass 2: adaptive thinking on claude-haiku-5-5 spent the whole 512-token cap and the verifier returned no verdict).
# The minimum is the reply's own size in tokens; the configured max_tokens must be at least twice it (margin).
# grounding_verifier: a long verdict (8 claims of 200 characters, about 2,300 characters of JSON) is about 575
# tokens at 4 characters per token, and about 750 with Haiku 5.5's tokenizer (about 30 percent more tokens, per the
# migration guide; not measured), so 640 is a rough figure and the 2x margin (1280) carries the rest. The largest
# measured verdict was 178 tokens (PR #68 eval). scope_classifier: ON or OFF (4 to 5 tokens measured).
GATE_REPLY_MIN_TOKENS = {"grounding_verifier": 640, "scope_classifier": 8}
GATE_CAP_MARGIN = 2
# Every role that returns a structured verdict gets a token rule (root-cause audit 2026-11-27 open item 5): the gate
# roles above and the offline judge. judge: a pass or fail per check with a short reason, about 2,000 characters of
# JSON at most (about 500 tokens); a rough figure, carried by the 2x margin.
VERDICT_REPLY_MIN_TOKENS = {**GATE_REPLY_MIN_TOKENS, "judge": 512}
# OpenAI reasoning models: max_output_tokens (max_completion_tokens on chat completions) caps reasoning and visible
# tokens together, and a reply cut there may hold no visible output; "OpenAI recommends reserving at least 25,000
# tokens for reasoning and outputs" (developers.openai.com/api/docs/guides/reasoning, "Allocating space for
# reasoning", read via context7 2026-11-27). reasoning_effort defaults to "medium" (API reference, chat completions
# and evals sampling_params), so a missing effort key means the model reasons.
OPENAI_REASONING_RESERVE_TOKENS = 25000
# Models that reject thinking {"type": "disabled"} with a 400 (claude-api skill, Thinking and Effort table, read
# 2026-11-27; live page platform.claude.com/docs/en/build-with-claude/thinking): Claude Opus 5.5 ("disabled and
# enabled return 400 at every effort level"), Claude Sonnet 5.5 ("disabled returns 400"; between_tools is its off
# switch, not supported here), Fable 5, 5.1, 5.2 and the Mythos counterparts ("explicit disabled returns 400").
# Claude Haiku 5.5, Opus 5 and Sonnet 5 accept disabled (Haiku 5.5 and Opus 5 at effort high or below, the highest
# value RoleSpec.effort allows). Prefix match, so a dated or suffixed id is covered.
THINKING_ALWAYS_ON = ("claude-opus-5-5", "claude-sonnet-5-5", "claude-fable-5", "claude-mythos-5")
# A gate role on one of those models keeps adaptive thinking at effort low, so its cap must also hold the thinking.
GATE_THINKING_ALLOWANCE_TOKENS = 4096


class Price(BaseModel):
    """What: per-1M-token price block with a mandatory as_of date.
    Use when: costing usage for a role.
    Inputs: USD per million tokens; as_of date.
    Output: validated object.
    Side effects: none.
    Role: pricing data.
    """

    model_config = ConfigDict(extra="forbid")
    input_per_mtok: float
    output_per_mtok: float
    cache_read_per_mtok: float | None = None
    cache_write_per_mtok: float | None = None
    audio_per_mtok: float | None = None      # audio input (voice)
    audio_out_per_mtok: float | None = None  # audio output (voice); extends doc 07 13.2
    as_of: date


class RoleSpec(BaseModel):
    """What: one role entry (provider, model id, timeouts, limits, price).
    Use when: building a client or costing a call for a role.
    Inputs: YAML mapping.
    Output: validated object.
    Side effects: none.
    Role: per-role configuration.
    """

    model_config = ConfigDict(extra="forbid", validate_assignment=True)
    provider: Literal["anthropic", "openai"]
    model: str
    effort: Literal["none", "low", "medium", "high"] | None = None  # "none": OpenAI only (tools on chat completions)
    thinking: Literal["disabled", "adaptive"] | None = None  # anthropic only; None: the model's default
    attempt_timeout_s: float | None = None
    first_token_timeout_s: float | None = None
    max_tokens: int | None = None
    dimensions: int | None = None            # embeddings only
    voice: str | None = None                 # voice only
    transcription_model: str | None = None   # voice only
    price: Price | None = None


class ModelsConfig(BaseModel):
    """What: whole models.yaml document.
    Use when: loading the registry.
    Inputs: YAML mapping with a `roles` key.
    Output: validated object; fails on missing roles, unknown keys or bad timeouts.
    Side effects: none.
    Role: config root.
    """

    model_config = ConfigDict(extra="forbid")
    roles: dict[str, RoleSpec]

    @model_validator(mode="after")
    def _check_roles(self) -> "ModelsConfig":
        """What: require every role and the chat roles' timeouts. Use when: pydantic validation of models.yaml.
        Inputs: self. Output: self; ValueError on a missing role or timeout, effort none off OpenAI, thinking off Anthropic, a gate
        role with thinking on or a cap too small for its reply, or a verdict role (judge) whose cap cannot hold its
        reasoning and verdict. Side effects: none. Role: validator."""
        missing = [r for r in REQUIRED_ROLES if r not in self.roles]
        if missing:
            raise ValueError(f"missing roles: {', '.join(missing)}")
        for name in CHAT_ROLES:
            if self.roles[name].attempt_timeout_s is None:
                raise ValueError(f"role {name}: attempt_timeout_s is required")
        for name in STREAMING_ROLES:
            r = self.roles[name]
            if r.first_token_timeout_s is None:
                raise ValueError(f"role {name}: first_token_timeout_s is required")
            if r.attempt_timeout_s is not None and r.first_token_timeout_s > r.attempt_timeout_s:
                raise ValueError(f"role {name}: first_token_timeout_s exceeds attempt_timeout_s")
        for name, r in self.roles.items():
            if r.price is None:
                raise ValueError(f"role {name}: price block is required")
            if r.effort == "none" and r.provider != "openai":
                raise ValueError(f"role {name}: effort none is OpenAI only (tools on chat completions)")
            if r.thinking is not None and r.provider != "anthropic":
                raise ValueError(f"role {name}: thinking is an anthropic setting (use effort none on OpenAI)")
            if r.thinking == "disabled" and thinking_always_on(r.model):
                raise ValueError(f"role {name}: {r.model} rejects thinking disabled (400); leave thinking unset")
        gates = gate_roles(self)
        for name in gates:
            _check_gate_role(name, self.roles[name])
        for name in verdict_roles(self):
            if name not in gates:
                _check_verdict_role(name, self.roles[name])
        return self


def gate_roles(config: "ModelsConfig") -> list[str]:
    """What: the gate roles in a config: grounding_verifier, scope_classifier and any `<gate role>_fallback`.
    Use when: the config validator and the gate request tests. Inputs: ModelsConfig. Output: role names.
    Side effects: none. Role: one list, so a fallback gate model added later gets the same rules."""
    return [r for r in config.roles if r in GATE_REPLY_MIN_TOKENS
            or (r.endswith("_fallback") and r.removesuffix("_fallback") in GATE_REPLY_MIN_TOKENS)]


def verdict_roles(config: "ModelsConfig") -> list[str]:
    """What: every role that returns a structured verdict: the gate roles and the judge, each with any
    `<role>_fallback`. Use when: the config validator and the verdict cap tests. Inputs: ModelsConfig.
    Output: role names. Side effects: none. Role: one list, so no verdict role sits outside the token rule."""
    return [r for r in config.roles if r in VERDICT_REPLY_MIN_TOKENS
            or (r.endswith("_fallback") and r.removesuffix("_fallback") in VERDICT_REPLY_MIN_TOKENS)]


def _check_verdict_role(name: str, r: RoleSpec) -> None:
    """What: the token rule for a verdict role that is not a gate (the offline judge, which may reason). OpenAI:
    effort must be explicit (the API default is medium); effort none needs a cap of GATE_CAP_MARGIN times the
    verdict; any reasoning effort needs OPENAI_REASONING_RESERVE_TOKENS. Anthropic: the gate thinking rule
    (thinking disabled with the 2x cap, or effort low plus the thinking allowance on a model that cannot turn it off).
    Use when: ModelsConfig validation. Inputs: role name, spec. Output: none; ValueError naming the role.
    Side effects: none. Role: audit open item 5; a judge cannot spend its cap on reasoning and return no verdict."""
    base = name.removesuffix("_fallback")
    if r.provider == "anthropic":
        need = VERDICT_REPLY_MIN_TOKENS[base] * GATE_CAP_MARGIN
        if thinking_always_on(r.model):
            if r.effort != "low":
                raise ValueError(f"role {name}: {r.model} cannot turn thinking off, so a verdict role needs effort: low")
            need += GATE_THINKING_ALLOWANCE_TOKENS
        elif r.thinking != "disabled":
            raise ValueError(f"role {name}: a verdict role needs thinking: disabled (thinking spends the reply's tokens)")
    else:
        if r.effort is None:
            raise ValueError(f"role {name}: a verdict role on OpenAI needs an explicit effort (the API default is medium)")
        need = (VERDICT_REPLY_MIN_TOKENS[base] * GATE_CAP_MARGIN if r.effort == "none"
                else OPENAI_REASONING_RESERVE_TOKENS)
    if r.max_tokens is None or r.max_tokens < need:
        raise ValueError(f"role {name}: max_tokens must be at least {need} for the verdict (effort {r.effort})")


def thinking_always_on(model: str) -> bool:
    """What: true for a model id that rejects thinking disabled (THINKING_ALWAYS_ON). Use when: the validator.
    Inputs: model id. Output: bool. Side effects: none. Role: per-model rule (review PR #68 I-2)."""
    return model.startswith(THINKING_ALWAYS_ON)


def _check_gate_role(name: str, r: RoleSpec) -> None:
    """What: a gate role's reply must fit its cap: thinking disabled where the model supports it, with a cap of at
    least GATE_CAP_MARGIN times the reply size; on a model that cannot turn thinking off, effort low and a cap that
    also holds GATE_THINKING_ALLOWANCE_TOKENS of thinking; on OpenAI, effort none.
    Use when: ModelsConfig validation. Inputs: role name, spec. Output: none; ValueError naming the role.
    Side effects: none. Role: the browser pass 2 bug (thinking spent the verifier's whole cap) cannot be configured
    again; per-model rows from the claude-api skill thinking table (see THINKING_ALWAYS_ON). OpenAI needs effort
    none because the gpt-5 family reasons by default and a missing key does not say the model is non-reasoning."""
    need = GATE_REPLY_MIN_TOKENS[name.removesuffix("_fallback")] * GATE_CAP_MARGIN
    if r.provider == "anthropic" and thinking_always_on(r.model):
        if r.effort != "low":
            raise ValueError(f"role {name}: {r.model} cannot turn thinking off, so a gate role needs effort: low")
        need += GATE_THINKING_ALLOWANCE_TOKENS
    elif r.provider == "anthropic" and r.thinking != "disabled":
        raise ValueError(f"role {name}: a gate role needs thinking: disabled (thinking spends the reply's tokens)")
    if r.provider == "openai" and r.effort != "none":
        raise ValueError(f"role {name}: a gate role on OpenAI needs effort: none")
    if r.max_tokens is None or r.max_tokens < need:
        raise ValueError(f"role {name}: max_tokens must be at least {need} for the gate reply")


def load_models_config(path: str | Path) -> ModelsConfig:
    """What: read and validate models.yaml.
    Use when: service start-up and tests.
    Inputs: path to the YAML file.
    Output: ModelsConfig; raises FileNotFoundError or pydantic ValidationError.
    Side effects: reads the file.
    Role: loader.
    """
    data = yaml.safe_load(Path(path).read_text())
    return ModelsConfig.model_validate(data)
