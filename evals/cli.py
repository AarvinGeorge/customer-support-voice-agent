"""What: the command line, `python -m evals run`: load the dataset, reseed per case, drive the agent API, grade with code graders,
print Gate 1 counts, write the dated report and exit 0 only when every graded case passed.
Use when: a measured run is wanted (against a running stack, or against a test double in CI).
Inputs: flags below and env DEMO_ACCESS_CODE, EVALS_AGENT_URL (default http://localhost:3000), EVALS_KNOWLEDGE_URL,
KNOWLEDGE_TOKEN_AGENT, EVALS_CORE_DB_URL or CORE_DB_URL, EVALS_AGENT_DB_URL or AGENT_DB_URL, EVALS_RESEED_CMD,
EVALS_FIXTURE_INGEST_CMD and EVALS_FIXTURE_REMOVE_CMD (templates with {path}). Output: report files, a summary on stdout.
Side effects: HTTP, Postgres SELECTs, reseed subprocesses, files. Role: single entry point (doc 09 s5 CS-E-06.S1.T1).
Exit codes: 0 all passed, 1 some case not passed, 2 usage or configuration problem (nothing was run)."""
from __future__ import annotations

import argparse
import datetime as dt
import os
import sys
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

import httpx
from contracts.policy import load_policy

from . import gate1, report, runner
from .agent_client import AgentClient, AgentError
from .cases import ROOT, DatasetError, load_cases, load_personas, load_retrieval
from .hooks import make_hooks
from .store import PgStore

DEFAULT_RESEED = "docker compose exec -T -e DEMO_RESEED=1 core python -m core.seeds --reseed"


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
    return p


def main(argv: Sequence[str] | None = None, env: Mapping[str, str] | None = None, *, agent_http: httpx.Client | None = None,
         store: Any = None, search: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
         before_case: Callable[[dict[str, Any]], None] | None = None, after_case: Callable[[dict[str, Any]], None] | None = None,
         now: dt.datetime | None = None) -> int:
    """What: run the command. Use when: executed, or called by tests with doubles. Inputs: argv, env, and optional injected
    agent_http, store, search, hooks and clock (tests). Output: exit code. Side effects: see module.
    Role: wires real clients from env unless doubles are injected."""
    args = _parser().parse_args(list(sys.argv[1:] if argv is None else argv))
    e = os.environ if env is None else env
    when = now or dt.datetime.now(dt.UTC)
    code = e.get("DEMO_ACCESS_CODE")
    if not code:
        print("DEMO_ACCESS_CODE is not set: cannot open the agent API", file=sys.stderr)
        return 2
    try:
        all_cases = load_cases()
        queries = load_retrieval()
        personas = load_personas()
    except DatasetError as ex:
        print(f"dataset error: {ex}", file=sys.stderr)
        return 2
    only = args.cases.split(",") if args.cases else None
    if only and not set(only) <= {c["id"] for c in all_cases}:
        print(f"unknown case ids: {sorted(set(only) - {c['id'] for c in all_cases})}", file=sys.stderr)
        return 2
    agent_url = args.agent_url or e.get("EVALS_AGENT_URL", "http://localhost:3000")
    if agent_http is None:
        agent_http = httpx.Client(base_url=agent_url, timeout=httpx.Timeout(90.0, connect=10.0))
    if store is None:
        core_db = e.get("EVALS_CORE_DB_URL") or e.get("CORE_DB_URL")
        agent_db = e.get("EVALS_AGENT_DB_URL") or e.get("AGENT_DB_URL")
        if not core_db or not agent_db:
            print("CORE_DB_URL and AGENT_DB_URL (or the EVALS_ overrides) are required to read rows and turn_events", file=sys.stderr)
            return 2
        store = PgStore(core_db, agent_db)
    if before_case is None:
        before_case, after_case = make_hooks(e.get("EVALS_RESEED_CMD", DEFAULT_RESEED), e.get("EVALS_FIXTURE_INGEST_CMD"),
                                             e.get("EVALS_FIXTURE_REMOVE_CMD"))
    if search is None and not args.skip_gate1:
        k_url, k_tok = args.knowledge_url or e.get("EVALS_KNOWLEDGE_URL"), e.get("KNOWLEDGE_TOKEN_AGENT")
        if not k_url or not k_tok:
            print("EVALS_KNOWLEDGE_URL and KNOWLEDGE_TOKEN_AGENT are required for Gate 1 (or pass --skip-gate1)", file=sys.stderr)
            return 2
        search = gate1.KnowledgeClient(httpx.Client(base_url=k_url, timeout=10.0), k_tok).search
    agent = AgentClient(agent_http)
    try:
        agent.access(code)
    except AgentError as ex:
        print(f"cannot start: {ex}", file=sys.stderr)
        return 2
    policy = load_policy(ROOT / "data" / "policy.yaml")
    result = runner.run_cases(all_cases, personas, agent, store, policy, only=only, split=args.split,
                              before_case=before_case, after_case=after_case)
    g1 = None if args.skip_gate1 or search is None else gate1.run_gate1(queries, search)
    rep = report.build_report(result.grades, g1, {"agent_url": agent_url, "cases_selected": [g.case_id for g in result.grades]}, when)
    md, js = report.write_report(rep, Path(args.out_dir), when)
    v = rep["cases"]["verdicts"]
    print(f"{rep['cases']['total']} cases: {v['pass']} pass, {v['fail']} fail, {v['error']} error. Report: {md} and {js}")
    return 0 if v["fail"] == 0 and v["error"] == 0 and rep["cases"]["total"] > 0 else 1
