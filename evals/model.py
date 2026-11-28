"""What: the plain data shapes the runner and the graders share (turn results, stored rows, check results).
Use when: building an observation of one case run, or reading a grade. Inputs: none. Output: dataclasses.
Side effects: none. Role: the seam between I/O (client, store) and the pure graders, so graders test without a network."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

Event = dict[str, Any]
Status = Literal["pass", "fail", "error"]


@dataclass
class TurnResult:
    """What: one turn as the browser saw it (request kind, HTTP status, SSE events in order).
    Use when: the runner sends a turn; graders read end_reason, final and the input that was sent.
    Inputs: see fields. Output: the record. Side effects: none. Role: per-turn observation."""

    index: int
    kind: str  # text, verification, confirm, voice (the case turn kind)
    input: dict[str, Any]  # what was sent that graders need: text, decision, pending_action_id, user_transcript, transcript_final
    status_code: int
    events: list[Event] = field(default_factory=list)
    turn_id: str | None = None
    error: str | None = None

    @property
    def completed(self) -> Event | None:
        """What: the terminal turn.completed event. Use when: reading end_reason or final. Inputs: none.
        Output: the event or None. Side effects: none. Role: accessor."""
        for e in reversed(self.events):
            if e.get("type") == "turn.completed":
                return e
        return None

    @property
    def end_reason(self) -> str | None:
        """What: the turn's end_reason. Use when: grading outcome. Inputs: none. Output: str or None when no
        turn.completed arrived. Side effects: none. Role: accessor."""
        c = self.completed
        return None if c is None else c.get("end_reason")

    @property
    def final(self) -> dict[str, Any] | None:
        """What: the FinalAnswer of the turn. Use when: reading text, say, citations. Inputs: none.
        Output: dict or None. Side effects: none. Role: accessor."""
        c = self.completed
        return None if c is None else c.get("final")


@dataclass
class RunData:
    """What: what the stores returned for one session: core rows and the stored agent.turn_events.
    Use when: grading final state and tool trajectory. Inputs: rows as dicts. Output: the record.
    Side effects: none. Role: the evidence the browser cannot see."""

    pending: list[dict[str, Any]]
    refunds: list[dict[str, Any]]
    tickets: list[dict[str, Any]]
    events: list[Event]  # stored events (no token events), in turn then seq order


@dataclass
class CaseRun:
    """What: everything observed for one case. Use when: grading. Inputs: see fields.
    Output: the record. Side effects: none. Role: the single input of grade_case."""

    case_id: str
    persona: dict[str, Any]
    session_id: str | None
    turns: list[TurnResult]
    data: RunData | None = None
    error: str | None = None  # the run could not finish (reseed, HTTP, SSE)
    data_error: str | None = None  # the stores could not be read


@dataclass
class Check:
    """What: one grader result. Use when: reporting. Inputs: name, status, detail (never a PIN), exercised (path probes).
    Output: the record. Side effects: none. Role: unit of grading; error means the data to decide was missing."""

    name: str
    status: Status
    detail: str = ""
    exercised: bool | None = None  # path probes only: whether the run took the path that tests the decision (None for other checks)


@dataclass
class CaseGrade:
    """What: the grade of one case. Use when: reporting. Inputs: checks, optional known_limitation tag.
    Output: the record with a verdict.
    Side effects: none. Role: fail beats error beats pass; an empty check list is an error, never a pass."""

    case_id: str
    split: str
    checks: list[Check]
    known_limitation: str | None = None  # the decision id (D-083) when the case's expected path is a known limitation

    @property
    def verdict(self) -> Status:
        """What: the case verdict. Use when: counting. Inputs: none. Output: pass, fail or error.
        Side effects: none. Role: fail if any check failed, else error if any errored or none ran, else pass."""
        if not self.checks:
            return "error"
        if any(c.status == "fail" for c in self.checks):
            return "fail"
        if any(c.status == "error" for c in self.checks):
            return "error"
        return "pass"
