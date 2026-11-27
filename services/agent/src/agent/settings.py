"""What: agent runtime settings read from the environment (doc 07 s15.2 names, Compose x-agent env).
Use when: building the app (Settings.from_env) or tests (construct directly).
Inputs: env SERVED_BY, AGENT_DB_URL, REDIS_URL, CORE_URL, KNOWLEDGE_URL, DEMO_FAULTS_ENABLED, SITE_ORIGIN,
SESSION_SIGNING_KEY, DEMO_ACCESS_CODE, PERSONAS_PATH, OPENAI_API_KEY (voice mint), CORE_TOKEN_AGENT and
KNOWLEDGE_TOKEN_AGENT (the tool clients, doc 07 s1.4), FORWARDED_ALLOW_IPS (client IP for rate limits),
SITE_ADDRESS (the Caddy site address) and AGENT_ENV (production config, see Settings.production).
Output: Settings, secret_problem.
Side effects: from_env reads os.environ.
Role: the one place that knows env names; nothing else reads os.environ for these.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field

MIN_SECRET_LEN = 20  # scripts/keys.py format_problem: shorter is "too short to be a real key"; gen writes 43
# Anchored at the start, so a random generated token cannot trip it by chance.
_PLACEHOLDER_RE = re.compile(r"^(?:change[-_ ]?me|replace|placeholder|example|your[-_ ]|x{3,}|todo|dummy|<)",
                             re.IGNORECASE)
_LOCAL_HOSTS = frozenset({"", "localhost", "127.0.0.1", "0.0.0.0", "::1", "[::1]"})


def secret_problem(value: str | None) -> str | None:
    """What: why a token or key value cannot be real: absent, empty, a placeholder word, or too short.
    Use when: the startup wiring check and CoreCommitClient.from_env. Inputs: the value (None when unset).
    Output: a short reason (never the value), or None when it looks real. Side effects: none.
    Role: one rule for "a secret is set", shared with scripts/keys.py's length floor."""
    if value is None:
        return "not set"
    if not value.strip():
        return "empty"
    if _PLACEHOLDER_RE.search(value.strip()):
        return "a placeholder"
    if len(value.strip()) < MIN_SECRET_LEN:
        return f"too short to be a real secret (under {MIN_SECRET_LEN} characters)"
    return None


def is_local_site(site_address: str) -> bool:
    """What: whether a Caddy SITE_ADDRESS serves only this machine (":80", ":3084", "localhost", "127.0.0.1",
    empty). Use when: Settings.production. Inputs: the address. Output: bool. Side effects: none. Role: helper."""
    addr = site_address.strip().lower().split("://", 1)[-1].split("/", 1)[0]
    if addr.startswith("[") and "]" in addr:
        host = addr[: addr.index("]") + 1]
    else:
        host = addr.rsplit(":", 1)[0] if ":" in addr else addr
    return host in _LOCAL_HOSTS or host.endswith(".localhost")


@dataclass(frozen=True)
class Settings:
    """What: immutable settings. Use when: app construction and dependency clients.
    Inputs: replica name, DB URL (agent_app), Redis URL, core and knowledge base URLs, fault flag,
    site origin, cookie signing key, demo access code, personas file path, OpenAI key (voice mint), models.yaml
    path (the /data mount; not an env name), the agent tokens for core and knowledge.
    Output: value object. Side effects: none. Role: config."""

    served_by: str
    agent_db_url: str = field(repr=False)  # carries the agent_app password
    redis_url: str
    core_url: str
    knowledge_url: str
    demo_faults_enabled: bool
    site_origin: str = "http://localhost:3000"
    session_signing_key: str = field(default="", repr=False)   # empty: the access route fails loudly and no cookie ever verifies
    demo_access_code: str = field(default="", repr=False)      # empty: nobody gets in
    personas_path: str = "/data/seeds/personas.yaml"
    openai_api_key: str = field(default="", repr=False)        # empty: voice/token answers 503 VOICE_UNAVAILABLE (never logged)
    models_path: str = "/data/models.yaml"
    core_token_agent: str = ""      # empty: core answers 401 to every tool call (never logged)
    knowledge_token_agent: str = ""  # empty: knowledge answers 401 to search_kb (never logged)
    forwarded_allow_ips: str = ""   # edge subnet (D-047); empty: X-Forwarded-For is never trusted (rate-limit IP key)
    site_address: str = ""          # Caddy SITE_ADDRESS; a public one (not :80 or localhost) means production config
    agent_env: str = ""             # AGENT_ENV=production forces production config whatever the site address

    @property
    def production(self) -> bool:
        """What: production config: AGENT_ENV is "production" (any case), or SITE_ADDRESS is not local.
        Use when: assert_wired decides whether a pending safety piece (Gate 2, PIN scan) fails startup or only warns.
        Inputs: self. Output: bool. Side effects: none. Role: the one "is this the shared URL" rule."""
        return self.agent_env.strip().lower() == "production" or not is_local_site(self.site_address)

    @classmethod
    def from_env(cls) -> "Settings":
        """What: read settings from os.environ with the Compose defaults.
        Use when: process start. Inputs: env. Output: Settings. Side effects: reads env.
        Role: loader; DEMO_FAULTS_ENABLED is true only for the literal "true" (case-insensitive)."""
        env = os.environ
        return cls(served_by=env.get("SERVED_BY", "agent"),
                   agent_db_url=env.get("AGENT_DB_URL", ""),
                   redis_url=env.get("REDIS_URL", "redis://redis:6379/0"),
                   core_url=env.get("CORE_URL", "http://core:8000"),
                   knowledge_url=env.get("KNOWLEDGE_URL", "http://knowledge:8000"),
                   demo_faults_enabled=env.get("DEMO_FAULTS_ENABLED", "false").strip().lower() == "true",
                   site_origin=env.get("SITE_ORIGIN", "http://localhost:3000").rstrip("/"),
                   session_signing_key=env.get("SESSION_SIGNING_KEY", ""),
                   demo_access_code=env.get("DEMO_ACCESS_CODE", ""),
                   personas_path=env.get("PERSONAS_PATH", "/data/seeds/personas.yaml"),
                   openai_api_key=env.get("OPENAI_API_KEY", ""),
                   core_token_agent=env.get("CORE_TOKEN_AGENT", ""),
                   knowledge_token_agent=env.get("KNOWLEDGE_TOKEN_AGENT", ""),
                   forwarded_allow_ips=env.get("FORWARDED_ALLOW_IPS", ""),
                   site_address=env.get("SITE_ADDRESS", ""),
                   agent_env=env.get("AGENT_ENV", ""))
