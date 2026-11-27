"""What: agent runtime settings read from the environment (doc 07 s15.2 names, Compose x-agent env).
Use when: building the app (Settings.from_env) or tests (construct directly).
Inputs: env SERVED_BY, AGENT_DB_URL, REDIS_URL, CORE_URL, KNOWLEDGE_URL, DEMO_FAULTS_ENABLED.
Output: Settings.
Side effects: from_env reads os.environ.
Role: the one place that knows env names; nothing else reads os.environ for these.
"""
from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    """What: immutable settings. Use when: app construction and dependency clients.
    Inputs: replica name, DB URL (agent_app), Redis URL, core and knowledge base URLs, fault flag.
    Output: value object. Side effects: none. Role: config."""

    served_by: str
    agent_db_url: str
    redis_url: str
    core_url: str
    knowledge_url: str
    demo_faults_enabled: bool

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
                   demo_faults_enabled=env.get("DEMO_FAULTS_ENABLED", "false").strip().lower() == "true")
