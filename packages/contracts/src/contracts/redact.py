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
import unicodedata
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


# ---- doc 07 s11.1 PIN scanner (fix round 1: review PR #57 I1, I2, M1, M2) ----

_UNITS = {"zero": "0", "oh": "0", "one": "1", "two": "2", "three": "3", "four": "4", "five": "5", "six": "6",
          "seven": "7", "eight": "8", "nine": "9"}
_TEENS = {"ten": "10", "eleven": "11", "twelve": "12", "thirteen": "13", "fourteen": "14", "fifteen": "15",
          "sixteen": "16", "seventeen": "17", "eighteen": "18", "nineteen": "19"}
_TENS = {"twenty": "2", "thirty": "3", "forty": "4", "fourty": "4", "fifty": "5", "sixty": "6", "seventy": "7",
         "eighty": "8", "ninety": "9"}
_DASHES = "‐‑‒–—―−"
_W_TENS = "|".join(_TENS)
_W_UNITS = "one|two|three|four|five|six|seven|eight|nine"
# One spoken or typed number item: a tens pair ("forty-eight"), a tens word, a teen, a unit word, or a 1 or 2 digit
# numeral. Items join with spaces, commas or dashes into a run; a full stop ends a run.
_ITEM = (rf"(?:\b(?:{_W_TENS})(?:[\s{_DASHES}-]?(?:{_W_UNITS})\b)?|\b(?:{'|'.join(_TEENS)})\b|\b(?:zero|oh|{_W_UNITS})\b"
         r"|(?<!\d)\d{1,2}(?!\d))")
_SEP = rf"[ \t,{_DASHES}-]+"
_RUN_RE = re.compile(rf"(?<![\w$€£])(?<!\d[-/.:])(?:(?P<qs>(?<![A-Za-z])q\s?\.?\s?s\.?)[\s,.{_DASHES}-]*)?"
                     rf"(?P<run>{_ITEM}(?:{_SEP}{_ITEM})+)(?![\w])(?![-/.:]\d)", re.IGNORECASE)
_ITEM_RE = re.compile(_ITEM, re.IGNORECASE)
_LONG_OPEN, _LONG_CLOSE = "", ""  # marks a spoken or grouped run longer than 4 digits
_ACCOUNT_SEP = rf"[\s_#:.{_DASHES}-]*"
_ACCOUNT_RE = re.compile(rf"(?<![A-Za-z])QS{_ACCOUNT_SEP}\d{{5}}(?!\d)", re.IGNORECASE)
_GLUED_RE = re.compile(rf"((?<![A-Za-z])QS{_ACCOUNT_SEP}\d{{5}})(\d{{4}})(?!\d)", re.IGNORECASE)
_PIN_DOTS_RE = re.compile(r"\bp\.\s?i\.\s?n\b\.?", re.IGNORECASE)
_MONTH = r"(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?"
_YEAR = r"(?:19|20)\d{2}"
_DATES = (
    re.compile(r"(?<!\d)\d{4}-\d{2}-\d{2}(?!\d)"),
    re.compile(r"(?<!\d)\d{1,2}/\d{1,2}/\d{2,4}(?!\d)"),
    re.compile(rf"\b{_MONTH}\s+(?:\d{{1,2}}(?:st|nd|rd|th)?,?\s+)?{_YEAR}\b", re.IGNORECASE),
    re.compile(rf"\b\d{{1,2}}(?:st|nd|rd|th)?\s+{_MONTH},?\s+{_YEAR}\b", re.IGNORECASE),
)
_CANDIDATE_RE = re.compile(rf"(?<!\d)\d{{4}}(?!\d)|{_LONG_OPEN}\d+{_LONG_CLOSE}")
_AMOUNT_BEFORE_RE = re.compile(r"[$€£]\s?$")
_TOKEN_RE = re.compile(r"[A-Za-z]+(?:'[A-Za-z]+)?|\d+")
_PIN_WORDS = frozenset({"pin", "pins", "pin's", "pincode", "pincodes", "passcode", "passcodes", "passkey", "code",
                        "codes"})
# "error code", "promo code" and the like are not credential words.
_NOT_PIN_CODE = frozenset({"error", "promo", "discount", "coupon", "zip", "postal", "area", "country", "reference",
                           "tracking", "source", "status", "gift", "voucher"})
# doc 07 s7.3 fillers plus a few conversational ones: a message of only these and the candidate is a bare PIN.
_FILLERS = frozenset({"ok", "okay", "sure", "please", "thanks", "thank", "you", "great", "perfect", "alright", "all",
                      "right", "um", "uh", "hi", "hello", "hey", "it", "it's", "its", "is", "my", "here", "the"})


@dataclass(frozen=True)
class PinScan:
    """What: the result of scanning one string. Use when: ingress step 9a decides on the s7.3 typed-PIN refusal.
    Inputs: text (redacted and normalized when something was redacted, else the original), redacted (candidates
    replaced), credential_like (a PIN was redacted and the message has an account id or a PIN word, or is only the
    PIN and fillers). Output: value. Side effects: none. Role: scanner output."""

    text: str
    redacted: int
    credential_like: bool


def _ascii_digits(text: str) -> str:
    """What: NFKC, then every Unicode decimal digit (full-width, Arabic-Indic...) as its ASCII digit.
    Use when: first step of normalization. Inputs: text. Output: text. Side effects: none. Role: review M1."""
    norm = unicodedata.normalize("NFKC", text)
    return "".join(str(unicodedata.decimal(c)) if c.isdecimal() and not c.isascii() else c for c in norm)


def _item_digits(item: str) -> str:
    """What: the digits of one number item. Use when: collapsing a run. Inputs: item text. Output: digits.
    Side effects: none. Role: helper."""
    low = item.lower()
    if low.isdigit():
        return low
    if low in _UNITS:
        return _UNITS[low]
    if low in _TEENS:
        return _TEENS[low]
    parts = re.split(rf"[\s{_DASHES}-]", low)
    return _TENS[parts[0]] + (_UNITS[parts[-1]] if len(parts) > 1 else "0")


def normalize_spoken_digits(text: str) -> str:
    """What: collapse number runs: four digits become "4821"; "QS" plus five (or nine) digits becomes "QS-10423"
    (plus " 4821"); a longer run becomes a marked long run (a candidate only under context or while awaiting); a
    shorter run stays as written. A leading "oh" is a word, not a zero.
    Use when: scan_pins, before the candidate rule. Inputs: text. Output: text. Side effects: none.
    Role: doc 07 s11.1 input ("four eight two one" becomes "4821"), pairs ("forty-eight twenty-one", "48 21"),
    grouped digits ("4-8-2-1") and the account form of rule V."""

    def collapse(m: re.Match[str]) -> str:
        """What: replace one run. Use when: re.sub. Inputs: match. Output: text. Side effects: none. Role: helper."""
        items = list(_ITEM_RE.finditer(m.group("run")))
        lead = ""
        while items and items[0].group().lower() == "oh" and not m.group("qs"):
            items = items[1:]
        if len(items) < 2:
            return m.group(0)
        lead = m.group(0)[:m.start("run") - m.start() + items[0].start()]
        digits = "".join(_item_digits(i.group()) for i in items)
        if m.group("qs") and len(digits) in (5, 9):
            return f"QS-{digits[:5]}" + (f" {digits[5:]}" if digits[5:] else "")
        if len(digits) == 4:
            return lead + digits
        if len(digits) > 4:
            return lead + _LONG_OPEN + digits + _LONG_CLOSE
        return m.group(0)

    return _RUN_RE.sub(collapse, text)


def _normalize(text: str) -> str:
    """What: the full normalization: ASCII digits, "P.I.N." as PIN, an account id glued to four digits split,
    number runs collapsed. Use when: scan_pins. Inputs: text. Output: text. Side effects: none. Role: helper."""
    norm = _ascii_digits(text)
    norm = _PIN_DOTS_RE.sub("PIN", norm)
    norm = _GLUED_RE.sub(r"\1 \2", norm)
    return normalize_spoken_digits(norm)


def _has_pin_word(tokens: list[str]) -> bool:
    """What: whether a credential word is present ("code" only when not "error code" and the like).
    Use when: scan_pins. Inputs: lowercased tokens. Output: bool. Side effects: none. Role: helper."""
    for i, t in enumerate(tokens):
        if t in _PIN_WORDS and not (t.startswith("code") and i > 0 and tokens[i - 1] in _NOT_PIN_CODE):
            return True
    return False


def scan_pins(text: str, context: RedactContext) -> PinScan:
    """What: the doc 07 s11.1 scanner with the fix round 1 context rule.
    Context (an account id, or the word PIN, code or passcode, anywhere in the message): every 4-digit group and every
    long number run is a candidate (with an account id only, amounts and full dates are kept) and the message is
    credential-looking. No context: a candidate (not in a date, decimal or amount) is redacted when the session awaits
    verification, a step-up is pending, or the message is only the candidate and fillers.
    Use when: ingress step 9a, POST /transcript, and every string inside redact.
    Inputs: text, context. Output: PinScan (the original text when nothing was redacted). Side effects: none.
    Role: the one free-text PIN rule; ingress applies it, the model only ever sees its output."""
    norm = _normalize(text)
    cands = list(_CANDIDATE_RE.finditer(norm))
    if not cands:
        return PinScan(text, 0, False)
    tokens = [m.group().lower() for m in _TOKEN_RE.finditer(norm)]
    pin_word = _has_pin_word(tokens)
    account = bool(_ACCOUNT_RE.search(norm))
    if not pin_word:
        dates = [m.span() for rx in _DATES for m in rx.finditer(norm)]
        keep_decimal = not account

        def excluded(m: re.Match[str]) -> bool:
            """What: kept shapes outside PIN-word context. Use when: filtering. Inputs: candidate. Output: bool.
            Side effects: none. Role: amounts, dates (and decimals without an account id) are not PINs."""
            a, b = m.span()
            if _AMOUNT_BEFORE_RE.search(norm[:a]) or any(x <= a < y for x, y in dates):
                return True
            return keep_decimal and (bool(re.search(r"\d[.,]$", norm[:a])) or bool(re.match(r"[.,]\d", norm[b:])))

        cands = [m for m in cands if not excluded(m)]
        if not cands:
            return PinScan(text, 0, False)
    spans = [m.span() for m in cands]
    only_fillers = all(t in _FILLERS for t in _TOKEN_RE.findall(_CANDIDATE_RE.sub(" ", norm).lower()))
    if pin_word or account:
        hits = cands
    elif context.awaiting_verification or context.step_up_pending or only_fillers:
        hits = cands
    else:
        return PinScan(text, 0, False)
    out = norm
    for a, b in reversed(spans if hits is cands else [m.span() for m in hits]):
        out = out[:a] + PIN_TOKEN + out[b:]
    out = out.replace(_LONG_OPEN, "").replace(_LONG_CLOSE, "")
    return PinScan(out, len(hits), pin_word or account or only_fillers)


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
