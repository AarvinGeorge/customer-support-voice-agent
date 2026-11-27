"""What: the agent side of the trust boundary (doc 07 s5.3): a hardened escaper for untrusted strings, the
tool result and search result wrappers, and the "unknown, not empty" payloads for failed, blocked and
not-run tool calls.
Use when: ToolGuard builds every ToolMessage the model sees; ingress step 10 repairs a dangling tool call
(not_run_tool_message).
Inputs: tool names, JSON-like results, chunk fields.
Output: harden, harden_obj, wrap_result, wrap_search, unknown_result, not_run_tool_message, UNKNOWN_NOTE.
Side effects: none.
Role: every string inside a wrapper goes through harden and then the contracts escaper. harden also defeats
tag look-alikes the contracts escaper leaves (review PR #4 minor 4): zero-width and other format characters
are removed, fullwidth and small-form "<" are escaped like "<", "//" or "\\" before the name is allowed, and
any tag whose name STARTS with a wrapper tag ("<system-reminder>", "<tool_use>") is escaped too.
"""
from __future__ import annotations

import json
import re
import unicodedata
from typing import Any

from contracts.tools import STATUS_NOT_RUN
from contracts.trust import UNTRUSTED_WRAPPER_TAGS, wrap_doc, wrap_tool_result
from langchain_core.messages import ToolMessage

_NAMES = "|".join(sorted((re.escape(t) for t in UNTRUSTED_WRAPPER_TAGS), key=len, reverse=True))
# "<", fullwidth "＜" (U+FF1C) or small "﹤" (U+FE64), then any run of spaces and slashes (ASCII, fullwidth or
# backslash), then a name that starts with a wrapper tag. No end-of-name check, on purpose: prefixes count.
_LOOKALIKE_RE = re.compile(rf"[<＜﹤](?=[\s/\\／]*(?:{_NAMES}))", re.IGNORECASE)

UNKNOWN_NOTE = ("No result is available, so the answer is unknown. Do not treat it as empty or as no records; "
                "tell the customer you could not check.")


def _strip_format_chars(text: str) -> str:
    """What: remove Unicode format characters (category Cf: zero-width space and joiners, BOM, bidi controls).
    Use when: inside harden. Inputs: text. Output: text without them. Side effects: none.
    Role: a zero-width character between "<" and "/tool_result" must not hide the tag."""
    return "".join(ch for ch in text if unicodedata.category(ch) != "Cf")


def harden(text: str) -> str:
    """What: make an untrusted string safe to place inside a wrapper.
    Use when: on every string of a tool result or chunk before wrapping. Inputs: text.
    Output: text with format characters removed and every wrapper tag or look-alike opened with "&lt;".
    Side effects: none. Role: s5.3 escaper, hardened (idempotent: "&lt;" is never matched again)."""
    return _LOOKALIKE_RE.sub("&lt;", _strip_format_chars(text))


def harden_obj(obj: Any) -> Any:
    """What: apply harden to every string value and dict key in nested JSON-like data.
    Use when: before wrap_result. Inputs: data. Output: a new hardened copy. Side effects: none.
    Role: every string field of every tool result (s5.3)."""
    if isinstance(obj, str):
        return harden(obj)
    if isinstance(obj, dict):
        return {harden(k) if isinstance(k, str) else k: harden_obj(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [harden_obj(v) for v in obj]
    return obj


def wrap_result(name: str, result: Any) -> str:
    """What: '<tool_result name="{tool}" untrusted="true">{json}</tool_result>' with hardened strings.
    Use when: any tool result except search_kb. Inputs: tool name, JSON-like result. Output: wrapped text.
    Side effects: none. Role: s5.3 "every tool result is wrapped"."""
    return wrap_tool_result(name, harden_obj(result))


def wrap_search(verdict: str, instruction: str | None, docs: list[dict[str, Any]]) -> str:
    """What: the search_kb result: the verdict JSON (plus the clarify instruction) followed by one <doc> block
    per relevant chunk, all inside one tool_result wrapper.
    Use when: ToolGuard wraps a search_kb result. Inputs: verdict, instruction or None, docs (id, article,
    section, text, injection_suspect). Output: wrapped text. Side effects: none.
    Role: s5.1 search_kb shape and s5.3 "<doc ...> blocks inside the tool_result"; the doc blocks are built
    with the contracts wrap_doc on hardened text, so they are not escaped a second time."""
    head: dict[str, Any] = {"verdict": verdict, "relevant_chunks": len(docs)}
    if instruction:
        head["instruction"] = instruction
    body = json.dumps(harden_obj(head), ensure_ascii=False, separators=(",", ":"))
    blocks = "".join(wrap_doc(harden(d["id"]), harden(d["article"]), harden(d["section"]), harden(d["text"]),
                              bool(d.get("injection_suspect"))) for d in docs)
    return f'<tool_result name="search_kb" untrusted="true">{body}{blocks}</tool_result>'


def unknown_result(status: str, **extra: Any) -> dict[str, Any]:
    """What: the payload for a tool call whose outcome the model must treat as unknown.
    Use when: a tool failed, timed out, was skipped, blocked by a limit, or never ran.
    Inputs: status word, extra fields. Output: {"status", **extra, "result": "unknown", "note": UNKNOWN_NOTE}.
    Side effects: none. Role: spike 9 lesson (a not_run result was read as "no tickets")."""
    return {"status": status, **extra, "result": "unknown", "note": UNKNOWN_NOTE}


def not_run_tool_message(tool_call: dict[str, Any]) -> ToolMessage:
    """What: the repair ToolMessage for a dangling tool call (doc 07 s2.4 step 10), wrapped and marked unknown.
    Use when: ingress finds an AI message with tool calls that have no results. Inputs: the tool call dict.
    Output: ToolMessage(status="error") whose payload is {"status":"not_run","reason":"turn_interrupted",
    "result":"unknown", "note": ...}. Side effects: none. Role: repair content (additive fields only)."""
    name = str(tool_call.get("name") or "tool")
    payload = unknown_result(STATUS_NOT_RUN["status"], reason=STATUS_NOT_RUN["reason"])
    return ToolMessage(content=wrap_result(name, payload), tool_call_id=str(tool_call["id"]), name=name,
                       status="error")
