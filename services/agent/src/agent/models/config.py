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
        Inputs: self. Output: self; ValueError on a missing role or timeout. Side effects: none. Role: validator."""
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
        return self


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
