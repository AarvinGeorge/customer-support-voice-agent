"""What: the two S5 fault scripts (doc 07 s10 "S5 scripts") plus the llm_429 fallback, end to end on a real Compose stack:
the browser-like evals client drives the agent API through Caddy, real core applies the tool_error fault, and the evidence is
read back with read-only SELECTs on core and agent Postgres. Cases G-10 (fault on the prepare turn), G-11 (fault on the yes
turn) and G-12 (llm_429 on the primary) are the shipped golden cases; each is graded by the golden graders and then checked
explicitly against the script wording.
Use when: proving CS-E-07.S1.T3 ("both scripts pass with DEMO_FAULTS_ENABLED=true") on a running stack. Skipped unless
EVALS_E2E=1 (CI never sets it, so CI stays free of real calls). About 7 real model calls per full run.
Inputs: env EVALS_E2E=1, DEMO_ACCESS_CODE, EVALS_AGENT_URL (Caddy, for example http://localhost:3085), EVALS_CORE_DB_URL and
EVALS_AGENT_DB_URL (host URLs of core_app and agent_app through infra/compose.pgtest.yaml), optional EVALS_E2E_OUT (a
directory for the trimmed SSE streams and rows) and EVALS_E2E_RESEED=0 to skip the one reseed at the start.
Output: pytest results. Side effects: real HTTP turns (Anthropic and OpenAI calls through the agent), one core reseed through
docker compose, SELECTs, optional JSON files. Role: the S5 acceptance test.

Command (from the repo root, stack up with DEMO_FAULTS_ENABLED=true, seeded and ingested; see the S5 report):
    EVALS_E2E=1 EVALS_AGENT_URL=http://localhost:$EDGE_PORT EVALS_CORE_DB_URL=... EVALS_AGENT_DB_URL=... \\
      uv run --project evals --frozen pytest evals/tests/test_s5_faults_e2e.py -m e2e -v
"""
from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any

import httpx
import pytest
from contracts.policy import load_policy

from evals import graders
from evals.agent_client import AgentClient
from evals.cases import ROOT, load_cases, load_personas
from evals.model import CaseRun
from evals.runner import run_case
from evals.store import PgStore, _connect

pytestmark = [pytest.mark.e2e,
              pytest.mark.skipif(os.environ.get("EVALS_E2E") != "1", reason="end-to-end: set EVALS_E2E=1 on a running stack")]

NOTHING_CHANGED = re.compile(r"nothing was changed", re.I)
TICKET_ENDING = re.compile(r"ticket,? ending (\d{4})", re.I)
RESEED = ["docker", "compose", "--profile", "seed", "run", "--rm", "-e", "DEMO_RESEED=true", "seed_core",
          "python", "-m", "core.seeds", "--reseed"]


def _env(name: str) -> str:
    """What: a required env value. Use when: fixtures start. Inputs: name. Output: value. Side effects: none.
    Role: fail loudly (never skip) when EVALS_E2E=1 but the stack is not described."""
    v = os.environ.get(name)
    if not v:
        pytest.fail(f"{name} must be set when EVALS_E2E=1")
    return v


@pytest.fixture(scope="module")
def stack() -> dict[str, Any]:
    """What: the client, store, cases, personas and policy for the live stack, after one reseed.
    Use when: every e2e test. Inputs: env (see module). Output: dict. Side effects: one reseed, one access call.
    Role: setup; checks /api/readyz reports demo_faults true, so a false flag fails instead of passing vacuously."""
    http = httpx.Client(base_url=_env("EVALS_AGENT_URL"), timeout=httpx.Timeout(90.0, connect=10.0))
    ready = http.get("/api/readyz")
    assert ready.status_code == 200, ready.text[:200]
    assert ready.json().get("demo_faults") is True, "the agent must run with DEMO_FAULTS_ENABLED=true"
    if os.environ.get("EVALS_E2E_RESEED", "1") != "0":
        r = subprocess.run(RESEED, cwd=ROOT, capture_output=True, text=True, timeout=300)
        assert r.returncode == 0, f"reseed failed: {r.stderr[-300:]}"
    agent = AgentClient(http)
    agent.access(_env("DEMO_ACCESS_CODE"))
    return {"agent": agent, "store": PgStore(_env("EVALS_CORE_DB_URL"), _env("EVALS_AGENT_DB_URL")),
            "cases": {c["id"]: c for c in load_cases()}, "personas": load_personas(),
            "policy": load_policy(ROOT / "data" / "policy.yaml"), "core_db": _env("EVALS_CORE_DB_URL")}


def _fault_audit(core_db: str, session_id: str) -> list[dict[str, Any]]:
    """What: core's fault_injected audit rows for a session. Use when: proving each faulted call reached core.
    Inputs: core URL, session id. Output: rows (ts order). Side effects: one SELECT on a read-only connection.
    Role: evidence that the retry happened at core, not only in the stream."""
    conn = _connect(core_db)  # the store's dict-row connector
    try:
        conn.read_only = True
        return list(conn.execute(
            "SELECT event, args_redacted->>'endpoint' AS endpoint, result_status, idempotency_key FROM audit_events "
            "WHERE session_id = %s AND event = 'fault_injected' ORDER BY ts", (session_id,)).fetchall())
    finally:
        conn.close()


def _trim(run: CaseRun) -> list[dict[str, Any]]:
    """What: the run's streams without token events (their text joined into one entry per turn).
    Use when: writing the evidence file. Inputs: run. Output: list per turn. Side effects: none. Role: report quoting."""
    out = []
    for t in run.turns:
        events, text = [], ""
        for e in t.events:
            if e.get("type") == "token":
                text += str(e.get("text") or e.get("delta") or "")
                continue
            events.append({k: v for k, v in e.items() if k not in ("session_id", "v", "ts")})
        out.append({"kind": t.kind, "status": t.status_code, "turn_id": t.turn_id, "tokens_text": text, "events": events})
    return out


def _play(stack: dict[str, Any], case_id: str) -> tuple[CaseRun, Any]:
    """What: run and grade one golden case, and save the evidence. Use when: each test. Inputs: stack, case id.
    Output: (run, grade). Side effects: the case's HTTP turns, SELECTs, an optional JSON file. Role: shared driver."""
    case = stack["cases"][case_id]
    run = run_case(case, stack["personas"][case["persona"]], stack["agent"], stack["store"])
    grade = graders.grade_case(case, run, stack["policy"])
    out = os.environ.get("EVALS_E2E_OUT")
    if out:
        Path(out).mkdir(parents=True, exist_ok=True)
        data = run.data
        rows = {} if data is None else {"pending": data.pending, "refunds": data.refunds, "tickets": data.tickets}
        audit = _fault_audit(stack["core_db"], run.session_id) if run.session_id else []
        Path(out, f"{case_id}.json").write_text(json.dumps(
            {"session_id": run.session_id, "error": run.error, "verdict": grade.verdict,
             "checks": [c.__dict__ for c in grade.checks], "turns": _trim(run), "rows": rows, "fault_audit": audit},
            indent=1, default=str))
    return run, grade


def _assert_pass(grade: Any) -> None:
    """What: the golden graders all passed. Use when: each test. Inputs: grade. Output: none; raises on fail or error.
    Side effects: none. Role: reuse the golden-set expectations."""
    bad = [f"{c.name}: {c.status} {c.detail}" for c in grade.checks if c.status != "pass"]
    assert grade.verdict == "pass", bad


def _types(run: CaseRun, index: int) -> list[str]:
    """What: the event types of one turn. Use when: asserting a sequence. Inputs: run, turn index. Output: types.
    Side effects: none. Role: helper."""
    return [str(e.get("type")) for e in run.turns[index].events]


def _ticket_ending(run: CaseRun) -> str:
    """What: the final line's "ticket ending NNNN" digits, checked against the one ticket core stored.
    Use when: (a) and (b). Inputs: run. Output: the 4 digits. Side effects: none. Role: the ticket the customer heard exists."""
    final = run.turns[-1].final or {}
    m = TICKET_ENDING.search(str(final.get("text")))
    assert m, final.get("text")
    assert run.data is not None and len(run.data.tickets) == 1
    assert str(run.data.tickets[0]["id"]).endswith(m.group(1))
    return m.group(1)


def test_s5_tool_error_on_the_prepare_turn(stack: dict[str, Any]) -> None:
    """What: script (a). Use when: e2e. Inputs: stack. Output: none. Side effects: 2 turns (card, faulted request).
    Role: prepare 503, one retry 503, escalate tool_failure, ticket, "nothing was changed", no prepared or committed row."""
    run, grade = _play(stack, "G-10")
    _assert_pass(grade)
    last = run.turns[-1]
    assert last.end_reason == "escalated"
    assert NOTHING_CHANGED.search(str((last.final or {}).get("text")))
    _ticket_ending(run)
    prep = [e for e in last.events if e.get("type") == "step.completed" and e.get("title", "").startswith("Refund prep")]
    assert [e["status"] for e in prep] == ["retrying", "error"], prep
    assert "status" in _types(run, len(run.turns) - 1)  # "Retrying the billing system..."
    assert run.data is not None
    assert [p for p in run.data.pending if p["state"] in ("prepared", "committed")] == []
    assert run.data.refunds == []
    audit = _fault_audit(stack["core_db"], str(run.session_id))
    assert [a["endpoint"] for a in audit] == ["POST /v1/actions/refund/prepare"] * 2, audit


def test_s5_tool_error_on_the_yes_turn(stack: dict[str, Any]) -> None:
    """What: script (b). Use when: e2e. Inputs: stack. Output: none. Side effects: 3 turns (request, card, faulted Confirm).
    Role: commit 503, status check shows prepared, one retry 503, ticket, "no charge was changed" wording
    ("Our billing system didn't respond, so nothing was changed."), the row stays prepared, nothing committed."""
    run, grade = _play(stack, "G-11")
    _assert_pass(grade)
    last = run.turns[-1]
    assert last.end_reason == "escalated"
    assert NOTHING_CHANGED.search(str((last.final or {}).get("text")))
    _ticket_ending(run)
    result = [e for e in last.events if e.get("type") == "action.result"]
    assert len(result) == 1 and result[0]["outcome"] == "failed" and result[0]["changed"] is False, result
    assert run.data is not None
    assert [p["state"] for p in run.data.pending] == ["prepared"]
    assert run.data.refunds == []
    audit = _fault_audit(stack["core_db"], str(run.session_id))
    pid = run.data.pending[0]["id"]
    assert [(a["endpoint"], a["idempotency_key"]) for a in audit] == \
        [("POST /v1/actions/pending/{pending_id}/commit", pid)] * 2, audit


def test_s5_llm_429_goes_to_the_fallback(stack: dict[str, Any]) -> None:
    """What: script (c). Use when: e2e. Inputs: stack. Output: none. Side effects: 1 turn. Role: primary 429 (injected),
    model.fallback reason fault_llm_429 (the badge), the answer comes from the fallback model. G-12's citation check
    is left to the golden runner: citations come from Gate 2 (GroundingMiddleware, PR #52), not from the fallback path."""
    run, grade = _play(stack, "G-12")
    want = ("outcome", "end_reason", "fallback", "trajectory", "must_mention", "final_state.tickets", "final_state.refunds")
    by = {c.name: c.status for c in grade.checks}
    assert {k: by.get(k) for k in want} == dict.fromkeys(want, "pass"), grade.checks
    t = run.turns[0]
    assert t.end_reason == "answered"
    assert "24 hours" in str((t.final or {}).get("text"))
    fb = [e for e in t.events if e.get("type") == "model.fallback"]
    assert len(fb) == 1 and fb[0]["reason"] == "fault_llm_429", fb
    usage = (t.completed or {})["usage"]
    assert usage["fallback_used"] is True
    assert usage["models_used"] and all(m.startswith(fb[0]["to_model"]) for m in usage["models_used"]), usage
    assert str((t.final or {}).get("text")).strip()
