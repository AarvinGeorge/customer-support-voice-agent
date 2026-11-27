"""What: the two S5 fault scripts (doc 07 s10 "S5 scripts") plus the llm_429 fallback, end to end on a real Compose stack:
the browser-like evals client drives the agent API through Caddy, real core applies the tool_error fault, and the evidence is
read back with read-only SELECTs on core and agent Postgres. Cases G-10 (fault on the prepare turn), G-11 (fault on the yes
turn) and G-12 (llm_429 on the primary) are the shipped golden cases; each is graded by the golden graders and then checked
explicitly against the script wording.
Use when: proving CS-E-07.S1.T3 ("both scripts pass with DEMO_FAULTS_ENABLED=true") on a running stack. Skipped unless
EVALS_E2E=1 (CI never sets it, so CI stays free of real calls). About 12 real model calls per full run (measured: 10 agent
calls plus 2 Gate 2 verifier calls).
Inputs: env EVALS_E2E=1, DEMO_ACCESS_CODE, EVALS_AGENT_URL (Caddy on loopback, for example http://localhost:3085),
COMPOSE_PROJECT_NAME (the stack under test, a lane name `cs-...`), DEMO_FAULTS_ENABLED=true, EVALS_CORE_DB_URL and
EVALS_AGENT_DB_URL (host URLs of core_app and agent_app through infra/compose.pgtest.yaml), optional EVALS_E2E_OUT (a
directory for the trimmed SSE streams and rows) and EVALS_E2E_RESEED=0 to skip the reseed before each case.
Output: pytest results. Side effects: real HTTP turns (Anthropic and OpenAI calls through the agent), one core reseed through
docker compose before each case (as the golden CLI does), SELECTs, optional JSON files. Role: the S5 acceptance test.

Reseed guard (the reseed TRUNCATEs core tables): before each reseed, reseed_command refuses unless COMPOSE_PROJECT_NAME
is a lane name (`cs-...`, never one naming production), DEMO_FAULTS_ENABLED=true, the checkout has no `.deploy` (the
VM marker: deploy.sh's state dir), EVALS_AGENT_URL is loopback with a port, and that project's Caddy publishes that
port. The reseed is pinned with `-p`, and afterwards the `reseed` audit row in EVALS_CORE_DB_URL must be newer.

Command (from the repo root, stack up with DEMO_FAULTS_ENABLED=true, seeded and ingested; see the S5 report):
    export COMPOSE_PROJECT_NAME=cs-a5 COMPOSE_FILE=compose.yaml:compose.override.yaml:infra/compose.pgtest.yaml \\
           EDGE_SUBNET=172.30.95.0/24 EDGE_PORT=3095 PG_TEST_PORT=55495 DEMO_FAULTS_ENABLED=true
    EVALS_E2E=1 EVALS_AGENT_URL=http://localhost:$EDGE_PORT DEMO_ACCESS_CODE=... EVALS_CORE_DB_URL=... \\
      EVALS_AGENT_DB_URL=... uv run --project evals --frozen pytest evals/tests/test_s5_faults_e2e.py -m e2e -v
"""
from __future__ import annotations

import json
import os
import re
import subprocess
from collections.abc import Callable, Iterator, Mapping
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

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
TICKET_ENDING = re.compile(r"ticket,? ending (\d{4})\b", re.I)
RESEED_ARGS = ["--profile", "seed", "run", "--rm", "-e", "DEMO_RESEED=true", "seed_core",
               "python", "-m", "core.seeds", "--reseed"]
LANE_PROJECT = re.compile(r"^cs-[a-z0-9]+(?:-[a-z0-9]+)*$")
LOOPBACK = frozenset({"localhost", "127.0.0.1", "::1"})


class ReseedRefused(RuntimeError):
    """What: the reseed target is not provably the stack under test. Use when: reseed_command refuses.
    Inputs: message. Output: exception. Side effects: none. Role: the reseed TRUNCATEs core; refuse rather than guess."""


def reseed_command(env: Mapping[str, str], agent_url: str, root: Path,
                   run: Callable[..., Any] = subprocess.run) -> list[str]:
    """What: the reseed command pinned to the stack under test, after checking that it is that stack.
    Use when: before every reseed. Inputs: env, EVALS_AGENT_URL, repo root, a subprocess.run (faked in tests).
    Output: the docker compose argv with `-p <project>`. Side effects: one read-only `docker compose port` call,
    only after the env checks pass. Role: review I-1; raises ReseedRefused with the reason."""
    project = env.get("COMPOSE_PROJECT_NAME", "")
    if not project:
        raise ReseedRefused("COMPOSE_PROJECT_NAME must name the stack under test (for example cs-a5)")
    if not LANE_PROJECT.match(project):
        raise ReseedRefused(f"COMPOSE_PROJECT_NAME={project!r} is not a lane stack name (cs-...)")
    if "prod" in project:
        raise ReseedRefused(f"COMPOSE_PROJECT_NAME={project!r} names production; never reseed it")
    if env.get("DEMO_FAULTS_ENABLED", "").lower() != "true":
        raise ReseedRefused("DEMO_FAULTS_ENABLED=true must be set for the stack under test")
    if (root / ".deploy").exists():
        raise ReseedRefused(f"{root / '.deploy'} exists: this checkout is a deploy target (the VM); never reseed it")
    url = urlsplit(agent_url)
    if url.scheme != "http" or url.hostname not in LOOPBACK or url.port is None:
        raise ReseedRefused(f"EVALS_AGENT_URL={agent_url!r} must be http on loopback with the stack's EDGE_PORT")
    r = run(["docker", "compose", "-p", project, "port", "caddy", "80"], cwd=root, capture_output=True, text=True,
            timeout=60)
    published = (r.stdout or "").strip().splitlines()
    ports = {p.rsplit(":", 1)[-1] for p in published if p.rsplit(":", 1)[0] in {"127.0.0.1", "[::1]", "::1"}}
    if r.returncode != 0 or str(url.port) not in ports:
        raise ReseedRefused(f"project {project} does not serve EVALS_AGENT_URL on loopback port {url.port} "
                            f"(its Caddy publishes {published or 'nothing'})")
    return ["docker", "compose", "-p", project, *RESEED_ARGS]


def _env(name: str) -> str:
    """What: a required env value. Use when: fixtures start. Inputs: name. Output: value. Side effects: none.
    Role: fail loudly (never skip) when EVALS_E2E=1 but the stack is not described."""
    v = os.environ.get(name)
    if not v:
        pytest.fail(f"{name} must be set when EVALS_E2E=1")
    return v


@pytest.fixture(scope="module")
def stack() -> Iterator[dict[str, Any]]:
    """What: the client, store, cases, personas and policy for the live stack.
    Use when: every e2e test. Inputs: env (see module). Output: dict. Side effects: one access call; closes the
    HTTP client at teardown. Role: setup; checks /api/readyz reports demo_faults true, so a false flag fails
    instead of passing vacuously (and the reseed guard never runs against a stack with faults off)."""
    http = httpx.Client(base_url=_env("EVALS_AGENT_URL"), timeout=httpx.Timeout(90.0, connect=10.0))
    ready = http.get("/api/readyz")
    assert ready.status_code == 200, ready.text[:200]
    assert ready.json().get("demo_faults") is True, "the agent must run with DEMO_FAULTS_ENABLED=true"
    agent = AgentClient(http)
    agent.access(_env("DEMO_ACCESS_CODE"))
    try:
        yield {"agent": agent, "store": PgStore(_env("EVALS_CORE_DB_URL"), _env("EVALS_AGENT_DB_URL")),
               "cases": {c["id"]: c for c in load_cases()}, "personas": load_personas(),
               "policy": load_policy(ROOT / "data" / "policy.yaml"), "core_db": _env("EVALS_CORE_DB_URL"),
               "agent_url": _env("EVALS_AGENT_URL")}
    finally:
        http.close()


def _last_reseed_ts(core_db: str) -> Any:
    """What: the newest `reseed` audit row's ts in the graded core database. Use when: around each reseed.
    Inputs: core URL. Output: ts or None. Side effects: one SELECT on a read-only connection.
    Role: proves the reseeded database is the one the tests read."""
    conn = _connect(core_db)
    try:
        conn.read_only = True
        row = conn.execute("SELECT max(ts) AS ts FROM audit_events WHERE event = 'reseed'").fetchone()
        return None if row is None else row["ts"]
    finally:
        conn.close()


def _reseed(stack: dict[str, Any]) -> None:
    """What: reseed core with the DEMO_RESEED CLI, unless EVALS_E2E_RESEED=0, after reseed_command's guard.
    Use when: before each case. Inputs: stack (agent_url, core_db), env. Output: none; raises ReseedRefused or
    AssertionError. Side effects: one guarded docker compose run (TRUNCATE in that project's core), two SELECTs.
    Role: each script starts clean; verified tickets merge by account and category (doc 07 s7), so without it
    G-11's failure ticket merges into G-10's open ticket and G-11's session shows none."""
    if os.environ.get("EVALS_E2E_RESEED", "1") == "0":
        return
    cmd = reseed_command(os.environ, stack["agent_url"], ROOT)
    before = _last_reseed_ts(stack["core_db"])
    r = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, timeout=300)
    assert r.returncode == 0, f"reseed failed: {r.stderr[-300:]}"
    after = _last_reseed_ts(stack["core_db"])
    assert after is not None and (before is None or after > before), \
        "the reseed did not reach the database in EVALS_CORE_DB_URL: it and COMPOSE_PROJECT_NAME name different stacks"


def _fallback_badges(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """What: the turn's model.fallback events, checked as one per model call, each with reason fault_llm_429.
    Use when: script (c). Inputs: one turn's events. Output: the badge events. Side effects: none.
    Role: llm_429 fires on the primary for the whole turn (doc 07 s10), so a turn with N model calls shows N badges."""
    fb = [e for e in events if e.get("type") == "model.fallback"]
    model_steps = [e for e in events if e.get("type") == "step.completed" and e.get("model")]
    assert fb and len(fb) == len(model_steps), (fb, model_steps)
    assert all(e.get("reason") == "fault_llm_429" for e in fb), fb
    assert all(str(m["model"]).startswith(str(b.get("to_model"))) for b, m in zip(fb, model_steps)), (fb, model_steps)
    return fb


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
    Output: (run, grade). Side effects: a reseed, the case's HTTP turns, SELECTs, an optional JSON file. Role: shared driver."""
    case = stack["cases"][case_id]
    _reseed(stack)
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
    assert any(e.get("type") == "status" and e.get("kind") == "retrying" for e in last.events), last.events
    assert run.data is not None
    assert [p for p in run.data.pending if p["state"] in ("prepared", "committed")] == []
    assert run.data.refunds == []
    audit = _fault_audit(stack["core_db"], str(run.session_id))
    assert [a["endpoint"] for a in audit] == ["POST /v1/actions/refund/prepare"] * 2, audit


def test_s5_tool_error_on_the_yes_turn(stack: dict[str, Any]) -> None:
    """What: script (b). Use when: e2e. Inputs: stack. Output: none. Side effects: 3 turns (request, card, faulted Confirm).
    Role: commit 503, status check shows prepared, one retry 503, ticket, "no charge was changed" wording
    ("Our billing system didn't respond, so nothing was changed."), the row stays prepared, nothing committed.
    The status check (s10 "status prepared") is inferred, not observed: core does not audit the status read, and
    the second commit audit row exists only because the Confirmer saw `prepared` and retried."""
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
    model.fallback reason fault_llm_429 (the badge, one per model call), the answer comes from the fallback model, and
    the full golden grade passes, citations included (Gate 2 grounds the fallback's answer too)."""
    run, grade = _play(stack, "G-12")
    _assert_pass(grade)
    t = run.turns[0]
    assert t.end_reason == "answered"
    assert "24 hours" in str((t.final or {}).get("text"))
    fb = _fallback_badges(t.events)
    usage = (t.completed or {})["usage"]
    assert usage["fallback_used"] is True
    assert usage["models_used"] and all(m.startswith(fb[0]["to_model"]) for m in usage["models_used"]), usage
    assert str((t.final or {}).get("text")).strip()
