"""What: runtime settings for core (tokens, database URL, demo fault flag, policy path).
Use when: the process starts (Settings.from_env) or a test builds an app (Settings(...)).
Inputs: env CORE_TOKEN_AGENT, CORE_TOKEN_COMMIT, CORE_TOKEN_OPS, CORE_DB_URL, DEMO_FAULTS_ENABLED, POLICY_PATH.
Output: Settings, ConfigError.
Side effects: from_env reads the environment.
Role: one validated source of configuration (doc 07 s15.2); a missing or shared token stops the process."""
from __future__ import annotations

import os
from dataclasses import dataclass


class ConfigError(Exception):
    """What: raised for any invalid or missing setting. Use when: validating Settings.
    Inputs: message. Output: exception. Side effects: none. Role: fail loudly at startup, never half-configured."""


@dataclass(frozen=True)
class Settings:
    """What: the three bearer tokens, the app-role database URL and flags.
    Use when: building the app. Inputs: token_agent, token_commit, token_ops, db_url (None only in unit tests),
    demo_faults, policy_path (None means the contracts default).
    Output: frozen settings. Side effects: none.
    Role: doc 07 s1.4 (three distinct tokens) and s15.2."""

    token_agent: str
    token_commit: str
    token_ops: str
    db_url: str | None
    demo_faults: bool = False
    policy_path: str | None = None

    def __post_init__(self) -> None:
        """What: reject empty or duplicate tokens. Use when: construction. Inputs: self.
        Output: none. Side effects: raises ConfigError. Role: a shared token would erase least privilege."""
        tokens = [self.token_agent, self.token_commit, self.token_ops]
        if not all(tokens):
            raise ConfigError("CORE_TOKEN_AGENT, CORE_TOKEN_COMMIT and CORE_TOKEN_OPS must all be set")
        if len(set(tokens)) != 3:
            raise ConfigError("the three core tokens must be distinct")

    @classmethod
    def from_env(cls) -> Settings:
        """What: build settings from the environment. Use when: process start. Inputs: env vars listed above.
        Output: Settings. Side effects: reads env; raises ConfigError when a required value is missing.
        Role: production entry; CORE_DB_URL is required here (only unit tests run without a database)."""
        env = os.environ
        db_url = env.get("CORE_DB_URL", "")
        if not db_url:
            raise ConfigError("CORE_DB_URL is not set")
        return cls(
            token_agent=env.get("CORE_TOKEN_AGENT", ""),
            token_commit=env.get("CORE_TOKEN_COMMIT", ""),
            token_ops=env.get("CORE_TOKEN_OPS", ""),
            db_url=db_url,
            demo_faults=env.get("DEMO_FAULTS_ENABLED", "false").strip().lower() == "true",
            policy_path=env.get("POLICY_PATH") or None,
        )
