"""What: Gate 1: the retrieval verdict (pass, weak, none, error, uncalibrated) and the calibration file loader.
Use when: POST /v1/search builds its confidence block; startup loads the calibration file; /readyz checks it.
Inputs: leg statuses, code facts, top cosine, a Calibration or None. Output: a (verdict, reason) pair, a Calibration.
Side effects: load_calibration reads one JSON file. Role: doc 07 s4.1 verdict order and s13.3 file shape. Cut fields
(ambiguity_margin, noise_floor, precision table) are deliberately not read."""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

DEFAULT_PATH = "/app/config/retrieval_calibration.json"
Verdict = Literal["pass", "weak", "none", "error", "uncalibrated"]
Reason = Literal["exact_code_match", "above_threshold", "weak_match", "below_threshold", "unknown_error_code",
                 "embedding_unavailable", "retrieval_unavailable", "no_calibration"]
Status = Literal["calibrated", "provisional"]


class CalibrationError(ValueError):
    """What: the calibration file is present but unusable. Use when: load_calibration rejects a file. Inputs: message.
    Output: exception. Side effects: none. Role: fail loudly at startup rather than gate with wrong numbers."""


@dataclass(frozen=True)
class Calibration:
    """What: the fields of retrieval_calibration.json that gating needs. Use when: decide and search read thresholds.
    Inputs: file values. Output: immutable record. Side effects: none. Role: doc 07 s13.3. k_default is validated but not yet applied to the request default (both are 5)."""
    calibration_id: str
    status: Status
    embedding_model: str
    corpus_version: str
    tau_low: float
    tau_high: float
    k_default: int
    notes: str = ""


def calibration_path() -> Path:
    """What: where the calibration file lives. Use when: startup. Inputs: env CALIBRATION_PATH. Output: Path.
    Side effects: none. Role: defaults to /app/config/retrieval_calibration.json, baked into the image from services/knowledge/config
    (doc 07 s13.3); the env name is an optional override, not in doc 07 s15.2."""
    return Path(os.environ.get("CALIBRATION_PATH", DEFAULT_PATH))


def load_calibration(path: Path) -> Calibration | None:
    """What: read and validate the calibration file. Use when: the service starts. Inputs: file path.
    Output: Calibration, or None when the file does not exist (verdicts become uncalibrated).
    Side effects: reads the file. Role: raises CalibrationError on bad JSON, a missing key, status other than
    provisional or calibrated, taus outside 0..1 or tau_low above tau_high, k_default outside 1..8, empty model id."""
    path = Path(path)
    if not path.is_file():
        return None
    try:
        raw = json.loads(path.read_text())
        low, high = raw["tau_low"], raw["tau_high"]
        cal = Calibration(calibration_id=str(raw["calibration_id"]), status=raw["status"],
                          embedding_model=str(raw["embedding_model"]), corpus_version=raw["corpus_version"],
                          tau_low=low, tau_high=high, k_default=raw.get("k_default", 5),
                          notes=str(raw.get("notes", "")))
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise CalibrationError(f"{path}: unreadable calibration file ({type(exc).__name__}: {exc})") from exc
    nums_ok = all(isinstance(v, (int, float)) and not isinstance(v, bool) and 0.0 <= v <= 1.0 for v in (low, high))
    if raw["status"] not in ("provisional", "calibrated"):
        raise CalibrationError(f"{path}: status must be provisional or calibrated, got {raw['status']!r}")
    if not nums_ok or low > high:
        raise CalibrationError(f"{path}: need 0 <= tau_low <= tau_high <= 1, got {low!r}, {high!r}")
    if not isinstance(cal.corpus_version, str) or not cal.corpus_version.strip():
        raise CalibrationError(f"{path}: corpus_version must be a non-empty string")
    if not isinstance(cal.k_default, int) or isinstance(cal.k_default, bool) or not 1 <= cal.k_default <= 8:
        raise CalibrationError(f"{path}: k_default must be 1..8, got {cal.k_default}")
    if not cal.embedding_model.strip():
        raise CalibrationError(f"{path}: embedding_model is empty")
    return cal


def is_stale(cal: Calibration | None, corpus_version: str) -> bool:
    """What: has the corpus changed since calibration. Use when: building the confidence block. Inputs: calibration
    or None, the live corpus_version. Output: True when the versions differ. Side effects: none.
    Role: doc 07 s13.3 calibration_stale."""
    return cal is not None and cal.corpus_version != corpus_version


def decide(*, vector: str, keyword: str, code_match: bool, has_unknown_code: bool, top_cosine: float | None,
           chunk_count: int, calibration: Calibration | None, calibration_mode: bool) -> tuple[Verdict, Reason]:
    """What: the Gate 1 verdict and its reason. Use when: a search result is assembled. Inputs: leg statuses
    (ok, empty, failed), whether a returned chunk carries a detected code, whether the query had an unknown code,
    the best cosine (None when the vector leg failed), number of chunks, calibration or None, the request's
    calibration flag. Output: (verdict, reason). top_cosine is the best cosine among the returned k chunks (what the model will see), so
    probe and calibrate at the production k. Side effects: none. Role: doc 07 s4.1 order: both legs failed
    error; calibration mode (gating off, doc 07 s4.1 calibration flag) uncalibrated; unknown code without a match none; code match pass; vector failed error (an outage is not evidence the
    docs lack the answer); no calibration uncalibrated; no chunks or below tau_low none;
    below tau_high weak; else pass."""
    if vector == "failed" and keyword == "failed":
        return "error", "retrieval_unavailable"
    if calibration_mode:
        return "uncalibrated", "no_calibration"
    if has_unknown_code and not code_match:
        return "none", "unknown_error_code"
    if code_match:
        return "pass", "exact_code_match"
    if vector == "failed":
        return "error", "embedding_unavailable"
    if calibration is None:
        return "uncalibrated", "no_calibration"
    if chunk_count == 0 or top_cosine is None or top_cosine < calibration.tau_low:
        return "none", "below_threshold"
    if top_cosine < calibration.tau_high:
        return "weak", "weak_match"
    return "pass", "above_threshold"
