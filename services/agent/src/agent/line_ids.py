"""What: the id prefixes of every line our code writes to the thread (never a model draft): route lines
(RouterMiddleware), ingress lines (agent.structured, agent.pin_guard), the Gate 2 clarify line, and the limit,
deadline and model-unavailable lines (agent.escalation, agent.sse).
Use when: a writer builds a code-line id (line_id), or Gate 2 decides a message is a code line
(agent.gates.is_code_line, CODE_LINE_PREFIXES).
Inputs: none. Output: ROUTE, INGRESS, GATE2, LIMIT, DEADLINE, UNAVAILABLE, CODE_LINE_PREFIXES, line_id.
Side effects: none.
Role: one constant per prefix (review PR #72 M2), so a writer and the code-line check cannot drift; this module
imports nothing from the agent, so every writer can use it without a cycle.
"""
from __future__ import annotations

ROUTE = "route-"
INGRESS = "ingress-"
GATE2 = "gate2-"
LIMIT = "limit-"
DEADLINE = "deadline-"
UNAVAILABLE = "unavailable-"
CODE_LINE_PREFIXES = (ROUTE, INGRESS, GATE2, LIMIT, DEADLINE, UNAVAILABLE)


def line_id(prefix: str, turn_id: str) -> str:
    """What: a code-line message id, prefix plus turn id. Use when: writing a fixed line to the thread.
    Inputs: one of the prefixes above, the turn id. Output: id. Side effects: none. Role: the only id builder."""
    if prefix not in CODE_LINE_PREFIXES:
        raise ValueError(f"unknown code-line prefix {prefix!r}")
    return f"{prefix}{turn_id}"
