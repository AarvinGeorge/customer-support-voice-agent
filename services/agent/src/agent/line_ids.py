"""What: the id prefixes of every line our code writes to the thread (never a model draft): route lines
(RouterMiddleware), ingress lines (agent.structured, agent.pin_guard), the Gate 2 clarify line, and the limit,
deadline and model-unavailable lines (agent.escalation, agent.sse), and the action records (agent.records).
Use when: a writer builds a code-line id (line_id), or Gate 2 decides a message is a code line
(agent.gates.is_code_line, CODE_LINE_PREFIXES).
Inputs: none. Output: ROUTE, INGRESS, GATE2, LIMIT, DEADLINE, UNAVAILABLE, RECORD, CODE_LINE_PREFIXES, line_id,
code_line.
Side effects: none.
Role: one constant per prefix (review PR #72 M2), so a writer and the code-line check cannot drift; this module
imports nothing from the agent, so every writer can use it without a cycle.
"""
from __future__ import annotations

from langchain_core.messages import AIMessage

ROUTE = "route-"
INGRESS = "ingress-"
GATE2 = "gate2-"
LIMIT = "limit-"
DEADLINE = "deadline-"
UNAVAILABLE = "unavailable-"
RECORD = "record-"  # code-written records of code-side actions (agent.records, D-071 part 2)
CODE_LINE_PREFIXES = (ROUTE, INGRESS, GATE2, LIMIT, DEADLINE, UNAVAILABLE, RECORD)


def line_id(prefix: str, turn_id: str) -> str:
    """What: a code-line message id, prefix plus turn id. Use when: writing a fixed line to the thread.
    Inputs: one of the prefixes above, the turn id. Output: id. Side effects: none. Role: the only id builder."""
    if prefix not in CODE_LINE_PREFIXES:
        raise ValueError(f"unknown code-line prefix {prefix!r}")
    return f"{prefix}{turn_id}"


def code_line(prefix: str, turn_id: str, content: str, bound_to: str | None) -> AIMessage:
    """What: the one builder of a code-written thread line: id prefix plus turn id, and the verification it was
    written under in response_metadata["bound_to"] ("user|account", or None while unverified; never sent to a
    model). Use when: every writer of a route, ingress, Gate 2, limit, deadline or unavailable line.
    Inputs: prefix, turn id, line text, the identity (agent.gates.identity_key of the state the line was written
    under). Output: AIMessage. Side effects: none.
    Role: review PR #71 N3: Gate 2 carries a code line only under the verification it was written under."""
    return AIMessage(content, id=line_id(prefix, turn_id), response_metadata={"bound_to": bound_to})
