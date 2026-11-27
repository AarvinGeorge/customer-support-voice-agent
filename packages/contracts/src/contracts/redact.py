"""What: the redact(obj, context) interface (doc 07 s11) with its field-level rules.
Use when: before anything is written to turn_events, transcript_items, logs, audit or LangSmith.
Inputs: none.
Output: RedactContext, redact, mask_email, PIN_FIELD_NAMES, PIN_TOKEN.
Side effects: none.
Role: one redaction entry point for every sink. Implemented now: card PIN fields are replaced and
email addresses are masked anywhere in the data, including inside models, dataclasses and sets. The s11.1 free-text PIN scanner plugs in here
(scan_text hook) in CS-E-03.S7.T9; the signature of redact does not change."""
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


def scan_text(text: str, context: RedactContext) -> str:
    """What: free-text redaction of one string: emails masked now; the s11.1 PIN scanner is added by
    CS-E-03.S7.T9 here. Use when: inside redact. Inputs: text, context.
    Output: redacted text. Side effects: none. Role: the single text hook."""
    return mask_email(text)


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
