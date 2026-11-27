"""What: step.started and step.completed for work ingress code does itself (doc 07 s6.2 kinds repair, memory,
verify, commit, submit, cancel).
Use when: ingress steps 10, 11 and 13 report progress to the drawer through the turn's event sink.
Inputs: an EventSink (the TurnStream) or None, kind, name, labels.
Output: Step (start, done).
Side effects: events on the turn stream; nothing when there is no sink (unit tests).
Role: one small helper so every ingress step event has the same shape as ToolGuard's.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from agent.turn_control import EventSink


@dataclass
class Step:
    """What: one running ingress step. Use when: Step.start, then done once.
    Inputs: sink, step id, start ms. Output: value. Side effects: none. Role: handle."""

    events: EventSink | None
    step_id: str | None
    t0: int

    @classmethod
    def start(cls, events: EventSink | None, kind: str, name: str, label: str,
              args: dict[str, Any] | None = None) -> "Step":
        """What: emit step.started. Use when: before the work. Inputs: sink, kind, name, present-tense label,
        redacted args. Output: Step. Side effects: one event. Role: constructor."""
        if events is None:
            return cls(None, None, 0)
        step_id = events.next_step_id()
        events.emit("step.started", step_id=step_id, kind=kind, name=name, label=label, args_redacted=args or {})
        return cls(events, step_id, events.ms())

    def done(self, status: str, title: str, *, detail: str | None = None, attempt: int = 1) -> None:
        """What: emit step.completed. Use when: the work ended. Inputs: status, past-tense title, detail,
        attempt. Output: none. Side effects: one event. Role: finisher."""
        if self.events is None or self.step_id is None:
            return
        self.events.emit("step.completed", step_id=self.step_id, status=status, ms=max(self.events.ms() - self.t0, 0),
                         title=title, detail=(detail or None) and detail[:80], summary={}, attempt=attempt)
