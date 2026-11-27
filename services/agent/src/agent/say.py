"""What: the `say` builder (doc 07 s6.2 FinalAnswer.say: "speakable: no markdown, IDs as 'ending 8213', amounts in
words"; s3.4 TicketRef): turns a chat line into plain spoken text. Markdown is removed (links keep their text, lists
and headings become sentences), ids become "<noun> ending <last four, one character at a time>" (TCK-26118213 is
"ticket ending eight two one three", the same form core uses in summary_say), "ending 8213" and "reference 0a1f" are
spelled out, amounts are words ("ninety-six dollars"), dates are spoken ("the twenty-first of November"), and other
numbers and percents are words.
Use when: every FinalAnswer (the SSE adapter applies it to `say` before turn.completed), every voice route line and
deterministic voice reply, and any fixed line given to the voice model.
Inputs: a chat line or a model draft. Output: speak(text) -> str; number_words, spell.
Never raises: a token that is not a valid date is spoken as plain numbers, and any unexpected error falls back to
the markdown-stripped text (logged), so model text can never crash a turn. Signs are kept ("minus twenty dollars"),
ordinals are spoken ("the 1st" is "the first"), multi-part ids use the last four characters of the whole id
(INV-2026-0412 is "invoice ending zero four one two").
Side effects: an ERROR log on the fallback. Role: one place for the spoken form; idempotent on its own output.
"""
from __future__ import annotations

import logging
import re

log = logging.getLogger("agent.say")

_ONES = ["zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten", "eleven", "twelve",
         "thirteen", "fourteen", "fifteen", "sixteen", "seventeen", "eighteen", "nineteen"]
_TENS = ["", "", "twenty", "thirty", "forty", "fifty", "sixty", "seventy", "eighty", "ninety"]
_ORDINAL = {"one": "first", "two": "second", "three": "third", "five": "fifth", "eight": "eighth", "nine": "ninth",
            "twelve": "twelfth"}
_MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October",
           "November", "December"]
_MONTH_RE = (r"(Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|June?|July?|Aug(?:ust)?|Sep(?:t(?:ember)?)?"
             r"|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)\b\.?")
_MINUS = "-\u2212"
_ID_NOUNS = {"TCK": "ticket", "CH": "charge", "PA": "request", "QS": "account", "INV": "invoice", "U": "user"}


def number_words(n: int) -> str:
    """What: an integer in words ("one thousand two hundred", "ninety-six"). Use when: amounts and numbers.
    Inputs: n >= 0. Output: words. Side effects: none. Role: helper."""
    if n < 20:
        return _ONES[n]
    if n < 100:
        return _TENS[n // 10] + (f"-{_ONES[n % 10]}" if n % 10 else "")
    for size, name in ((10**9, "billion"), (10**6, "million"), (1000, "thousand"), (100, "hundred")):
        if n >= size:
            head, rest = divmod(n, size)
            return f"{number_words(head)} {name}" + (f" {number_words(rest)}" if rest else "")
    raise AssertionError("unreachable")


def ordinal_words(n: int) -> str:
    """What: an ordinal in words ("twenty-first", "third"). Use when: dates. Inputs: n >= 1. Output: words.
    Side effects: none. Role: helper."""
    words = number_words(n)
    head, sep, last = words.rpartition("-") if "-" in words else ("", "", words)
    if last in _ORDINAL:
        last = _ORDINAL[last]
    elif last.endswith("y"):
        last = last[:-1] + "ieth"
    else:
        last += "th"
    return f"{head}{sep}{last}"


def year_words(y: int) -> str:
    """What: a year as spoken ("twenty twenty-six", "two thousand five"). Use when: dates. Inputs: year.
    Output: words. Side effects: none. Role: helper."""
    if 2000 <= y < 2010:
        return number_words(y)
    head, tail = divmod(y, 100)
    return f"{number_words(head)} {number_words(tail) if tail else 'hundred'}"


def spell(chars: str) -> str:
    """What: characters one at a time: digits as words, letters as capitals ("0a1f" is "zero A one F").
    Use when: ids, ticket endings, references. Inputs: text. Output: words. Side effects: none. Role: helper."""
    return " ".join(_ONES[int(c)] if c.isdigit() else c.upper() for c in chars)


def _date(day: int, month: int, year: int | None) -> str | None:
    """What: "the twenty-first of November[ twenty twenty-six]", or None when day or month is out of range.
    Use when: date rules. Inputs: day, month, year. Output: words or None (the caller keeps the text as written).
    Side effects: none. Role: helper (the form core uses in readback voice lines)."""
    if not (1 <= month <= 12 and 1 <= day <= 31):
        return None
    return f"the {ordinal_words(day)} of {_MONTHS[month - 1]}" + (f" {year_words(year)}" if year else "")


def _month(name: str) -> int:
    """What: month number from a name or abbreviation. Use when: date rules. Inputs: text. Output: 1..12.
    Side effects: none. Role: helper."""
    return [m[:3] for m in _MONTHS].index(name[:3].title()) + 1


def _date_or_text(match: re.Match[str], day: str, month: int | None, year: str | None) -> str:
    """What: the spoken date, or the matched text unchanged when it is not a valid date (the number rule then speaks
    its digits). Use when: the date rules. Inputs: match, day digits, month number (None: unknown), year digits.
    Output: text. Side effects: none. Role: helper."""
    spoken = _date(int(day), month, int(year) if year else None) if month is not None else None
    return spoken if spoken is not None else match.group(0)


def _money(match: re.Match[str]) -> str:
    """What: "$96.50" as "ninety-six dollars and fifty cents", with a sign before or after the "$" as "minus".
    Use when: the amount rule. Inputs: match (groups: sign, sign, dollars, cents). Output: words.
    Side effects: none. Role: helper."""
    minus = "minus " if match.group(1) or match.group(2) else ""
    dollars = int(match.group(3).replace(",", ""))
    cents = int(match.group(4) or 0)
    parts = []
    if dollars:
        parts.append(f"{number_words(dollars)} dollar{'s' if dollars != 1 else ''}")
    if cents:
        parts.append(f"{number_words(cents)} cent{'s' if cents != 1 else ''}")
    return minus + (" and ".join(parts) or "zero dollars")


def _ident(match: re.Match[str]) -> str:
    """What: an id as "<noun> ending <last four spelled>"; the noun is dropped when the word before already says it.
    Use when: the id rule. Inputs: match (groups: word before, prefix, body). Output: words. Side effects: none.
    Role: helper."""
    before, prefix, body = match.group(1) or "", match.group(2), match.group(3).replace("-", "")
    noun = _ID_NOUNS.get(prefix, "")
    lead = "" if not noun or before.strip().lower() == noun else f"{noun} "
    return f"{before}{lead}ending {spell(body[-4:])}"


def _number(match: re.Match[str]) -> str:
    """What: a plain number in words (decimals as "one point five"). Use when: the last rule. Inputs: match.
    Output: words. Side effects: none. Role: helper."""
    return _number_text(match.group(0).replace(",", ""))


def _strip_markdown(text: str) -> str:
    """What: remove markdown: links keep their text, code ticks, emphasis, headings and list markers go; each line
    becomes a sentence. Use when: speak. Inputs: text. Output: text. Side effects: none. Role: helper."""
    text = re.sub(r"!?\[([^\]]*)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"https?://\S+", "", text)
    text = text.replace("`", "")
    text = re.sub(r"(\*\*|__|\*|(?<!\w)_|_(?!\w))", "", text)
    lines = []
    for raw in text.splitlines():
        line = re.sub(r"^\s*(#{1,6}\s+|[-*+]\s+|\d+[.)]\s+|>\s*)", "", raw).strip()
        if line:
            lines.append(line)
    out = ""
    for line in lines:
        if out:
            out += (" " if out[-1] in ".!?:;," else ". ")
        out += line
    return out


def speak(text: str) -> str:
    """What: the spoken form of a line (see module). Use when: building FinalAnswer.say and voice lines.
    Inputs: text (markdown allowed). Output: plain text with no markdown and no raw id; never raises (on an
    unexpected error the markdown-stripped text, logged). Side effects: an ERROR log on the fallback.
    Role: the say builder."""
    stripped = _strip_markdown(text)
    try:
        return _tidy(_spoken(stripped))
    except Exception:  # noqa: BLE001 (untrusted model text must never crash a turn)
        log.exception("say fallback: text spoken as written chars=%d", len(stripped))
        return _tidy(stripped)


def _tidy(s: str) -> str:
    """What: collapse spaces and drop spaces before punctuation. Use when: speak. Inputs: text. Output: text.
    Side effects: none. Role: helper."""
    return re.sub(r"\s+([.,!?])", r"\1", re.sub(r"[ \t]+", " ", s)).strip()


def _month_of(name: str | None) -> int | None:
    """What: _month, or None for no name. Use when: date rules. Inputs: name. Output: 1..12 or None.
    Side effects: none. Role: helper."""
    return _month(name) if name else None


def _spoken(s: str) -> str:
    """What: the ordered rules (ids, references, amounts, dates, ordinals, percents, signs, numbers).
    Use when: speak. Inputs: markdown-free text. Output: spoken text. Side effects: none. Role: helper."""
    s = re.sub(r"(\b\w+\s)?\b([A-Z]{1,4})-([0-9A-Z]{4,}(?:-[0-9A-Z]+)*)\b", _ident, s)
    s = re.sub(r"\b(ending|reference)\s+((?=[0-9A-Fa-f]*\d)[0-9A-Fa-f]{4})\b",
               lambda m: f"{m.group(1)} {spell(m.group(2))}", s)
    s = re.sub(rf"(?:(?<!\w)([{_MINUS}])\s?)?\$\s?([{_MINUS}])?(\d[\d,]*)(?:\.(\d{{2}}))?", _money, s)
    s = re.sub(r"\b(\d{4})-(\d{2})-(\d{2})\b", lambda m: _date_or_text(m, m.group(3), int(m.group(2)), m.group(1)), s)
    s = re.sub(rf"\b(\d{{1,2}})(?:st|nd|rd|th)?\s+{_MONTH_RE}(?:\s+(\d{{4}}))?",
               lambda m: _date_or_text(m, m.group(1), _month_of(m.group(2)), m.group(3)), s)
    s = re.sub(rf"\b{_MONTH_RE}\s+(\d{{1,2}})(?:st|nd|rd|th)?(?:,?\s+(\d{{4}}))?\b",
               lambda m: _date_or_text(m, m.group(2), _month_of(m.group(1)), m.group(3)), s)
    s = re.sub(r"\b(\d+)(?:st|nd|rd|th)\b", lambda m: ordinal_words(int(m.group(1))), s)
    s = re.sub(r"(\d+(?:\.\d+)?)\s?%", lambda m: f"{_number_text(m.group(1))} percent", s)
    s = re.sub(rf"(?<![\w.])[{_MINUS}](?=\d)", "minus ", s)
    return re.sub(r"\d[\d,]*(?:\.\d+)?", _number, s)


def _number_text(raw: str) -> str:
    """What: number_words for a numeric string (decimals as "point"). Use when: percents. Inputs: digits.
    Output: words. Side effects: none. Role: helper."""
    whole, _, frac = raw.partition(".")
    return number_words(int(whole)) + (f" point {spell(frac)}" if frac else "")
