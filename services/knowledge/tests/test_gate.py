"""What: tests for Gate 1: the verdict function (one table row per branch), the calibration loader, staleness, the
/v1/search confidence block with a calibration, and the /readyz model-id check.
Use when: CI. Inputs: fakes (no Postgres, no network). Output: pass or fail. Side effects: temp files only.
Role: CS-E-02.S3.T3 Done-when 'one table test per verdict branch passes'; doc 07 s4.1 verdict order, s13.3, s4.2 readyz."""
import json
from typing import Any

import pytest

from knowledge.gate import Calibration, CalibrationError, decide, is_stale, load_calibration
from tests.test_search import AUTH, FakeDb, FakeEmbedder, body, row, setup

CAL = Calibration(calibration_id="cal-2026-11-27-01", status="provisional", embedding_model="text-embedding-3-small",
                  corpus_version="cv123", tau_low=0.30, tau_high=0.45, k_default=5, notes="probe")


def call(**kw: Any):
    """What: call decide with healthy defaults. Use when: table rows. Inputs: overrides. Output: (verdict, reason).
    Side effects: none. Role: test helper."""
    args: dict[str, Any] = dict(vector="ok", keyword="ok", code_match=False, has_unknown_code=False, top_cosine=0.6, chunk_count=5,
                calibration=CAL, calibration_mode=False)
    args.update(kw)
    return decide(**args)


# One row per verdict branch of doc 07 s4.1, in the contract's order.
@pytest.mark.parametrize("kw,expected", [
    (dict(vector="failed", keyword="failed", top_cosine=None, chunk_count=0), ("error", "retrieval_unavailable")),
    (dict(has_unknown_code=True), ("none", "unknown_error_code")),
    (dict(code_match=True, top_cosine=0.1), ("pass", "exact_code_match")),
    (dict(vector="failed", top_cosine=None), ("error", "embedding_unavailable")),
    (dict(calibration=None), ("uncalibrated", "no_calibration")),
    (dict(calibration_mode=True), ("uncalibrated", "no_calibration")),
    (dict(calibration_mode=True, code_match=True), ("uncalibrated", "no_calibration")),
    (dict(calibration_mode=True, has_unknown_code=True), ("uncalibrated", "no_calibration")),
    (dict(chunk_count=0, top_cosine=None, vector="empty", keyword="empty"), ("none", "below_threshold")),
    (dict(top_cosine=0.29), ("none", "below_threshold")),
    (dict(top_cosine=0.30), ("weak", "weak_match")),
    (dict(top_cosine=0.449), ("weak", "weak_match")),
    (dict(top_cosine=0.45), ("pass", "above_threshold")),
    (dict(top_cosine=0.9), ("pass", "above_threshold")),
], ids=["both_failed", "unknown_code", "code_match", "embedding_outage", "no_file", "calibration_flag", "flag_beats_code", "flag_beats_unknown",
        "no_chunks", "below_low", "at_low", "below_high", "at_high", "high"])
def test_verdict_branches(kw, expected):
    """What: each branch of the verdict order returns its verdict and reason. Use when: CI. Inputs: one case per
    branch plus boundaries (low and high are inclusive upward). Output: assertion. Side effects: none.
    Role: doc 07 s4.1."""
    assert call(**kw) == expected


def test_unknown_code_beats_embedding_outage_and_code_match_beats_unknown():
    """What: precedence: an unknown code with no match is none even when the vector leg failed; a code match
    wins over an unknown code in the same query. Use when: CI. Inputs: two combos. Output: assertion.
    Side effects: none. Role: order in doc 07 s4.1."""
    assert call(has_unknown_code=True, vector="failed", top_cosine=None) == ("none", "unknown_error_code")
    assert call(has_unknown_code=True, code_match=True) == ("pass", "exact_code_match")


def write(tmp_path, **over):
    """What: write a calibration file. Use when: loader tests. Inputs: field overrides. Output: path.
    Side effects: file. Role: test helper."""
    data = {"calibration_id": "cal-2026-11-27-01", "status": "provisional", "embedding_model": "text-embedding-3-small",
            "corpus_version": "cv123", "tau_low": 0.3, "tau_high": 0.45, "k_default": 5, "recall_at_k": {},
            "train": {"n": 0, "confusion": {}}, "test": {"n": 0, "confusion": {}}, "created_at": "2026-11-27T07:00:00Z",
            "notes": "probe notes"}
    data.update(over)
    p = tmp_path / "cal.json"
    p.write_text(json.dumps(data))
    return p


def test_loader_reads_a_valid_file_and_missing_is_none(tmp_path):
    """What: a valid file loads; a missing file is None (verdict uncalibrated). Use when: CI. Inputs: temp
    files. Output: assertion. Side effects: none. Role: doc 07 s13.3, s4.2."""
    cal = load_calibration(write(tmp_path))
    assert cal is not None and cal.tau_low == 0.3 and cal.tau_high == 0.45 and cal.status == "provisional" and cal.k_default == 5
    assert load_calibration(tmp_path / "nope.json") is None


@pytest.mark.parametrize("over", [dict(status="guess"), dict(tau_low=0.5, tau_high=0.4), dict(tau_low=-0.1),
                                  dict(tau_high=1.5), dict(k_default=9), dict(embedding_model=""), dict(tau_low="x"),
                                  dict(corpus_version=""), dict(corpus_version=None), dict(k_default=5.9)])
def test_loader_fails_loudly_on_a_bad_file(tmp_path, over):
    """What: an invalid value raises CalibrationError instead of silently gating wrongly. Use when: CI.
    Inputs: ten bad files. Output: assertion. Side effects: none. Role: fail loudly."""
    with pytest.raises(CalibrationError):
        load_calibration(write(tmp_path, **over))


def test_loader_rejects_unparseable_json_and_missing_keys(tmp_path):
    """What: broken JSON and a missing tau raise CalibrationError. Use when: CI. Inputs: two files.
    Output: assertion. Side effects: none. Role: fail loudly."""
    p = tmp_path / "bad.json"
    p.write_text("{nope")
    with pytest.raises(CalibrationError):
        load_calibration(p)
    p.write_text(json.dumps({"status": "provisional"}))
    with pytest.raises(CalibrationError):
        load_calibration(p)


def test_staleness_is_a_corpus_version_mismatch():
    """What: stale when the file's corpus_version differs from the live one; not stale when equal or no file.
    Use when: CI. Inputs: three combos. Output: assertion. Side effects: none. Role: doc 07 s13.3."""
    assert is_stale(CAL, "other") is True and is_stale(CAL, "cv123") is False and is_stale(None, "x") is False


def test_endpoint_confidence_block_with_calibration(client):
    """What: /v1/search carries score, thresholds, verdict, calibration id and status; relevant uses tau_low;
    a stale corpus version sets calibration_stale. Use when: CI. Inputs: cosines 0.5, 0.35, 0.2. Output:
    assertion. Side effects: none. Role: doc 07 s4.1 response."""
    vec = [row("a#1", v_rank=1, cosine=0.5), row("b#1", v_rank=2, cosine=0.35), row("c#1", v_rank=3, cosine=0.2)]
    setup(client, FakeDb(vec, []), FakeEmbedder())
    client.app.state.calibration = CAL
    c = client.post("/v1/search", json=body(), headers=AUTH).json()["confidence"]
    assert (c["verdict"], c["reason"]) == ("pass", "above_threshold")
    assert c["top_cosine"] == 0.5 and c["tau_low"] == 0.30 and c["tau_high"] == 0.45
    assert c["calibration_id"] == "cal-2026-11-27-01" and c["calibration_status"] == "provisional"
    assert c["calibration_stale"] is False and c["support_count"] == 2
    j = client.post("/v1/search", json=body(), headers=AUTH).json()
    assert [ch["relevant"] for ch in j["chunks"]] == [True, True, False]
    client.app.state.calibration = Calibration(**{**CAL.__dict__, "corpus_version": "old"})
    assert client.post("/v1/search", json=body(), headers=AUTH).json()["confidence"]["calibration_stale"] is True


def test_endpoint_calibration_flag_returns_uncalibrated_with_scores(client):
    """What: calibration=true switches gating off: verdict uncalibrated, top_cosine still reported, every chunk
    shown. Use when: CI. Inputs: a low-cosine result. Output: assertion. Side effects: none.
    Role: the probe script path (doc 07 s4.1 calibration flag)."""
    setup(client, FakeDb([row("a#1", v_rank=1, cosine=0.1)], []), FakeEmbedder())
    client.app.state.calibration = CAL
    c = client.post("/v1/search", json=body(calibration=True), headers=AUTH).json()["confidence"]
    assert c["verdict"] == "uncalibrated" and c["top_cosine"] == 0.1


def test_endpoint_none_and_weak_rows(client):
    """What: an off-topic cosine gives none/below_threshold and a middling one weak/weak_match over HTTP.
    Use when: CI. Inputs: cosine 0.2 then 0.4. Output: assertion. Side effects: none. Role: wiring check."""
    client.app.state.calibration = CAL
    setup(client, FakeDb([row("a#1", v_rank=1, cosine=0.2)], []), FakeEmbedder())
    c = client.post("/v1/search", json=body(), headers=AUTH).json()["confidence"]
    assert (c["verdict"], c["reason"]) == ("none", "below_threshold")
    setup(client, FakeDb([row("a#1", v_rank=1, cosine=0.4)], []), FakeEmbedder())
    c = client.post("/v1/search", json=body(), headers=AUTH).json()["confidence"]
    assert (c["verdict"], c["reason"]) == ("weak", "weak_match")


def test_endpoint_unknown_code_is_none(client):
    """What: an unknown error code with no match is none/unknown_error_code even with high cosine. Use when: CI.
    Inputs: query with E-SSO-999. Output: assertion. Side effects: none. Role: doc 07 s4.1 order."""
    client.app.state.calibration = CAL
    setup(client, FakeDb([row("a#1", v_rank=1, cosine=0.9)], []), FakeEmbedder())
    c = client.post("/v1/search", json=body(query="what is E-SSO-999"), headers=AUTH).json()["confidence"]
    assert (c["verdict"], c["reason"]) == ("none", "unknown_error_code")


def test_readyz_fails_on_model_mismatch_and_reports_status(client):
    """What: /readyz is 503 when the calibration file's embedding model differs from the configured one; ready
    with calibration_status provisional or none otherwise. Use when: CI. Inputs: three calibrations.
    Output: assertion. Side effects: none. Role: doc 07 s4.2."""
    r = client.get("/readyz")
    assert r.status_code == 200 and r.json()["calibration_status"] == "none"
    client.app.state.calibration = CAL
    r = client.get("/readyz")
    assert r.status_code == 200 and r.json()["calibration_status"] == "provisional"
    client.app.state.calibration = Calibration(**{**CAL.__dict__, "embedding_model": "other-model"})
    assert client.get("/readyz").status_code == 503


def test_committed_calibration_file_is_provisional_with_probe_notes():
    """What: services/knowledge/config/retrieval_calibration.json loads, is provisional, matches the thresholds and the configured
    embedding model, holds a corpus_version and carries the probe notes. Use when: CI. Inputs: the committed file.
    Output: assertion. Side effects: none. Role: CS-E-02.S3.T3 Done-when (provisional status and probe notes)."""
    from tests.conftest import ROOT
    from knowledge import config

    cal = load_calibration(ROOT / "services" / "knowledge" / "config" / "retrieval_calibration.json")
    assert cal is not None
    assert cal.status == "provisional" and 0.0 < cal.tau_low < cal.tau_high < 1.0
    assert cal.embedding_model == config.DEFAULT_EMBEDDING_MODEL and len(cal.corpus_version) == 64
    for word in ("answerable", "off_topic", "not_in_docs", "tau_low", "tau_high", "S1"):
        assert word in cal.notes


def test_missing_calibration_file_is_loud(tmp_path, monkeypatch, caplog):
    """What: starting with no calibration file logs an ERROR naming the path, serves verdict uncalibrated and
    /readyz says calibration_status none; a loaded file logs one INFO line with id, status and both thresholds.
    Use when: CI. Inputs: a missing path, then a valid one. Output: assertion. Side effects: env, logs.
    Role: review PR26 M1: a missing file must never be silent (every verdict would become weak)."""
    import logging

    from fastapi.testclient import TestClient

    from knowledge.main import create_app

    monkeypatch.setenv("KNOWLEDGE_TOKEN_AGENT", "t" * 40)
    monkeypatch.setenv("KNOWLEDGE_DB_URL", "postgresql://unused")
    monkeypatch.setenv("CALIBRATION_PATH", str(tmp_path / "missing.json"))
    with caplog.at_level(logging.INFO, logger="knowledge"):
        with TestClient(create_app()) as c:
            assert c.app.state.calibration is None  # type: ignore[attr-defined]
    errs = [r for r in caplog.records if r.levelno == logging.ERROR and "missing.json" in r.getMessage()]
    assert errs and "uncalibrated" in errs[0].getMessage()
    caplog.clear()
    monkeypatch.setenv("CALIBRATION_PATH", str(write(tmp_path)))
    with caplog.at_level(logging.INFO, logger="knowledge"):
        with TestClient(create_app()):
            pass
    info = [r.getMessage() for r in caplog.records if "cal-2026-11-27-01" in r.getMessage()]
    assert info and "provisional" in info[0] and "0.3" in info[0] and "0.45" in info[0]
