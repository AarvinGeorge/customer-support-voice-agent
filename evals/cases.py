"""What: loaders for the golden cases, the retrieval queries and the personas, plus placeholder resolution.
Use when: the runner starts. Inputs: evals/golden/golden.yaml, retrieval.yaml, schema/*.json, data/seeds/personas.yaml.
Output: lists of dicts (cases validated against the JSON schema) and a persona map. Side effects: reads files.
Role: the dataset boundary; an invalid case stops the run before any turn is sent."""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import jsonschema
import yaml

ROOT = Path(__file__).resolve().parents[1]
GOLDEN_DIR = ROOT / "evals" / "golden"
PERSONAS = ROOT / "data" / "seeds" / "personas.yaml"


class DatasetError(ValueError):
    """What: raised when a dataset file is invalid. Use when: loading. Inputs: message. Output: exception.
    Side effects: none. Role: fail before sending anything."""


def _load(path: Path, key: str, schema_name: str) -> list[dict[str, Any]]:
    """What: load a YAML list and validate each item. Use when: cases and queries. Inputs: path, top key, schema file.
    Output: items. Side effects: reads files. Role: shared validation."""
    import json
    schema = json.loads((GOLDEN_DIR / "schema" / schema_name).read_text())
    items = yaml.safe_load(path.read_text())[key]
    validator = jsonschema.Draft202012Validator(schema)
    for it in items:
        errs = sorted(validator.iter_errors(it), key=lambda e: list(e.path))
        if errs:
            raise DatasetError(f"{it.get('id', '?')}: {errs[0].message} at {list(errs[0].path)}")
    return items


def load_cases(path: Path | None = None) -> list[dict[str, Any]]:
    """What: the golden cases, schema-validated. Use when: the runner starts. Inputs: optional path. Output: case dicts.
    Side effects: reads files. Role: dataset boundary."""
    return _load(path or GOLDEN_DIR / "golden.yaml", "cases", "case.schema.json")


def load_retrieval(path: Path | None = None) -> list[dict[str, Any]]:
    """What: the labelled retrieval queries, schema-validated. Use when: Gate 1 counts. Inputs: optional path.
    Output: query dicts. Side effects: reads files. Role: dataset boundary."""
    return _load(path or GOLDEN_DIR / "retrieval.yaml", "queries", "retrieval.schema.json")


def load_personas(path: Path | None = None) -> dict[str, dict[str, Any]]:
    """What: personas by name (the runner reads account id and PIN from here). Use when: building card submits.
    Inputs: optional path. Output: name to persona dict. Side effects: reads a file. Role: the PIN never lives in the dataset."""
    return {p["name"]: p for p in yaml.safe_load((path or PERSONAS).read_text())}


def resolve_text(text: str, persona: dict[str, Any]) -> str:
    """What: fill {persona.account_id} and {persona.pin}. Use when: sending a text turn. Inputs: text, persona.
    Output: the text to send. Side effects: none. Role: the typed-PIN case (G-02); other braces are left alone."""
    return re.sub(r"\{persona\.(account_id|pin)\}", lambda m: str(persona[m.group(1)]), text)


def wrong_pin(real: str) -> str:
    """What: a four-digit PIN that is not the persona's. Use when: credentials is wrong_pin. Inputs: the real PIN.
    Output: a different 4-digit string. Side effects: none. Role: failed-verification turns."""
    return "0000" if real != "0000" else "1111"
