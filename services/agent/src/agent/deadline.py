"""What: the shared clamp and budget helpers of doc 07 s8.2, and the signal that a call was skipped because the
turn has too little time left.
Use when: DeadlineAndFaultMiddleware (model attempts), ToolGuard (tool calls) and ingress (its own core calls)
size an outbound call against the turn deadline held on TurnControl.
Inputs: listed timeouts and the seconds left in the turn (TurnControl.remaining_s()).
Output: clamp_timeout, retry_fits, TurnBudgetExhausted.
Side effects: none.
Role: one clamp rule for every outbound call: min(listed, remaining - 3 s reserve), skipped below the minimum.
"""
from __future__ import annotations

from contracts import limits


class TurnBudgetExhausted(Exception):
    """What: an outbound call was skipped because its clamped timeout fell below the minimum (doc 07 s8.2).
    Use when: raised by DeadlineAndFaultMiddleware; ingress catches it and takes the deadline path.
    Inputs: message. Output: exception. Side effects: none. Role: never retried (the predicate refuses it)."""


def clamp_timeout(listed_s: float, remaining_s: float, *, reserve_s: float = limits.ESCALATION_RESERVE_S,
                  minimum_s: float = limits.CORE_MIN_CALL_S) -> float | None:
    """What: doc 07 s8.2 clamp: min(listed, remaining - reserve), or None when that is below the minimum.
    Use when: before every attempt. Inputs: listed timeout, seconds left in the turn, reserve, minimum (1 s for
    core and knowledge, 2 s for a model attempt).
    Output: the attempt timeout, or None (skip the call; the turn takes the deadline path). Side effects: none.
    Role: shared clamp rule for model and tool calls."""
    t = min(listed_s, remaining_s - reserve_s)
    return None if t < minimum_s else t


def retry_fits(listed_s: float, remaining_s: float, backoff_s: float, *,
               reserve_s: float = limits.ESCALATION_RESERVE_S, extra_s: float = 0.0) -> bool:
    """What: doc 07 s8.2 budget check: retry only if, after the backoff, remaining - reserve still fits the
    full retry timeout (plus extra_s, the 6 s a primary model retry must leave for the fallback).
    Use when: before a retry. Inputs: listed timeout, seconds left, backoff, reserve, extra.
    Output: bool. Side effects: none. Role: deadline-aware retry."""
    return remaining_s - backoff_s - reserve_s >= listed_s + extra_s
