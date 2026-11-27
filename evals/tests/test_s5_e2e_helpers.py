"""What: offline tests for the S5 e2e module's helpers (reseed per case, the fallback badge check), with no stack.
Use when: CI and local runs; the e2e tests themselves are skipped unless EVALS_E2E=1.
Inputs: none. Output: pytest results. Side effects: none (subprocess, runner and graders are faked).
Role: keeps the e2e harness honest; both tests come from the live rerun after PRs #52 and #58.
"""
from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from evals.tests import test_s5_faults_e2e as s5


def test_play_reseeds_before_every_case(monkeypatch: pytest.MonkeyPatch) -> None:
    """What: _play reseeds core before each case, like the golden CLI. Use when: CI. Inputs: monkeypatch.
    Output: none. Side effects: none. Role: G-11 must not merge into G-10's open ticket (doc 07 s7 ticket merge)."""
    calls: list[str] = []
    monkeypatch.delenv("EVALS_E2E_OUT", raising=False)
    monkeypatch.delenv("EVALS_E2E_RESEED", raising=False)
    monkeypatch.setattr(s5.subprocess, "run", lambda *a, **k: calls.append("reseed") or SimpleNamespace(returncode=0))
    monkeypatch.setattr(s5, "run_case", lambda *a, **k: calls.append("run") or SimpleNamespace())
    monkeypatch.setattr(s5.graders, "grade_case", lambda *a, **k: SimpleNamespace(verdict="pass", checks=[]))
    stack: dict[str, Any] = {"cases": {"G-10": {"persona": "p"}, "G-11": {"persona": "p"}}, "personas": {"p": {}},
                             "agent": None, "store": None, "policy": None}
    s5._play(stack, "G-10")
    s5._play(stack, "G-11")
    assert calls == ["reseed", "run", "reseed", "run"]


def _model_step(model: str) -> dict[str, Any]:
    """What: a completed model step event. Use when: building fake turns. Inputs: model id. Output: event dict.
    Side effects: none. Role: helper."""
    return {"type": "step.completed", "kind": "model", "model": model, "status": "ok"}


def _badge() -> dict[str, Any]:
    """What: a model.fallback event for the llm_429 fault. Use when: building fake turns. Inputs: none.
    Output: event dict. Side effects: none. Role: helper."""
    return {"type": "model.fallback", "reason": "fault_llm_429", "from_model": "claude-sonnet-5-5", "to_model": "gpt-5.4-mini"}


def test_fallback_badges_one_per_model_call() -> None:
    """What: the fault fires on the primary for the whole turn (doc 07 s10), so each model call shows one badge.
    Use when: CI. Inputs: none. Output: none. Side effects: none. Role: a turn with 2 model calls has 2 badges."""
    two = [_badge(), _model_step("gpt-5.4-mini-2026-03-17"), _badge(), _model_step("gpt-5.4-mini-2026-03-17")]
    assert [b["reason"] for b in s5._fallback_badges(two)] == ["fault_llm_429"] * 2
    with pytest.raises(AssertionError):
        s5._fallback_badges([_badge(), _model_step("gpt-5.4-mini"), _model_step("gpt-5.4-mini")])
    with pytest.raises(AssertionError):
        s5._fallback_badges([_model_step("claude-sonnet-5-5")])
