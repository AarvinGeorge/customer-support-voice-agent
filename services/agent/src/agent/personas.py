"""What: load the demo persona list from data/seeds/personas.yaml (doc 07 s2.2 Persona).
Use when: GET /api/demo/personas.
Inputs: path to the YAML file (a top-level list of mappings, or a mapping with a `personas` list). Each item may
carry keys beyond the contract; they are ignored and never returned.
Output: list[Persona] built from the contract fields only. A missing file gives [] and a warning; a file that is
not valid YAML or not a list gives [] and an error; an item that fails validation is skipped with an error that
names the item and the fields, never their values (PINs are demo data but logs stay clean).
Side effects: reads one file; logs.
Role: the only reader of the personas file; the values belong to lane A. Never raises, so the landing page cannot 500.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import yaml
from contracts.agent_api import Persona
from pydantic import ValidationError

log = logging.getLogger("agent.personas")
CONTRACT_FIELDS = tuple(Persona.model_fields)


def load_personas(path: str) -> list[Persona]:
    """What: read the file and keep every item that validates as a Persona, using only the contract fields.
    Use when: each GET /api/demo/personas (the file is small and can change on reseed).
    Inputs: file path. Output: Persona list; empty when the file is missing or unusable.
    Side effects: file read; warning or error logs without field values. Role: loader that never raises."""
    p = Path(path)
    if not p.is_file():
        log.warning("personas file not found path=%s; returning an empty list", path)
        return []
    try:
        raw: Any = yaml.safe_load(p.read_text())
    except (yaml.YAMLError, OSError, UnicodeDecodeError) as exc:
        log.error("personas file unreadable path=%s kind=%s; returning an empty list", path, type(exc).__name__)
        return []
    items = raw.get("personas") if isinstance(raw, dict) else raw
    if not isinstance(items, list):
        log.error("personas file path=%s is not a list of personas; returning an empty list", path)
        return []
    out: list[Persona] = []
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            log.error("personas item %d is not a mapping; skipped", index)
            continue
        try:
            out.append(Persona.model_validate({k: item[k] for k in CONTRACT_FIELDS if k in item}))
        except ValidationError as exc:
            fields = sorted({str(e["loc"][0]) if e["loc"] else "?" for e in exc.errors()})
            log.error("personas item %d invalid (fields: %s); skipped", index, ", ".join(fields))
    return out
