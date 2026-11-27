"""What: the live smoke gate for agent-behaviour PRs: a fixed subset of the golden cases (S1 verify and commit plus the
policy follow-up, S3 member decline, S3 owner approval, S4 out of docs, S5 one fault) run against the real agent through
Caddy, graded by the golden graders plus live checks per turn, under a hard cap on real model calls.
Use when: `python -m evals smoke`, which scripts/live_smoke.sh calls on a throwaway stack (RUN_REAL=1 only).
Inputs: the golden cases, TurnResults from the runner, the throwaway stack's container logs.
Output: smoke_cases, CappedAgent, BudgetExceeded, turn_calls, live_checks, log_findings, render_summary.
Side effects: CappedAgent sends HTTP turns; log_findings runs `docker compose -p <project> logs` (read-only).
Role: the detector for class A of the root-cause audit (a fake stood in for the real model, so green tests proved the
fake): the same graders, the real model, before merge. No new framework: the golden runner, graders and reseed guard."""
from __future__ import annotations

import copy
import re
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from .agent_client import AgentClient
from .model import CaseGrade, Check, Event, TurnResult

DEFAULT_CAP = 25

# (case id, label, gates the case must show). S1-CHAT is G-03 played in chat without the voice barge-in turn, so one
# session covers S1 verify and commit and the S1 policy follow-up (the turn that showed the clarify line live).
SMOKE: list[tuple[str, str, frozenset[str]]] = [
    ("S1-CHAT", "S1 verify, commit, policy follow-up", frozenset({"retrieval", "grounding_pass"})),
    ("G-04", "S3 member decline", frozenset({"grounding_pass"})),
    ("G-05", "S3 owner approval", frozenset()),
    ("G-07", "S4 out of docs", frozenset({"retrieval"})),
    ("G-12", "S5 llm_429 fallback", frozenset({"retrieval", "grounding_pass"})),
]

# end_reason values that mean the turn did not finish its job (doc 07 s6.2 EndReason).
BAD_ENDS = frozenset({"error", "deadline", "cap_reached", "limit_reached", "swept"})
# services/agent/src/agent/gates.py CLARIFY_LINE (test_smoke pins this copy to the source).
CLARIFY_LINE = "I want to be sure I give you the right answer."

# Warning lines the agent logs when a real call went wrong without failing the turn (test_smoke pins each one to the
# format string in the agent source, so a zero here can be non-zero).
AGENT_LOG_PATTERNS: dict[str, str] = {
    "agent reply cut at max_tokens": r"model reply cut at max_tokens",
    "verifier reply truncated": r"grounding verifier reply truncated",
    "verifier failed": r"grounding verifier failed",
    "classifier reply truncated": r"scope classifier reply truncated",
    "classifier failed": r"scope classifier failed",
}
# Knowledge access log: one line per search request (one query embedding each), plus failed first attempts (a retry
# embeds again; counted as an upper bound).
SEARCH_LINE = r" POST /v1/search \d{3} "
EMBED_RETRY_LINE = r"embedding attempt 1 failed"


class BudgetExceeded(RuntimeError):
    """What: the real-call cap would be or was exceeded, or a turn's call count is unknown. Use when: CappedAgent.
    Inputs: message. Output: exception. Side effects: none. Role: aborts the smoke before more spend."""


def smoke_cases(all_cases: list[dict[str, Any]]) -> list[tuple[str, dict[str, Any], frozenset[str]]]:
    """What: the smoke subset as (label, case, required gates), in run order. Use when: the smoke starts.
    Inputs: the golden cases (evals.cases.load_cases). Output: list. Side effects: none.
    Role: S1-CHAT is derived from G-03 (turns 0 to 3, the first voice question sent as a chat text turn with the same
    expect; the case expectations are G-03's), so the dataset keeps one source of truth."""
    by_id = {c["id"]: c for c in all_cases}
    out: list[tuple[str, dict[str, Any], frozenset[str]]] = []
    for cid, label, gates in SMOKE:
        if cid == "S1-CHAT":
            case = copy.deepcopy(by_id["G-03"])
            voice = case["turns"][3]
            case["turns"] = [*case["turns"][:3], {"kind": "text", "text": voice["question"], "expect": voice["expect"]}]
            case.update(id="S1-CHAT", mode="chat", title=label)
        else:
            case = by_id[cid]
        out.append((label, case, gates))
    return out


def _usage(t: TurnResult) -> dict[str, Any] | None:
    """What: the turn's Usage. Use when: counting. Inputs: turn. Output: dict or None. Side effects: none. Role: helper."""
    c = t.completed
    return None if c is None else c.get("usage")


def _steps(t: TurnResult) -> list[tuple[Event, Event | None]]:
    """What: (step.started, matching step.completed or None) pairs in order. Use when: counting and checks.
    Inputs: turn. Output: pairs. Side effects: none. Role: helper."""
    done = {e["step_id"]: e for e in t.events if e.get("type") == "step.completed"}
    return [(e, done.get(e["step_id"])) for e in t.events if e.get("type") == "step.started"]


def turn_calls(t: TurnResult) -> dict[str, int] | None:
    """What: real calls of one turn: total model calls (Usage.model_calls, which equals LangSmith's llm run count, doc 07
    s2.3), split into verifier (grounding gate steps that called the model), classifier (scope gate steps), agent (the
    rest: primary, retries and fallback), plus search (search_kb tool steps, one query embedding each).
    Use when: after each turn. Inputs: turn. Output: dict, or None when the turn streamed but sent no turn.completed
    (the count is unknown). A non-200 turn made no call: zeros. Side effects: none. Role: the call ledger."""
    if t.status_code != 200:
        return {"total": 0, "agent": 0, "verifier": 0, "classifier": 0, "search": 0}
    u = _usage(t)
    if u is None:
        return None
    verifier = classifier = search = 0
    for start, end in _steps(t):
        summary = (end or {}).get("summary") or {}
        if start.get("name") == "grounding_gate" and summary.get("verdict") != "skipped" and summary.get("reason") != "no_time":
            verifier += 1
        elif start.get("name") == "scope_gate":
            classifier += 1
        elif start.get("kind") == "tool" and start.get("name") == "search_kb":
            search += 1
    total = int(u.get("model_calls") or 0)
    return {"total": total, "agent": total - verifier - classifier, "verifier": verifier, "classifier": classifier,
            "search": search}


class CappedAgent(AgentClient):
    """What: the browser-like AgentClient with a hard cap on real model calls and a per-turn ledger.
    Use when: the smoke drives the real agent. Inputs: httpx client, cap. Output: TurnResults; ledger of
    (case label, turn index, calls). Side effects: HTTP. Role: refuses a turn once the cap is reached and aborts as soon
    as a turn takes the total past it, or when a turn's count is unknown (the cap could not be proven)."""

    def __init__(self, http: Any, cap: int) -> None:
        """What: wrap the client. Use when: smoke setup. Inputs: httpx client, cap. Output: none. Side effects: none.
        Role: init."""
        super().__init__(http)
        self.cap, self.spent, self.label = cap, 0, ""
        self.ledger: list[tuple[str, int, dict[str, int]]] = []
        self.results: list[tuple[str, TurnResult]] = []

    def turn(self, index: int, kind: str, sid: str, body: dict[str, Any], sent: dict[str, Any], fault: str | None = None,
             voice: bool = False) -> TurnResult:
        """What: send one turn under the cap. Use when: the runner sends a turn. Inputs: as AgentClient.turn.
        Output: TurnResult. Side effects: one HTTP request unless refused. Role: raises BudgetExceeded (see class)."""
        if self.spent >= self.cap:
            raise BudgetExceeded(f"cap {self.cap} reached ({self.spent} real model calls); turn {index} of {self.label} not sent")
        res = super().turn(index, kind, sid, body, sent, fault=fault, voice=voice)
        self.results.append((self.label, res))
        calls = turn_calls(res)
        if calls is None:
            raise BudgetExceeded(f"{self.label} turn {index} streamed without turn.completed ({res.error}); "
                                 "its real-call count is unknown")
        self.spent += calls["total"]
        self.ledger.append((self.label, index, calls))
        if self.spent > self.cap:
            raise BudgetExceeded(f"cap {self.cap} exceeded: {self.spent} real model calls after {self.label} turn {index}")
        return res


def _gates(t: TurnResult, gate: str) -> list[Event]:
    """What: gate events of one kind in a turn. Use when: checks. Inputs: turn, retrieval or grounding. Output: events.
    Side effects: none. Role: helper."""
    return [e for e in t.events if e.get("type") == "gate" and e.get("gate") == gate]


def live_checks(case: dict[str, Any], turns: list[TurnResult], required: frozenset[str]) -> list[Check]:
    """What: the live checks per turn and per case. Per turn: stop (turn.completed, no error event, end_reason not in
    BAD_ENDS and equal to the turn's expect), truncation (no model step cut at max_tokens, no gate reply truncated),
    gate1 (a turn that searched shows a retrieval gate event), gate2 (a turn with a model draft, final.confidence set,
    shows a grounding gate event, and no grounding gate errored), clarify (no Gate 2 clarify decision, end_reason clarify or
    clarify line unless the turn expects clarify). Per case: the required gates (retrieval; grounding_pass: a grounding gate passed).
    Use when: after a case ran. Inputs: case, its turns, required gates. Output: Checks named live.<kind>[.N].
    Side effects: none. Role: what the browser pass used to catch after merge."""
    out: list[Check] = []
    specs = case["turns"]
    for i, spec in enumerate(specs):
        if i >= len(turns):
            out.append(Check(f"live.stop.{i}", "error", "turn was not run"))
            continue
        t = turns[i]
        exp = (spec.get("expect") or {}).get("end_reason")
        end = t.end_reason
        errors = [e.get("code") for e in t.events if e.get("type") == "error"]
        bad = ([f"no turn.completed ({t.error})"] if t.completed is None else []) + \
              ([f"end_reason {end}"] if end in BAD_ENDS else []) + \
              ([f"end_reason {end}, expected {exp}"] if exp and end is not None and end != exp else []) + \
              ([f"error events {errors}"] if errors else [])
        out.append(Check(f"live.stop.{i}", "fail" if bad else "pass", "; ".join(bad) or f"{end}"))
        cut = [s.get("model") for s, e in _steps(t)
               if s.get("kind") == "model" and int(((e or {}).get("summary") or {}).get("truncated") or 0) > 0]
        cut += [s.get("name") for s, e in _steps(t)
                if s.get("kind") == "gate" and ((e or {}).get("summary") or {}).get("reason") == "truncated"]
        out.append(Check(f"live.truncation.{i}", "fail" if cut else "pass", f"cut at max_tokens: {cut}" if cut else "none"))
        searched = any(s.get("kind") == "tool" and s.get("name") == "search_kb" for s, _ in _steps(t))
        if searched:
            n = len(_gates(t, "retrieval"))
            out.append(Check(f"live.gate1.{i}", "pass" if n else "fail", f"{n} retrieval gate events" if n else
                             "search_kb ran but no retrieval gate event"))
        conf = (t.final or {}).get("confidence")
        g2 = _gates(t, "grounding")
        g2_err = [((e or {}).get("summary") or {}).get("reason") for s, e in _steps(t)
                  if s.get("name") == "grounding_gate" and (e or {}).get("status") in ("error", "timeout")]
        if conf is not None or g2:
            bad2 = (["model draft but no grounding gate event"] if conf is not None and not g2 else []) + \
                   ([f"verifier gave no verdict: {g2_err}"] if g2_err else [])
            out.append(Check(f"live.gate2.{i}", "fail" if bad2 else "pass", "; ".join(bad2) or
                             f"grounding {[e['verdict'] for e in g2]}"))
        # The clarify line is Gate 2's (or a turn ending clarify). A Gate 1 weak verdict's decision "clarify" is an
        # instruction to the model, not the line, so it is not counted here (it shows in the gate1 events).
        clar = ["grounding decision clarify" for e in _gates(t, "grounding") if e.get("decision") == "clarify"]
        clar += ["end_reason clarify"] if end == "clarify" else []
        clar += ["clarify line in the reply"] if CLARIFY_LINE in str((t.final or {}).get("text") or "") else []
        if exp != "clarify":
            out.append(Check(f"live.clarify.{i}", "fail" if clar else "pass", "; ".join(clar) or "none"))
    every = [e for t in turns for e in t.events if e.get("type") == "gate"]
    if "retrieval" in required:
        n = sum(1 for e in every if e["gate"] == "retrieval")
        out.append(Check("live.gate1.case", "pass" if n else "fail", f"{n} retrieval gate events in the case"))
    if "grounding_pass" in required:
        n = sum(1 for e in every if e["gate"] == "grounding" and e["verdict"] == "pass")
        out.append(Check("live.gate2.case", "pass" if n else "fail", f"{n} grounding gates passed in the case"))
    return out


def log_findings(project: str, run: Callable[..., Any] = subprocess.run) -> dict[str, int]:
    """What: counts from the throwaway stack's container logs: each AGENT_LOG_PATTERNS warning (agent_a, agent_b),
    search requests and failed first embedding attempts (knowledge). Use when: after the cases ran.
    Inputs: Compose project name, a subprocess.run (faked in tests). Output: name to count; raises RuntimeError when the
    logs cannot be read (a zero must come from a read, never from a failed one). Side effects: `docker compose -p
    <project> logs` (read-only). Role: truncation and gate failures that do not reach the stream."""
    def logs(*services: str) -> str:
        """What: one service group's logs. Use when: log_findings. Inputs: services. Output: text. Side effects: docker.
        Role: helper."""
        r = run(["docker", "compose", "-p", project, "logs", "--no-color", "--no-log-prefix", *services],
                capture_output=True, text=True, timeout=120)
        if r.returncode != 0:
            raise RuntimeError(f"docker compose logs {services} exited {r.returncode}")
        return r.stdout or ""
    agent, knowledge = logs("agent_a", "agent_b"), logs("knowledge")
    if "Uvicorn running" not in agent or "Uvicorn running" not in knowledge:
        raise RuntimeError("container logs do not show the services starting: the log read cannot be trusted")
    out = {name: len(re.findall(p, agent)) for name, p in AGENT_LOG_PATTERNS.items()}
    out["search requests"] = len(re.findall(SEARCH_LINE, knowledge))
    out["embedding retries (upper bound)"] = len(re.findall(EMBED_RETRY_LINE, knowledge))
    return out


@dataclass
class SmokeRow:
    """What: one case's smoke outcome. Use when: rendering. Inputs: label, grade, live checks, end reasons, calls.
    Output: the record with a verdict. Side effects: none. Role: summary row."""

    label: str
    grade: CaseGrade | None
    live: list[Check] = field(default_factory=list)
    ends: list[str] = field(default_factory=list)
    calls: int = 0

    @property
    def verdict(self) -> str:
        """What: FAIL when a golden or live check failed, ERROR when one errored or the case did not grade, else PASS.
        Use when: rendering. Inputs: none. Output: str. Side effects: none. Role: verdict."""
        checks = (self.grade.checks if self.grade else []) + self.live
        if self.grade is None or not checks:
            return "ERROR"
        if any(c.status == "fail" for c in checks):
            return "FAIL"
        return "ERROR" if any(c.status == "error" for c in checks) else "PASS"


def render_summary(rows: list[SmokeRow], ledger: list[tuple[str, int, dict[str, int]]], logs: dict[str, int] | None,
                   meta: dict[str, Any], cap: int, abort: str | None) -> tuple[str, bool]:
    """What: the one-screen summary and whether the smoke passed. Use when: the smoke ends (also after an abort).
    Inputs: rows, call ledger, log counts (None when unread), meta (when, project, edge port, commit, dirty, ingest
    embedding calls, cost), cap, abort reason. Output: (text, passed). Side effects: none.
    Role: the block a PR body pastes; scripts/check_smoke_summary.py reads its first line and RESULT line."""
    tot = {k: sum(c[k] for _, _, c in ledger) for k in ("total", "agent", "verifier", "classifier", "search")}
    ingest = int(meta.get("ingest_embedding_calls") or 0)
    retries = (logs or {}).get("embedding retries (upper bound)", 0)
    lines = [f"LIVE SMOKE {meta['when']} commit {meta['commit']}{' (dirty tree)' if meta.get('dirty') else ''}",
             f"stack {meta['project']} via Caddy 127.0.0.1:{meta['edge_port']}  models {meta.get('models', '?')}", ""]
    lines.append(f"{'case':<38}{'result':<8}{'calls':>6}  end reasons per turn")
    for r in rows:
        lines.append(f"{r.label:<38}{r.verdict:<8}{r.calls:>6}  {' > '.join(r.ends) or '-'}")
    live = [c for r in rows for c in r.live]

    def tally(kind: str) -> str:
        """What: passed/total for one live check kind. Use when: rendering. Inputs: kind. Output: text.
        Side effects: none. Role: helper."""
        cs = [c for c in live if c.name.startswith(f"live.{kind}")]
        return f"{sum(c.status == 'pass' for c in cs)}/{len(cs)}"
    lines += ["", f"checks: stop {tally('stop')} | no truncation {tally('truncation')} | gate1 {tally('gate1')} | "
              f"gate2 {tally('gate2')} | clarify only where expected {tally('clarify')}"]
    bad_logs = {k: v for k, v in (logs or {}).items() if k in AGENT_LOG_PATTERNS and v}
    lines.append("agent log warnings: " + ("not read" if logs is None else
                                           (", ".join(f"{k} {v}" for k, v in bad_logs.items()) or "none")))
    if logs is not None and logs.get("search requests") != tot["search"]:
        lines.append(f"note: knowledge logged {logs.get('search requests')} search requests, the streams show {tot['search']}")
    emb = ingest + (logs or {}).get("search requests", tot["search"]) + retries
    lines.append(f"real calls: agent {tot['agent']}, verifier {tot['verifier']}, classifier {tot['classifier']} = "
                 f"{tot['total']} model calls (cap {cap}); embeddings {emb} (ingest {ingest}, search "
                 f"{(logs or {}).get('search requests', tot['search'])}, retries {retries}); cost est ${meta.get('cost', 0):.4f}")
    fails = [(r.label, c) for r in rows for c in (r.grade.checks if r.grade else []) + r.live if c.status != "pass"]
    for label, c in fails[:12]:
        lines.append(f"  {c.status.upper()} {label}: {c.name}: {c.detail[:110]}")
    if len(fails) > 12:
        lines.append(f"  ... {len(fails) - 12} more in the JSON")
    if abort:
        lines.append(f"ABORTED: {abort}")
    passed = (abort is None and len(rows) == len(SMOKE) and all(r.verdict == "PASS" for r in rows)
              and logs is not None and not bad_logs and not meta.get("dirty"))
    lines.append(f"RESULT: {'PASS' if passed else 'FAIL'}")
    return "\n".join(lines), passed
