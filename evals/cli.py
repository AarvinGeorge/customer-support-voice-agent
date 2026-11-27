"""What: the command line. `python -m evals run`: load the dataset, reseed per case, drive the agent API, grade with code graders,
print Gate 1 counts, write the dated report and exit 0 only when every graded case passed. `python -m evals smoke`: the live
smoke subset (evals.smoke) under a real-call cap, refused unless RUN_REAL=1; scripts/live_smoke.sh runs it on a throwaway stack.
Use when: a measured run is wanted (against a running stack, or against a test double in CI).
Inputs: flags below and env DEMO_ACCESS_CODE, EVALS_AGENT_URL (default http://localhost:3000), EVALS_KNOWLEDGE_URL,
KNOWLEDGE_TOKEN_AGENT, EVALS_CORE_DB_URL or CORE_DB_URL, EVALS_AGENT_DB_URL or AGENT_DB_URL, COMPOSE_PROJECT_NAME and
DEMO_FAULTS_ENABLED=true (the reseed guard, evals.reseed), EVALS_FIXTURE_INGEST_CMD and EVALS_FIXTURE_REMOVE_CMD
(templates with {path}). Output: report files, a summary on stdout.
Side effects: HTTP, Postgres SELECTs, guarded reseed subprocesses (one per case, pinned to the stack under test), files. Role: single entry point (doc 09 s5 CS-E-06.S1.T1).
Exit codes: 0 all passed, 1 some case not passed, 2 usage or configuration problem (nothing was run), 3 smoke aborted at the cap."""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

import httpx
from contracts.policy import load_policy

from . import gate1, report, runner, smoke
from .agent_client import AgentClient, AgentError
from .cases import ROOT, DatasetError, load_cases, load_personas, load_retrieval
from . import reseed
from .hooks import make_hooks
from .store import PgStore


def _parser() -> argparse.ArgumentParser:
    """What: the argument parser. Use when: main starts. Inputs: none. Output: parser. Side effects: none. Role: CLI surface."""
    p = argparse.ArgumentParser(prog="python -m evals")
    sub = p.add_subparsers(dest="command", required=True)
    r = sub.add_parser("run", help="run the golden cases and Gate 1 counts")
    r.add_argument("--cases", help="comma-separated case ids, for example G-01,G-06")
    r.add_argument("--split", choices=["train", "test"])
    r.add_argument("--skip-gate1", action="store_true")
    r.add_argument("--out-dir", default=str(ROOT / "evals" / "reports"))
    r.add_argument("--agent-url")
    r.add_argument("--knowledge-url")
    s = sub.add_parser("smoke", help="live smoke: the fixed agent-behaviour subset on a throwaway stack (RUN_REAL=1)")
    s.add_argument("--cap", type=int, help=f"hard cap on real model calls (default SMOKE_MAX_CALLS or {smoke.DEFAULT_CAP})")
    s.add_argument("--out-dir", help="where the summary and the JSON go (default: no files)")
    s.add_argument("--agent-url")
    return p


class _Setup:
    """What: what both commands need: dataset, agent URL and client, store, hooks. Use when: main.
    Inputs: filled by _wire. Output: the record. Side effects: none. Role: one wiring path for run and smoke."""

    def __init__(self, cases: list[dict[str, Any]], personas: dict[str, dict[str, Any]], agent_url: str,
                 agent_http: httpx.Client, store: Any, before: Any, after: Any) -> None:
        """What: keep the parts. Use when: _wire. Inputs: see class. Output: none. Side effects: none. Role: init."""
        self.cases, self.personas, self.agent_url, self.agent_http = cases, personas, agent_url, agent_http
        self.store, self.before, self.after = store, before, after


def _wire(args: argparse.Namespace, e: Mapping[str, str], agent_http: httpx.Client | None, store: Any,
          before_case: Callable[[dict[str, Any]], None] | None,
          after_case: Callable[[dict[str, Any]], None] | None) -> _Setup | int:
    """What: load the dataset and build the client, store and guarded reseed hooks from env unless injected.
    Use when: both commands. Inputs: args, env, optional doubles. Output: _Setup, or exit code 2 with the reason on
    stderr. Side effects: one read-only reseed guard check (docker compose port) when the real hooks are built.
    Role: shared wiring, so the smoke uses the golden CLI's reseed guard unchanged."""
    try:
        all_cases = load_cases()
        personas = load_personas()
    except DatasetError as ex:
        print(f"dataset error: {ex}", file=sys.stderr)
        return 2
    agent_url = args.agent_url or e.get("EVALS_AGENT_URL", "http://localhost:3000")
    if agent_http is None:
        agent_http = httpx.Client(base_url=agent_url, timeout=httpx.Timeout(90.0, connect=10.0))
    core_db = e.get("EVALS_CORE_DB_URL") or e.get("CORE_DB_URL")
    agent_db = e.get("EVALS_AGENT_DB_URL") or e.get("AGENT_DB_URL")
    if store is None or before_case is None:
        if not core_db or not agent_db:
            print("CORE_DB_URL and AGENT_DB_URL (or the EVALS_ overrides) are required to read rows and turn_events", file=sys.stderr)
            return 2
    if store is None:
        assert core_db and agent_db
        store = PgStore(core_db, agent_db)
    if before_case is None:
        assert core_db
        try:
            reseed.reseed_command(e, agent_url, ROOT)  # refuse up front, before any turn or truncate
        except reseed.ReseedRefused as ex:
            print(f"refusing to reseed: {ex}", file=sys.stderr)
            return 2
        db = core_db

        def guarded() -> None:
            """What: the shared guarded reseed for this stack. Use when: before each case. Inputs: none. Output: none.
            Side effects: see evals.reseed.guarded_reseed. Role: one guard for the CLI and the e2e tests."""
            reseed.guarded_reseed(e, agent_url, db, ROOT)
        before_case, after_case = make_hooks(guarded, e.get("EVALS_FIXTURE_INGEST_CMD"), e.get("EVALS_FIXTURE_REMOVE_CMD"))
    return _Setup(all_cases, personas, agent_url, agent_http, store, before_case, after_case)


def main(argv: Sequence[str] | None = None, env: Mapping[str, str] | None = None, *, agent_http: httpx.Client | None = None,
         store: Any = None, search: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
         before_case: Callable[[dict[str, Any]], None] | None = None, after_case: Callable[[dict[str, Any]], None] | None = None,
         now: dt.datetime | None = None, log_run: Callable[..., Any] | None = None) -> int:
    """What: run the command. Use when: executed, or called by tests with doubles. Inputs: argv, env, and optional injected
    agent_http, store, search, hooks, clock and log reader (tests). Output: exit code. Side effects: see module.
    Role: wires real clients from env unless doubles are injected."""
    args = _parser().parse_args(list(sys.argv[1:] if argv is None else argv))
    e = os.environ if env is None else env
    when = now or dt.datetime.now(dt.UTC)
    if args.command == "smoke" and e.get("RUN_REAL") != "1":
        print("refusing: the live smoke makes real model calls; set RUN_REAL=1 (scripts/live_smoke.sh)", file=sys.stderr)
        return 2
    code = e.get("DEMO_ACCESS_CODE")
    if not code:
        print("DEMO_ACCESS_CODE is not set: cannot open the agent API", file=sys.stderr)
        return 2
    only: list[str] | None = None
    queries: list[dict[str, Any]] = []
    if args.command == "run":
        try:
            queries = load_retrieval()
        except DatasetError as ex:
            print(f"dataset error: {ex}", file=sys.stderr)
            return 2
        only = args.cases.split(",") if args.cases else None
        if only and not set(only) <= {c["id"] for c in load_cases()}:
            print(f"unknown case ids: {sorted(set(only) - {c['id'] for c in load_cases()})}", file=sys.stderr)
            return 2
        if search is None and not args.skip_gate1:
            k_url, k_tok = args.knowledge_url or e.get("EVALS_KNOWLEDGE_URL"), e.get("KNOWLEDGE_TOKEN_AGENT")
            if not k_url or not k_tok:
                print("EVALS_KNOWLEDGE_URL and KNOWLEDGE_TOKEN_AGENT are required for Gate 1 (or pass --skip-gate1)", file=sys.stderr)
                return 2
            search = gate1.KnowledgeClient(httpx.Client(base_url=k_url, timeout=10.0), k_tok).search
    wired = _wire(args, e, agent_http, store, before_case, after_case)
    if isinstance(wired, int):
        return wired
    if args.command == "smoke":
        return _smoke(args, e, wired, code, when, log_run)
    agent = AgentClient(wired.agent_http)
    try:
        agent.access(code)
    except AgentError as ex:
        print(f"cannot start: {ex}", file=sys.stderr)
        return 2
    policy = load_policy(ROOT / "data" / "policy.yaml")
    result = runner.run_cases(wired.cases, wired.personas, agent, wired.store, policy, only=only, split=args.split,
                              before_case=wired.before, after_case=wired.after)
    g1 = None if args.skip_gate1 or search is None else gate1.run_gate1(queries, search)
    rep = report.build_report(result.grades, g1, {"agent_url": wired.agent_url, "cases_selected": [g.case_id for g in result.grades]}, when)
    md, js = report.write_report(rep, Path(args.out_dir), when)
    v = rep["cases"]["verdicts"]
    print(f"{rep['cases']['total']} cases: {v['pass']} pass, {v['fail']} fail, {v['error']} error. Report: {md} and {js}")
    return 0 if v["fail"] == 0 and v["error"] == 0 and rep["cases"]["total"] > 0 else 1


def _smoke(args: argparse.Namespace, e: Mapping[str, str], w: _Setup, code: str, when: dt.datetime,
           log_run: Callable[..., Any] | None) -> int:
    """What: the live smoke: each smoke case through the golden runner on a CappedAgent, graded by the golden graders
    and evals.smoke.live_checks, then the log counts and the one-screen summary. Use when: `python -m evals smoke`.
    Inputs: args, env (SMOKE_MAX_CALLS, COMPOSE_PROJECT_NAME, EDGE_PORT, SMOKE_COMMIT, SMOKE_DIRTY,
    SMOKE_INGEST_EMBEDDING_CALLS), wiring, access code, clock, log reader (tests). Output: 0 PASS, 1 FAIL, 3 aborted
    at the cap, 2 setup. Side effects: real turns, reseeds, log reads, optional files. Role: the smoke gate."""
    cap = args.cap if args.cap is not None else int(e.get("SMOKE_MAX_CALLS") or smoke.DEFAULT_CAP)
    agent = smoke.CappedAgent(w.agent_http, cap)
    try:
        agent.access(code)
    except AgentError as ex:
        print(f"cannot start: {ex}", file=sys.stderr)
        return 2
    policy = load_policy(ROOT / "data" / "policy.yaml")
    rows: list[smoke.SmokeRow] = []
    abort: str | None = None
    for label, case, gates in smoke.smoke_cases(w.cases):
        agent.label = label
        n0 = len(agent.results)
        try:
            result = runner.run_cases([case], w.personas, agent, w.store, policy, before_case=w.before, after_case=w.after)
        except smoke.BudgetExceeded as ex:
            abort = str(ex)
            turns = [t for _, t in agent.results[n0:]]
            rows.append(smoke.SmokeRow(label, None, smoke.live_checks(case, turns, gates), [str(t.end_reason) for t in turns],
                                       sum(c["total"] for lb, _, c in agent.ledger if lb == label)))
            break
        run, grade = result.runs[0], result.grades[0]
        rows.append(smoke.SmokeRow(label, grade, smoke.live_checks(case, run.turns, gates),
                                   [str(t.end_reason) for t in run.turns],
                                   sum(c["total"] for lb, _, c in agent.ledger if lb == label)))
    project = e.get("COMPOSE_PROJECT_NAME", "")
    try:
        logs = smoke.log_findings(project, **({"run": log_run} if log_run else {}))
    except (RuntimeError, OSError) as ex:
        print(f"log read failed: {ex}", file=sys.stderr)
        logs = None
    cost = sum(float(((t.completed or {}).get("usage") or {}).get("cost_usd_est") or 0) for _, t in agent.results)
    models = sorted({m for _, t in agent.results for m in (((t.completed or {}).get("usage") or {}).get("models_used") or [])})
    meta = {"when": when.strftime("%Y-%m-%dT%H:%MZ"), "project": project, "edge_port": e.get("EDGE_PORT", "?"),
            "commit": e.get("SMOKE_COMMIT", "unknown"), "dirty": e.get("SMOKE_DIRTY") == "1",
            "ingest_embedding_calls": e.get("SMOKE_INGEST_EMBEDDING_CALLS", "0"), "cost": cost, "models": ",".join(models)}
    text, passed = smoke.render_summary(rows, agent.ledger, logs, meta, cap, abort)
    print(text)
    if args.out_dir:
        out = Path(args.out_dir)
        out.mkdir(parents=True, exist_ok=True)
        stamp = when.strftime("%Y-%m-%d-%H%M%S")
        (out / f"smoke-{stamp}.txt").write_text(text + "\n")
        detail = {"meta": meta, "cap": cap, "abort": abort, "logs": logs, "ledger": agent.ledger,
                  "rows": [{"label": r.label, "verdict": r.verdict,
                            "checks": [c.__dict__ for c in (r.grade.checks if r.grade else []) + r.live]} for r in rows],
                  "turns": [{"case": lb, "index": t.index, "kind": t.kind, "error": t.error,
                             "events": [ev for ev in t.events if ev.get("type") != "token"]} for lb, t in agent.results]}
        (out / f"smoke-{stamp}.json").write_text(json.dumps(detail, indent=1, default=str))
    if abort:
        return 3
    return 0 if passed else 1
