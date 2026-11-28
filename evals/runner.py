"""What: runs golden cases against the agent HTTP API like the browser and collects what graders need.
Use when: `python -m evals run`. Inputs: cases, personas, an AgentClient, a Store, the policy, optional hooks.
Output: RunResult (every CaseRun and its CaseGrade). Side effects: HTTP calls, store reads, hooks (reseed).
Role: per case: hooks, new session, each turn as the browser sends it (text, masked card, Confirm or Cancel press, voice ask),
then the store read and grading. Any setup failure becomes a CaseRun.error, which grades error and never pass."""
from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from contracts.policy import PolicyConfig

from . import graders
from .agent_client import AgentClient, AgentError
from .cases import resolve_text, wrong_pin
from .hooks import HookError
from .model import CaseGrade, CaseRun, TurnResult
from .store import DataUnavailable, Store


@dataclass
class RunResult:
    """What: all runs and grades of one invocation. Use when: reporting. Inputs: filled by run_cases. Output: the record.
    Side effects: none. Role: input of the report."""

    runs: list[CaseRun] = field(default_factory=list)
    grades: list[CaseGrade] = field(default_factory=list)


def select(cases: list[dict[str, Any]], only: list[str] | None, split: str | None) -> list[dict[str, Any]]:
    """What: filter cases by id list and split. Use when: the CLI got --cases or --split. Inputs: cases, ids, split.
    Output: the chosen cases in file order. Side effects: none. Role: selection."""
    return [c for c in cases if (not only or c["id"] in only) and (not split or c["split"] == split)]


def _last_pending(turns: list[TurnResult]) -> dict[str, Any] | None:
    """What: the most recent confirmation card the agent showed. Use when: building a Confirm press. Inputs: finished turns.
    Output: the PendingPublic dict or None. Side effects: none. Role: the press names the card's pending_action_id (07 s2.3)."""
    for t in reversed(turns):
        for e in reversed(t.events):
            if e.get("type") == "confirmation.required":
                return e["pending"]
    return None


def run_case(case: dict[str, Any], persona: dict[str, Any], agent: AgentClient, store: Store,
             personas: dict[str, dict[str, Any]] | None = None) -> CaseRun:
    """What: play one case's turns and read the stores. Use when: run_cases (hooks already ran). Inputs: case, persona, client,
    store, optional persona map (for a verification turn's as_persona). Output: CaseRun (error set when it could not finish). Side effects: HTTP, a store read, maybe a voice session insert.
    Role: the browser stand-in. Stops at the first failed turn but still reads what the stores hold."""
    run = CaseRun(case_id=case["id"], persona=persona, session_id=None, turns=[])
    try:
        sid = agent.create_session()
    except AgentError as e:
        run.error = str(e)
        return run
    run.session_id = sid
    voice_session: str | None = None
    for i, spec in enumerate(case["turns"]):
        kind = spec["kind"]
        cid = str(uuid.uuid4())
        fault = spec.get("fault")
        voice = False
        if kind == "text":
            text = resolve_text(spec["text"], persona)
            body: dict[str, Any] = {"client_turn_id": cid, "text": text}
            sent: dict[str, Any] = {"text": text}
        elif kind == "verification":
            who = persona
            if spec.get("as_persona"):
                who = (personas or {}).get(spec["as_persona"])
                if who is None:
                    run.error = f"turn {i}: as_persona {spec['as_persona']} is not in personas.yaml"
                    break
            pin = who["pin"] if spec["credentials"] == "persona" else wrong_pin(who["pin"])
            body = {"client_turn_id": cid, "verification": {"account_id": who["account_id"], "pin": pin}}
            sent = {"credentials": spec["credentials"]}
        elif kind == "confirm":
            card = _last_pending(run.turns)
            if card is None:
                run.error = f"turn {i} is a {spec['decision']} press but no confirmation.required card was shown earlier"
                break
            press: dict[str, Any] = {"pending_action_id": card["pending_action_id"], "decision": spec["decision"]}
            if spec["decision"] == "confirm" and card.get("requires_step_up"):
                press["step_up_pin"] = persona["pin"]
            body = {"client_turn_id": cid, "confirm": press}
            sent = {"decision": spec["decision"], "pending_action_id": card["pending_action_id"]}
        elif kind == "voice":
            try:
                voice_session = voice_session or store.register_voice_session(sid)
            except DataUnavailable as e:
                run.error = str(e)
                break
            body = {"client_turn_id": cid, "voice_session_id": voice_session, "realtime_call_id": f"call_{uuid.uuid4().hex[:12]}",
                    "question": spec["question"], "user_item_id": f"item_{uuid.uuid4().hex[:12]}",
                    "user_transcript": spec["user_transcript"], "transcript_final": True,
                    "last_assistant_interrupted": bool(spec.get("last_assistant_interrupted", False))}
            sent = {"user_transcript": spec["user_transcript"], "transcript_final": True}
            voice = True
        else:  # pragma: no cover - the schema allows four kinds
            run.error = f"unknown turn kind {kind}"
            break
        res = agent.turn(i, kind, sid, body, sent, fault=fault, voice=voice)
        run.turns.append(res)
        if res.error:
            run.error = f"turn {i} ({kind}): {res.error}"
            break
    expected = {t.turn_id: sum(1 for e in t.events if e.get("type") != "token") for t in run.turns if t.turn_id}
    try:
        run.data = store.collect(sid, expected)
    except DataUnavailable as e:
        run.data_error = str(e)
    return run


def run_cases(cases: list[dict[str, Any]], personas: dict[str, dict[str, Any]], agent: AgentClient, store: Store,
              policy: PolicyConfig, *, only: list[str] | None = None, split: str | None = None,
              before_case: Callable[[dict[str, Any]], None] | None = None,
              after_case: Callable[[dict[str, Any]], None] | None = None) -> RunResult:
    """What: run and grade the selected cases one by one. Use when: `python -m evals run`. Inputs: cases, personas, client, store,
    policy, optional id and split filters, before and after hooks. Output: RunResult. Side effects: see run_case and hooks.
    Role: orchestrator. A failing before hook marks that case error and the rest still run."""
    out = RunResult()
    for case in select(cases, only, split):
        persona = personas.get(case["persona"])
        if persona is None:
            run = CaseRun(case["id"], {}, None, [], error=f"persona {case['persona']} is not in personas.yaml")
        else:
            try:
                if before_case:
                    before_case(case)
                run = run_case(case, persona, agent, store, personas)
            except HookError as e:
                run = CaseRun(case["id"], persona, None, [], error=str(e))
            finally:
                if after_case:
                    try:
                        after_case(case)
                    except HookError:  # cleanup trouble must not hide the case result; the next before-hook reseeds
                        pass
        out.runs.append(run)
        out.grades.append(graders.grade_case(case, run, policy))
    return out
