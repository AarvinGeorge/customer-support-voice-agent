"""What: tests for the models.yaml loader. Use when: pytest. Inputs: none. Output: pass/fail. Side effects: temp files. Role: test."""
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from agent.models.config import REQUIRED_ROLES, load_models_config

REPO_YAML = Path(__file__).resolve().parents[3] / "data" / "models.yaml"


def _doc():
    return yaml.safe_load(REPO_YAML.read_text())


def _write(tmp_path, doc):
    p = tmp_path / "models.yaml"
    p.write_text(yaml.safe_dump(doc))
    return p


def test_repo_yaml_loads_and_has_every_doc07_role():
    cfg = load_models_config(REPO_YAML)
    assert set(cfg.roles) >= set(REQUIRED_ROLES)
    assert cfg.roles["agent"].model == "claude-sonnet-5-5"
    assert cfg.roles["agent"].provider == "anthropic"
    assert cfg.roles["agent_fallback"].provider == "openai"
    assert cfg.roles["grounding_verifier"].attempt_timeout_s == 3
    assert cfg.roles["scope_classifier"].attempt_timeout_s == 2


def test_every_price_block_has_as_of():
    cfg = load_models_config(REPO_YAML)
    for name, role in cfg.roles.items():
        assert role.price is not None, name
        assert role.price.as_of, name


def test_voice_block_from_lane_c_is_preserved():
    v = load_models_config(REPO_YAML).roles["voice"]
    assert (v.model, v.voice, v.transcription_model) == ("gpt-realtime-2.1", "marin", "gpt-transcribe")


def test_unknown_top_level_key_fails(tmp_path):
    d = _doc()
    d["surprise"] = 1
    with pytest.raises(ValidationError):
        load_models_config(_write(tmp_path, d))


def test_unknown_role_key_fails(tmp_path):
    d = _doc()
    d["roles"]["agent"]["temprature"] = 0
    with pytest.raises(ValidationError):
        load_models_config(_write(tmp_path, d))


def test_unknown_price_key_fails(tmp_path):
    d = _doc()
    d["roles"]["agent"]["price"]["inptu"] = 1
    with pytest.raises(ValidationError):
        load_models_config(_write(tmp_path, d))


def test_missing_required_role_fails(tmp_path):
    d = _doc()
    del d["roles"]["agent_fallback"]
    with pytest.raises(ValidationError, match="agent_fallback"):
        load_models_config(_write(tmp_path, d))


def test_agent_roles_require_first_token_timeout(tmp_path):
    d = _doc()
    del d["roles"]["agent"]["first_token_timeout_s"]
    with pytest.raises(ValidationError, match="first_token_timeout_s"):
        load_models_config(_write(tmp_path, d))


def test_first_token_timeout_cannot_exceed_attempt_timeout(tmp_path):
    d = _doc()
    d["roles"]["agent"]["first_token_timeout_s"] = 99
    with pytest.raises(ValidationError):
        load_models_config(_write(tmp_path, d))


def test_bad_provider_and_bad_date_fail(tmp_path):
    d = _doc()
    d["roles"]["agent"]["provider"] = "google"
    with pytest.raises(ValidationError):
        load_models_config(_write(tmp_path, d))
    d = _doc()
    d["roles"]["agent"]["price"]["as_of"] = "27/11/2026"
    with pytest.raises(ValidationError):
        load_models_config(_write(tmp_path, d))


def test_missing_file_fails_loudly(tmp_path):
    with pytest.raises(FileNotFoundError):
        load_models_config(tmp_path / "nope.yaml")


def test_effort_none_is_rejected_on_an_anthropic_role(tmp_path):
    d = _doc()
    assert d["roles"]["agent"]["provider"] == "anthropic"
    d["roles"]["agent"]["effort"] = "none"
    with pytest.raises(ValidationError, match="effort none is OpenAI only"):
        load_models_config(_write(tmp_path, d))
