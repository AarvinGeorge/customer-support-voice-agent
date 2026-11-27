"""What: knowledge service constants and the embedding model lookup.
Use when: ingest and search need the embedding model id or vector size (doc 07 s13.2 embeddings role).
Inputs: optional env MODELS_PATH (default /data/models.yaml); that file's roles.embeddings.model when present.
Output: EMBEDDING_DIMENSIONS and embedding_model(). Side effects: reads the file when it exists.
Role: the model id belongs in data/models.yaml (D-024); until lane B's file is on main the documented default applies.
A change of embedding model needs a re-ingest and recalibration."""
import os
from pathlib import Path

import yaml

EMBEDDING_DIMENSIONS = 1536
DEFAULT_EMBEDDING_MODEL = "text-embedding-3-small"


def embedding_model() -> str:
    """What: the embedding model id. Use when: stamping rows, calling the API, checking readiness.
    Inputs: env MODELS_PATH. Output: model id string. Side effects: reads models.yaml if present.
    Role: models.yaml roles.embeddings.model, else text-embedding-3-small."""
    path = Path(os.environ.get("MODELS_PATH", "/data/models.yaml"))
    if path.is_file():
        data = yaml.safe_load(path.read_text()) or {}
        model = ((data.get("roles") or {}).get("embeddings") or {}).get("model")
        if model:
            return str(model)
    return DEFAULT_EMBEDDING_MODEL
