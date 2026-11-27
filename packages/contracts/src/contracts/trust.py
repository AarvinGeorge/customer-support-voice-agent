"""What: untrusted wrapper tags, the escaper and the wrapper formats (doc 07 s5.3).
Use when: knowledge sanitizes chunk text at ingest; the agent wraps every tool result, chunk,
account context and client voice item before it reaches the model.
Inputs: none.
Output: UNTRUSTED_WRAPPER_TAGS, escape_untrusted, escape_untrusted_obj, wrap_tool_result, wrap_doc,
wrap_account_context, wrap_voice_transcript.
Side effects: none.
Role: the prompt-injection boundary; text inside a wrapper is data, never instructions."""
import json
import re
from html import escape as _attr_escape
from typing import Any

UNTRUSTED_WRAPPER_TAGS = ["doc", "policy_chunk", "tool_result", "account_context", "voice_transcript",
                          "system", "tool"]

_ALT = "|".join(sorted((re.escape(t) for t in UNTRUSTED_WRAPPER_TAGS), key=len, reverse=True))
_TAG_RE = re.compile(rf"<(?=\s*/?\s*(?:{_ALT})(?![A-Za-z0-9_-]))", re.IGNORECASE)


def escape_untrusted(text: str) -> str:
    """What: replace "<" with "&lt;" before any opening or closing tag named in UNTRUSTED_WRAPPER_TAGS.
    Use when: on every string that goes inside a wrapper. Inputs: text.
    Output: escaped text (other tags such as <b> are kept). Side effects: none. Role: s5.3 escaper."""
    return _TAG_RE.sub("&lt;", text)


def escape_untrusted_obj(obj: Any) -> Any:
    """What: apply escape_untrusted to every string value (and dict key) in nested data.
    Use when: wrapping a tool result. Inputs: JSON-like data. Output: a new escaped copy.
    Side effects: none (input not mutated). Role: wrap-time escaper for every string field."""
    if isinstance(obj, str):
        return escape_untrusted(obj)
    if isinstance(obj, dict):
        return {escape_untrusted(k) if isinstance(k, str) else k: escape_untrusted_obj(v)
                for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [escape_untrusted_obj(v) for v in obj]
    return obj


def _attr(value: str) -> str:
    """What: escape a value for an XML-like attribute. Use when: building wrapper attributes.
    Inputs: value. Output: escaped value. Side effects: none. Role: helper."""
    return _attr_escape(value, quote=True)


def wrap_tool_result(name: str, result: Any) -> str:
    """What: wrap one tool result as untrusted JSON.
    Use when: ToolGuard returns any tool result to the model. Inputs: tool name, JSON-like result.
    Output: '<tool_result name="{tool}" untrusted="true">{json}</tool_result>'. Side effects: none.
    Role: doc 07 s5.3 (every tool result, not only chunks)."""
    body = json.dumps(escape_untrusted_obj(result), ensure_ascii=False, separators=(",", ":"))
    return f'<tool_result name="{_attr(name)}" untrusted="true">{body}</tool_result>'


def wrap_doc(chunk_id: str, article: str, section: str, text: str, injection_suspect: bool = False) -> str:
    """What: wrap one retrieved chunk. Use when: building search_kb's chunks list.
    Inputs: chunk id, article slug, section, text, injection flag.
    Output: '<doc id=".." article=".." section=".." untrusted="true">text</doc>'. Side effects: none.
    Role: doc 07 s5.3 (injection_suspect passed as an attribute)."""
    flag = ' injection_suspect="true"' if injection_suspect else ""
    return (f'<doc id="{_attr(chunk_id)}" article="{_attr(article)}" section="{_attr(section)}" '
            f'untrusted="true"{flag}>{escape_untrusted(text)}</doc>')


def wrap_account_context(context: Any) -> str:
    """What: wrap the MeContext snapshot. Use when: ingress step 11 adds it to the human message.
    Inputs: JSON-like context. Output: '<account_context untrusted="true">{json}</account_context>'.
    Side effects: none. Role: doc 07 s2.6, s5.3."""
    body = json.dumps(escape_untrusted_obj(context), ensure_ascii=False, separators=(",", ":"))
    return f'<account_context untrusted="true">{body}</account_context>'


def wrap_voice_transcript(text: str) -> str:
    """What: wrap merged client voice chatter. Use when: ingress step 12.
    Inputs: text. Output: '<voice_transcript source="client" untrusted="true">..</voice_transcript>'.
    Side effects: none. Role: doc 07 s2.6, s5.3."""
    return f'<voice_transcript source="client" untrusted="true">{escape_untrusted(text)}</voice_transcript>'
