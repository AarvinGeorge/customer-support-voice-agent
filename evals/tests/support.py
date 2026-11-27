"""What: test helpers that build contract-valid SSE events, turn results and case runs without a network.
Use when: unit-testing graders and the runner. Inputs: small keyword arguments. Output: events, TurnResult, CaseRun.
Side effects: none. Role: keeps each grader test to a few lines; every event is validated by contracts.events.parse_event."""
from __future__ import annotations

import copy
import itertools
from pathlib import Path
from typing import Any

import yaml
from contracts.events import parse_event

from evals.model import CaseRun, Event, RunData, TurnResult

ROOT = Path(__file__).resolve().parents[2]
SID = "019a7c2e-5b10-7000-8000-00000000a001"
_counter = itertools.count(1)

PERSONA = {"name": "Priya Raman", "account_id": "QS-10423", "pin": "7342", "tier": "pro", "role": "billing_admin"}


def case_by_id(case_id: str) -> dict[str, Any]:
    """What: one golden case from the real dataset. Use when: a test wants a real expectation. Inputs: id.
    Output: the case dict. Side effects: reads evals/golden/golden.yaml. Role: tests use the shipped cases."""
    for c in yaml.safe_load((ROOT / "evals/golden/golden.yaml").read_text())["cases"]:
        if c["id"] == case_id:
            return c
    raise KeyError(case_id)


def _env(turn_id: str, seq: int, type_: str, **fields: Any) -> Event:
    e: Event = {"v": 1, "type": type_, "seq": seq, "session_id": SID, "turn_id": turn_id, "trace_id": turn_id,
                "ts": f"2026-11-27T13:00:{seq:02d}.000000Z", "mode": "chat"}
    e.update(fields)
    return e


def step(name: str, kind: str = "tool", status: str = "ok", rule: dict[str, Any] | None = None, **extra: Any) -> list[dict[str, Any]]:
    """What: a started and completed step pair as field dicts. Use when: scripting a tool call. Inputs: name, kind,
    status, optional rule. Output: two partial events (type and fields). Side effects: none. Role: builder."""
    sid = f"s{next(_counter)}"
    return [
        {"type": "step.started", "step_id": sid, "kind": kind, "name": name, "label": name, "args_redacted": extra.get("args", {})},
        {"type": "step.completed", "step_id": sid, "status": status, "ms": 5, "title": name, "detail": None, "summary": {},
         "attempt": 1, "model": None, "input_tokens": None, "output_tokens": None, "cache_read_tokens": None, "rule": rule},
    ]


def rule(code: str, decision: str, article: str, section: str) -> dict[str, Any]:
    """What: a StepRule dict. Use when: scripting prepare_*. Inputs: code, decision, article, section.
    Output: dict. Side effects: none. Role: builder."""
    return {"decision": decision, "reason_code": code, "policy_ref": {"article": article, "section": section, "title": "T"}}


def gate(verdict: str, decision: str, chunk_ids: list[str] | None = None, kind: str = "retrieval") -> dict[str, Any]:
    """What: a gate event as fields. Use when: scripting retrieval. Inputs: verdict, decision, chunk ids.
    Output: partial event. Side effects: none. Role: builder."""
    return {"type": "gate", "step_id": "g", "gate": kind, "verdict": verdict, "decision": decision, "top_cosine": 0.6,
            "tau_low": 0.3, "tau_high": 0.45, "calibration_status": "provisional", "code_match": False,
            "chunk_ids": chunk_ids or [], "claims_total": None, "claims_supported": None, "claims": None, "commitment": None}


def pending(pa: str = "PA-3F2A9C1B", amount: int = 9600) -> dict[str, Any]:
    """What: a PendingPublic dict. Use when: confirmation.required. Inputs: id, amount. Output: dict.
    Side effects: none. Role: builder."""
    return {"pending_action_id": pa, "action_type": "refund", "kind": "commit", "state": "prepared",
            "readback_text": "Refund $96.00 for the charge ending 0412?", "readback_say": "Refund ninety six dollars?",
            "amount_cents": amount, "title": "Refund", "invoice_ref": None, "charge_date": None,
            "reason_code": "PRO_WITHIN_LIMITS", "requires_step_up": False, "expires_at": None, "ticket": None}


def turn(index: int, kind: str, parts: list[dict[str, Any]] | None = None, *, end_reason: str = "answered", text: str = "Done.",
         say: str | None = None, citations: list[str] | None = None, input: dict[str, Any] | None = None,
         input_kind: str | None = None, tokens: bool = False) -> tuple[TurnResult, list[Event]]:
    """What: build one turn: the SSE events (with tokens) and the stored events (without tokens).
    Use when: a grader test needs an observed turn. Inputs: index, case turn kind, partial events in order, end_reason,
    final text and say, cited chunk ids, the request input. Output: (TurnResult, stored events). Side effects: none.
    Role: every event is validated against the contract so a bad helper cannot hide a grader bug."""
    tid = f"019a7c2e-5b20-7000-8000-{index:012d}"
    ik = input_kind or {"text": "text", "verification": "verification", "confirm": "confirm", "voice": "voice"}[kind]
    seq = 1
    sse: list[Event] = []
    stored: list[Event] = []

    def add(type_: str, **f: Any) -> None:
        nonlocal seq
        e = _env(tid, seq, type_, **f)
        parse_event(e)
        sse.append(e)
        if type_ != "token":
            stored.append(e)
        seq += 1

    add("turn.started", served_by="agent_a", input_kind=ik, contract_version="2026-11-26.r4")
    for p in parts or []:
        p = copy.deepcopy(p)  # shared builders (rules, pendings) must not leak mutations between tests
        add(p.pop("type"), **p)
    if tokens:
        add("token", text=text)
    cites = [{"chunk_id": c, "label": "L", "ref": c, "spoken": "s", "url": "https://x.test/a", "section_title": "S",
              "excerpt": "e", "score": 0.7} for c in citations or []]
    final = {"text": text, "say": say if say is not None else text, "citations": cites, "escalated": end_reason == "escalated",
             "ticket": None, "pending_action": None, "confidence": None}
    usage = {"input_tokens": 1, "output_tokens": 1, "cache_read_tokens": 0, "cache_write_tokens": 0, "cost_usd_est": 0.0,
             "first_status_ms": 1, "ttft_ms": 1, "latency_ms": 10, "model_calls": 1, "tool_calls": 0, "models_used": [],
             "fallback_used": False}
    add("turn.completed", final=final, usage=usage, end_reason=end_reason, replayed=False)
    tr = TurnResult(index=index, kind=kind, input=input or ({"text": "hello"} if kind == "text" else {}), status_code=200,
                    events=sse, turn_id=tid)
    return tr, stored


def make_run(case_id: str, turns: list[tuple[TurnResult, list[Event]]], *, pending_rows: list[dict[str, Any]] | None = None,
             refunds: list[dict[str, Any]] | None = None, tickets: list[dict[str, Any]] | None = None,
             persona: dict[str, Any] | None = None, data: bool = True) -> CaseRun:
    """What: assemble a CaseRun from built turns and rows. Use when: calling grade_case or one grader.
    Inputs: case id, turns from turn(), core rows. Output: CaseRun. Side effects: none. Role: builder."""
    events = [e for _, st in turns for e in st]
    rd = RunData(pending=pending_rows or [], refunds=refunds or [], tickets=tickets or [], events=events) if data else None
    return CaseRun(case_id=case_id, persona=persona or PERSONA, session_id=SID, turns=[t for t, _ in turns], data=rd,
                   data_error=None if data else "stores not readable")
