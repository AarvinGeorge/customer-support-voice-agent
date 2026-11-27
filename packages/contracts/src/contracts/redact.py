"""What: the redact(obj, context) interface (doc 07 s11) with its field-level rules.
Use when: before anything is written to turn_events, transcript_items, logs, audit or LangSmith.
Inputs: none.
Output: RedactContext, redact, scan_text, scan_pins, PinScan, normalize_spoken_digits, hide_for_langsmith,
mask_email, PIN_FIELD_NAMES, PIN_TOKEN.
Side effects: none.
Role: one redaction entry point for every sink: card PIN fields are replaced, email addresses are masked and every
string goes through the doc 07 s11.1 free-text PIN scanner (CS-E-03.S7.T9), anywhere in the data, including inside
models, dataclasses and sets. hide_for_langsmith is the LangSmith hide_inputs and hide_outputs function.
"""
import json
import re
from collections.abc import Mapping
from dataclasses import asdict, dataclass, is_dataclass
from datetime import date, datetime, time
from decimal import Decimal
from enum import Enum
from typing import Any
from uuid import UUID

from pydantic import BaseModel

PIN_TOKEN = "[PIN]"
PIN_FIELD_NAMES = frozenset({"pin", "step_up_pin"})
_EMAIL_RE = re.compile(r"\b([A-Za-z0-9._%+-])[A-Za-z0-9._%+-]*@([A-Za-z0-9.-]+\.[A-Za-z]{2,})\b")


@dataclass(frozen=True)
class RedactContext:
    """What: session facts the s11.1 scanner conditions depend on.
    Use when: calling redact. Inputs: awaiting_verification, verified, step_up_pending.
    Output: frozen context. Side effects: none. Role: scanner inputs (conditions 1, 3, 4)."""

    awaiting_verification: bool = False
    verified: bool = False
    step_up_pending: bool = False


def mask_email(email: str) -> str:
    """What: mask an email address to first letter plus domain ("p***@acme.com").
    Use when: an email goes to events, logs, audit or LangSmith. Inputs: email.
    Output: masked string. Side effects: none. Role: doc 07 s11 email rule."""
    return _EMAIL_RE.sub(lambda m: f"{m.group(1)}***@{m.group(2)}", email)


# ---- doc 07 s11.1 PIN scanner ----

_DIGIT_WORDS = {"zero": "0", "oh": "0", "one": "1", "two": "2", "three": "3", "four": "4", "five": "5", "six": "6",
                "seven": "7", "eight": "8", "nine": "9"}
_ITEM = r"(?:zero|oh|one|two|three|four|five|six|seven|eight|nine|(?<!\d)\d(?!\d))"
# A run of two or more spoken digits or single numerals ("four eight two one", "4 8 2 1", "four-eight-two-one"),
# optionally led by a spoken or typed "QS" ("Q S one zero four two three").
_RUN_RE = re.compile(rf"(?<![\w$])(?:(?P<qs>q\s?\.?\s?s\.?)[\s,.-]+)?(?P<run>{_ITEM}(?:[\s,.-]+{_ITEM})+)(?!\w)",
                     re.IGNORECASE)
_ACCOUNT_RE = re.compile(r"\bQS[-\s]?\d{5}\b", re.IGNORECASE)
_MONTH = r"(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?"
_YEAR = r"(?:19|20)\d{2}"
# Spans that are never a candidate: account id, error code, dates (ISO, slash, month name).
_NOT_CANDIDATE = (
    _ACCOUNT_RE,
    re.compile(r"\bE-[A-Z]+-\d{3}\b"),
    re.compile(r"\b\d{4}-\d{2}-\d{2}\b"),
    re.compile(r"\b\d{1,2}/\d{1,2}/\d{2,4}\b"),
    re.compile(rf"\b{_MONTH}\s+(?:\d{{1,2}}(?:st|nd|rd|th)?,?\s+)?{_YEAR}\b", re.IGNORECASE),
    re.compile(rf"\b\d{{1,2}}(?:st|nd|rd|th)?\s+{_MONTH},?\s+{_YEAR}\b", re.IGNORECASE),
)
_CANDIDATE_RE = re.compile(r"(?<!\d[.,])\b\d{4}\b(?![.,]\d)")
_AMOUNT_BEFORE_RE = re.compile(r"[$€£]\s?$")
_TOKEN_RE = re.compile(r"[A-Za-z0-9]+(?:'[A-Za-z]+)?")
_PIN_WORDS = frozenset({"pin", "pins", "pin's"})
# doc 07 s7.3 fillers plus a few conversational ones ("it's 4821", "um, 4821"): a message of only these and the
# candidate is a bare PIN (condition 5).
_FILLERS = frozenset({"ok", "okay", "sure", "please", "thanks", "thank", "you", "great", "perfect", "alright", "all",
                      "right", "um", "uh", "hi", "hello", "hey", "it", "it's", "its", "is", "my", "here", "the"})
_PIN_WINDOW = 3


@dataclass(frozen=True)
class PinScan:
    """What: the result of scanning one string. Use when: ingress step 9a decides on the s7.3 typed-PIN refusal.
    Inputs: text (redacted, spoken digits normalized when something was redacted), redacted (count of candidates
    replaced), credential_like (a PIN was redacted and the message also has the word PIN, an account id, or is only
    the PIN and fillers). Output: value. Side effects: none. Role: scanner output."""

    text: str
    redacted: int
    credential_like: bool


def normalize_spoken_digits(text: str) -> str:
    """What: collapse spoken digit runs: four digits become "4821"; "QS" plus five digits becomes "QS-10423" (plus
    " 4821" when four more follow). Runs of any other length, and lone number words, are left as they are.
    Use when: scan_pins, before the candidate rule. Inputs: text. Output: text. Side effects: none.
    Role: doc 07 s11.1 input ("four eight two one" becomes "4821") and the account form of rule V."""

    def collapse(m: re.Match[str]) -> str:
        """What: replace one run. Use when: re.sub. Inputs: match. Output: text. Side effects: none. Role: helper."""
        items = re.findall(_ITEM, m.group("run"), flags=re.IGNORECASE)
        digits = "".join(_DIGIT_WORDS.get(i.lower()) or i for i in items)
        if m.group("qs") and len(digits) in (5, 9):
            return f"QS-{digits[:5]}" + (f" {digits[5:]}" if digits[5:] else "")
        if len(digits) == 4:
            return (m.group("qs") + " " if m.group("qs") else "") + digits
        return m.group(0)

    return _RUN_RE.sub(collapse, text)


def scan_pins(text: str, context: RedactContext) -> PinScan:
    """What: the doc 07 s11.1 scanner. A candidate (a standalone 4-digit group that is not in an account id, error
    code, date, decimal or after a currency sign) becomes "[PIN]" when the session awaits verification, the word PIN
    is within 3 tokens, the session is unverified and the message has an account id, a step-up is pending, or the
    message is only the candidate and fillers.
    Use when: ingress step 9a, POST /transcript, and every string inside redact.
    Inputs: text, context. Output: PinScan (the original text when nothing was redacted). Side effects: none.
    Role: the one free-text PIN rule; ingress applies it, the model only ever sees its output."""
    norm = normalize_spoken_digits(text)
    blocked = [m.span() for rx in _NOT_CANDIDATE for m in rx.finditer(norm)]
    cands = [m for m in _CANDIDATE_RE.finditer(norm)
             if not any(a <= m.start() < b for a, b in blocked) and not _AMOUNT_BEFORE_RE.search(norm[:m.start()])]
    if not cands:
        return PinScan(text, 0, False)
    tokens = [(m.start(), m.group().lower()) for m in _TOKEN_RE.finditer(norm)]
    cand_starts = {m.start() for m in cands}
    has_account = bool(_ACCOUNT_RE.search(norm))
    has_pin_word = any(t in _PIN_WORDS for _, t in tokens)
    only_fillers = all(s in cand_starts or t in _FILLERS for s, t in tokens)
    session_rule = (context.awaiting_verification or context.step_up_pending
                    or (has_account and not context.verified) or only_fillers)
    index = {s: i for i, (s, _) in enumerate(tokens)}
    hits = []
    for m in cands:
        i = index.get(m.start())
        near = i is not None and any(t in _PIN_WORDS for _, t in tokens[max(i - _PIN_WINDOW, 0):i + _PIN_WINDOW + 1])
        if session_rule or near:
            hits.append(m)
    if not hits:
        return PinScan(text, 0, False)
    out = norm
    for m in reversed(hits):
        out = out[:m.start()] + PIN_TOKEN + out[m.end():]
    return PinScan(out, len(hits), has_pin_word or has_account or only_fillers)


def scan_text(text: str, context: RedactContext) -> str:
    """What: free-text redaction of one string: the s11.1 PIN scanner, then the email mask.
    Use when: inside redact, and wherever one string goes to a sink. Inputs: text, context.
    Output: redacted text. Side effects: none. Role: the single text hook."""
    return mask_email(scan_pins(text, context).text)


_SCALARS = (int, float, bool, type(None), date, datetime, time, UUID, Decimal, Enum)


def redact(obj: Any, context: RedactContext) -> Any:
    """What: return a redacted copy of data: values of PIN fields become "[PIN]", every string (and
    bytes, decoded as UTF-8) goes through scan_text; Pydantic models and dataclasses are dumped to
    dicts first; lists, tuples, sets and frozensets keep their container type.
    Use when: before any sink write and in the LangSmith hide_inputs and hide_outputs functions.
    Inputs: data, context. Output: a new redacted copy.
    Side effects: none (input not mutated); raises TypeError for any other non-scalar type, so an
    unknown object can never pass through unredacted (review PR4 I1).
    Role: doc 07 s11 redact(obj, context)."""
    if isinstance(obj, str):
        return scan_text(obj, context)
    if isinstance(obj, (bytes, bytearray, memoryview)):
        return scan_text(bytes(obj).decode("utf-8", errors="replace"), context)
    if isinstance(obj, BaseModel):
        return redact(obj.model_dump(mode="python"), context)
    if is_dataclass(obj) and not isinstance(obj, type):
        return redact(asdict(obj), context)
    if isinstance(obj, Mapping):
        return {k: (PIN_TOKEN if isinstance(k, str) and k.lower() in PIN_FIELD_NAMES and v is not None
                    else redact(v, context))
                for k, v in obj.items()}
    if isinstance(obj, list):
        return [redact(v, context) for v in obj]
    if isinstance(obj, tuple):
        return tuple(redact(v, context) for v in obj)
    if isinstance(obj, frozenset):
        return frozenset(redact(v, context) for v in obj)
    if isinstance(obj, set):
        return {redact(v, context) for v in obj}
    if isinstance(obj, _SCALARS):
        return obj
    raise TypeError(f"redact cannot inspect {type(obj).__name__}; convert it to plain data first")


# ---- LangSmith second net (doc 07 s11.1: hide_inputs and hide_outputs set to redact) ----

_TRACE_CONTEXT = RedactContext()


def _plain(obj: Any) -> Any:
    """What: JSON round trip of any payload: models dumped, anything else as its string (or its type name when even
    that fails). Use when: redact refused an object it cannot walk. Inputs: object. Output: plain data.
    Side effects: none. Role: fallback so a trace payload is always redacted, never passed through raw."""

    def default(o: Any) -> Any:
        """What: JSON fallback for one object. Use when: json.dumps. Inputs: object. Output: plain value.
        Side effects: none. Role: helper."""
        if isinstance(o, BaseModel):
            return o.model_dump(mode="json")
        try:
            return str(o)
        except Exception:  # noqa: BLE001 - a broken __str__ must not leak or crash tracing
            return f"<{type(o).__name__}>"

    return json.loads(json.dumps(obj, default=default))


def hide_for_langsmith(payload: Any) -> dict[str, Any]:
    """What: the LangSmith Client hide_inputs and hide_outputs function: redact with the default context
    (unverified, nothing awaiting), so the scanner runs a second time on every trace string.
    Use when: langsmith.Client(hide_inputs=hide_for_langsmith, hide_outputs=hide_for_langsmith).
    Inputs: a run's inputs or outputs dict. Output: a redacted dict (never the raw payload).
    Side effects: none. Role: doc 07 s11.1 second net; ingress already redacted with the session's context."""
    try:
        out = redact(payload, _TRACE_CONTEXT)
    except (TypeError, ValueError):
        try:
            out = redact(_plain(payload), _TRACE_CONTEXT)
        except Exception:  # noqa: BLE001 - never send what could not be inspected
            return {"redaction": "failed"}
    return out if isinstance(out, dict) else {"output": out}
