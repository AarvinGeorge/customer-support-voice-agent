"""What: a small parser for the agent's SSE wire format (doc 07 s6.1: `event:`, `id:`, `data:` lines, blank line ends an event,
`: keepalive` comments).
Use when: reading a streamed turn response. Inputs: an iterable of text lines. Output: one dict per event (the JSON data).
Side effects: none. Role: the runner's reading side; it never trusts a line that is not JSON."""
from __future__ import annotations

import json
from collections.abc import Iterable, Iterator

from .model import Event


class SseError(ValueError):
    """What: raised when an event's data is not a JSON object. Use when: parsing. Inputs: message. Output: exception.
    Side effects: none. Role: a malformed stream grades the case error, never pass."""


def parse_sse(lines: Iterable[str]) -> Iterator[Event]:
    """What: yield each event's JSON data. Use when: a turn response is streaming. Inputs: lines without newlines.
    Output: event dicts in order; comment lines and unknown fields are skipped. Side effects: none.
    Role: wire format reader; raises SseError on bad JSON."""
    data: list[str] = []
    for line in lines:
        if line == "":
            if data:
                yield _decode(data)
                data = []
        elif line.startswith(":"):
            continue
        elif line.startswith("data:"):
            data.append(line[5:].lstrip(" "))
    if data:
        yield _decode(data)


def _decode(data: list[str]) -> Event:
    """What: decode the joined data lines of one event. Use when: an event ends. Inputs: data lines. Output: dict.
    Side effects: none. Role: raises SseError if the payload is not a JSON object."""
    try:
        obj = json.loads("\n".join(data))
    except json.JSONDecodeError as e:
        raise SseError(f"event data is not JSON: {e}") from e
    if not isinstance(obj, dict):
        raise SseError("event data is not a JSON object")
    return obj
