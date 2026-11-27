"""What: load the demo persona list from data/seeds/personas.yaml (doc 07 s2.2 Persona).
Use when: GET /api/demo/personas.
Inputs: path to the YAML file (a top-level list, or a mapping with a `personas` list).
Output: list[Persona]. A missing file gives an empty list and a logged warning (lane A adds the file);
a present but malformed file raises so a bad seed is never hidden.
Side effects: reads one file; logs.
Role: the only reader of the personas file; the values belong to lane A.
"""
from __future__ import annotations

import logging
from pathlib import Path

import yaml
from contracts.agent_api import Persona

log = logging.getLogger("agent.personas")


def load_personas(path: str) -> list[Persona]:
    """What: read and validate the personas file.
    Use when: each GET /api/demo/personas (the file is small and can change on reseed).
    Inputs: file path. Output: validated Persona list, empty when the file does not exist.
    Side effects: file read; a warning log when missing. Role: loader."""
    p = Path(path)
    if not p.is_file():
        log.warning("personas file not found path=%s; returning an empty list", path)
        return []
    raw = yaml.safe_load(p.read_text()) or []
    items = raw.get("personas", []) if isinstance(raw, dict) else raw
    return [Persona.model_validate(item) for item in items]
