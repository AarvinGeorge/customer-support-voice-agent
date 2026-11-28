"""What: the redact(obj, context) interface (doc 07 s11) with its field-level rules.
Use when: before anything is written to turn_events, transcript_items, logs, audit or LangSmith.
Inputs: none.
Output: RedactContext, redact, redact_system, scan_text, scan_pins, scan_relayed, PinScan, normalize_spoken_digits,
hide_for_langsmith, ours_id, OURS_ID_PREFIX, OURS_FIELDS, mask_email, PIN_FIELD_NAMES, PIN_TOKEN.
Side effects: none.
Role: redaction by provenance (D-071 part 3). Card PIN fields are replaced and email addresses are masked in all data.
The doc 07 s11.1 free-text PIN scanner applies to customer-originated text only: `redact` (every string scanned) is
for what the customer typed or said; `redact_system` is for what our system wrote (assistant text, code-written lines,
tool results, event fields holding code-generated ids), where only text inside a customer frame (<customer_message>,
<voice_transcript> customer lines) is scanned again as a second net. `scan_relayed` is for text the realtime voice
model said: full scan, except our own lines keep the exact ids core issued in the session. hide_for_langsmith treats
text labelled ours (ours_id messages, OURS_FIELDS state) and model and tool messages as system data, and every other
string as unknown provenance (full scan, D-078).
"""
import json
import re
import unicodedata
from collections.abc import Callable, Mapping
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


# ---- doc 07 s11.1 PIN scanner (fix rounds 1 and 2 of PR #57; D-060: defence in depth behind the secure card) ----

_UNITS = {"zero": "0", "oh": "0", "nought": "0", "naught": "0", "one": "1", "two": "2", "three": "3", "four": "4",
          "five": "5", "six": "6", "seven": "7", "eight": "8", "nine": "9"}
_TEENS = {"ten": "10", "eleven": "11", "twelve": "12", "thirteen": "13", "fourteen": "14", "fifteen": "15",
          "sixteen": "16", "seventeen": "17", "eighteen": "18", "nineteen": "19"}
_TENS = {"twenty": "2", "thirty": "3", "forty": "4", "fourty": "4", "fifty": "5", "sixty": "6", "seventy": "7",
         "eighty": "8", "ninety": "9"}
_DASHES = "\u2010\u2011\u2012\u2013\u2014\u2015\u2212"  # Unicode hyphens and dashes, written as escapes
_W_TENS = "|".join(sorted(_TENS, key=len, reverse=True))
_W_UNITS = "one|two|three|four|five|six|seven|eight|nine"
_W_ZERO = "zero|oh|nought|naught"
_W_TEENS = "|".join(sorted(_TEENS, key=len, reverse=True))
_TENS_PAIR_RE = re.compile(rf"({_W_TENS})[\s{_DASHES}-]?({_W_UNITS})?", re.IGNORECASE)
_MULT_RE = re.compile(rf"(double|triple)[\s{_DASHES}-]+({_W_ZERO}|{_W_UNITS}|\d)", re.IGNORECASE)
_CARDINAL_RE = re.compile(
    rf"\b({_W_UNITS})\s+thousand(?:[\s,]+({_W_UNITS})\s+hundred)?(?:[\s,]+and)?"
    rf"(?:[\s,]+({_W_TENS})(?:[\s{_DASHES}-]?({_W_UNITS}))?\b|[\s,]+({_W_TEENS})\b|[\s,]+({_W_UNITS})\b)?",
    re.IGNORECASE)


def _item_pattern(max_digits: int) -> str:
    """What: the regex of one number item: double or triple a digit, a tens pair ("forty-eight", "fortyeight"), a
    teen, a unit or zero word, or a numeral of up to max_digits digits. Use when: building the run regexes.
    Inputs: max digits per numeral. Output: pattern text. Side effects: none. Role: helper."""
    return (rf"(?:\b(?:double|triple)[\s{_DASHES}-]+(?:{_W_ZERO}|{_W_UNITS}|\d)(?!\d)"
            rf"|\b(?:{_W_TENS})(?:[\s{_DASHES}-]?(?:{_W_UNITS}))?\b|\b(?:{_W_TEENS})\b|\b(?:{_W_ZERO}|{_W_UNITS})\b"
            rf"|(?<!\d)\d{{1,{max_digits}}}(?!\d))")


def _run_regex(sep: str, max_digits: int) -> re.Pattern[str]:
    """What: a run of two or more number items joined by sep, optionally led by "QS". Use when: module load.
    Inputs: separator class, max digits per numeral. Output: compiled regex. Side effects: none. Role: helper."""
    item = _item_pattern(max_digits)
    return re.compile(rf"(?<![\w$€£])(?<!\d[-/.:])(?:(?P<qs>(?<![A-Za-z])q\s?\.?\s?s\.?)[\s,.{_DASHES}-]*)?"
                      rf"(?P<run>{item}(?:{sep}{item})+)(?![\w])(?![-/.:]\d)", re.IGNORECASE)


# Strict runs: spaces, commas, dashes (a full stop ends a run). Loose runs, used only when a PIN word is present or a
# verification is awaited: also full stops, slashes and newlines, 3-digit numerals ("4,821", "4.8.2.1", "48\n21") and
# filled pauses between items ("four eight, uh, two one", PR #57 follow-up).
_LOOSE_SEP = rf"[ \t\r\n,./{_DASHES}-]+"
_RUN_STRICT = _run_regex(rf"[ \t,{_DASHES}-]+", 2)
_RUN_LOOSE = _run_regex(rf"{_LOOSE_SEP}(?:(?:uh+|um+|uhm|er+|erm|ah+|hmm+|like){_LOOSE_SEP})*", 3)
_ITEM_STRICT = re.compile(_item_pattern(2), re.IGNORECASE)
_ITEM_LOOSE = re.compile(_item_pattern(3), re.IGNORECASE)
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
# Strong credential words count anywhere in the message.
_STRONG_WORDS = frozenset({"pin", "pins", "pin's", "pincode", "pincodes", "passcode", "passcodes", "passkey",
                           "password", "passwords", "passphrase"})
# Misspelled or joined forms of "pin" (PR #57 follow-up): "pni", "pim", "pinn", "pn", "mypin", "mypinis", "pinnumber".
# A whole-token match, so "pinning", "spinner" and "pinpoint" stay ordinary words.
_PIN_TOKEN_RE = re.compile(r"(?:my|your|ur|our)?(?:pin|pni|pim|pinn|pn)(?:s|is|number|num|no|code|codes)?")
_STRONG_BIGRAMS = frozenset({("secret", "number"), ("secret", "code"), ("security", "code"), ("support", "number")})
# "code" counts only within _NEAR tokens of the number (or while awaiting), and never as "error code" and the like.
_WEAK_WORDS = frozenset({"code", "codes"})
_NOT_PIN_CODE = frozenset({"error", "promo", "discount", "coupon", "zip", "postal", "area", "country", "reference",
                           "tracking", "source", "status", "gift", "voucher", "embed", "verification", "sample"})
_NEAR = 3
# A verified customer has no reason to type a PIN: there "code" counts only when announced ("my code is 4821").
_CODE_CUE_BEFORE = frozenset({"my", "our", "your"})
# Between an account id and a number of an unverified customer: words that still read as "id then PIN"
# ("QS-10423, 4821", "I'm QS-10423 and 4821"); any other word means the number is about something else.
_ACCOUNT_GAP = frozenset({"and", "then", "um", "uh", "ok", "okay", "my", "is", "it's", "its", "here", "the", "number",
                          "num", "no"})
# Words that make a message ending in a PIN word a question about the PIN, not an announcement ("I forgot my PIN").
_ABOUT_PIN = frozenset({"forgot", "forget", "forgotten", "lost", "lose", "reset", "change", "changing", "changed",
                        "update", "recover", "remember", "where", "how", "enter", "find", "set", "locked", "wrong"})
# A year next to one of these is a date, also when a PIN word is in the message ("I set my PIN back in 2023").
_YEAR_BEFORE = frozenset({"in", "since", "from", "of", "during", "until", "till", "before", "after", "by", "back",
                          "year", "early", "late", "mid"})
_YEAR_AFTER = frozenset({"update", "release", "version", "plan", "model", "edition"})
# doc 07 s7.3 fillers plus a few conversational ones: a message of only these and the candidate is a bare PIN.
_FILLERS = frozenset({"ok", "okay", "sure", "please", "thanks", "thank", "you", "great", "perfect", "alright", "all",
                      "right", "um", "uh", "hi", "hello", "hey", "it", "it's", "its", "is", "my", "here", "the",
                      "yeah", "yes", "yep", "yup", "so", "well"})
# Words after a credential word that still mean "the PIN comes next" ("my PIN is", "the code is:").
_CUE_TAIL = frozenset({"is", "was", "number", "it's", "its", "here", "below", "be", "will"})
_ACCOUNT_ONLY = _FILLERS | frozenset({"i", "am", "i'm", "im", "this", "account", "id", "number", "on", "qs"})
# Latin look-alikes from Cyrillic and Greek (tokenization only; "рin" is "pin").
_CONFUSABLES = str.maketrans({"а": "a", "е": "e", "о": "o", "р": "p", "с": "c", "у": "y", "х": "x", "і": "i",
                              "ј": "j", "к": "k", "п": "n", "А": "A", "Е": "E", "О": "O", "Р": "P", "С": "C",
                              "Т": "T", "Х": "X", "І": "I", "К": "K", "М": "M", "Н": "H", "ο": "o", "ρ": "p",
                              "ι": "i", "ν": "v", "Ι": "I", "Ο": "O", "Ρ": "P", "Ν": "N"})


@dataclass(frozen=True)
class PinScan:
    """What: the result of scanning one string. Use when: ingress step 9a decides on the s7.3 typed-PIN refusal and
    on the awaiting flag. Inputs: text (redacted and normalized when something was redacted, else the original),
    redacted (candidates replaced), credential_like (a PIN was redacted next to a credential word, an account id of an
    unverified session, or alone with fillers), expects_pin (unverified, nothing redacted, and the message ends by
    announcing a PIN or is only an account id), moved_on (no number, credential word or account id, and at least 3
    other words). Output: value. Side effects: none. Role: scanner output."""

    text: str
    redacted: int
    credential_like: bool
    expects_pin: bool = False
    moved_on: bool = False


def _ascii_digits(text: str) -> str:
    """What: NFKC; drop format characters and combining marks (zero-width spaces, accents on digits, keycap emoji);
    Latin look-alikes; every Unicode decimal digit as ASCII. Use when: first step of normalization.
    Inputs: text. Output: text. Side effects: none. Role: review M1 and the cheap crafted forms of round 2."""
    norm = unicodedata.normalize("NFKC", text)
    norm = "".join(c for c in norm if unicodedata.category(c) not in ("Cf", "Mn", "Me"))
    norm = norm.translate(_CONFUSABLES)
    return "".join(str(unicodedata.decimal(c)) if c.isdecimal() and not c.isascii() else c for c in norm)


def _item_digits(item: str) -> str:
    """What: the digits of one number item. Use when: collapsing a run. Inputs: item text. Output: digits.
    Side effects: none (an unknown item raises ValueError; scan_pins then masks the whole string). Role: helper."""
    low = item.lower()
    if low.isdigit():
        return low
    if low in _UNITS:
        return _UNITS[low]
    if low in _TEENS:
        return _TEENS[low]
    mult = _MULT_RE.fullmatch(low)
    if mult:
        digit = mult.group(2)
        return (digit if digit.isdigit() else _UNITS[digit]) * (2 if mult.group(1) == "double" else 3)
    pair = _TENS_PAIR_RE.fullmatch(low)
    if pair:
        return _TENS[pair.group(1)] + (_UNITS[pair.group(2)] if pair.group(2) else "0")
    raise ValueError(f"not a number item: {item!r}")


def _cardinal(m: re.Match[str]) -> str:
    """What: "four thousand eight hundred and twenty-one" as "4821". Use when: re.sub. Inputs: match.
    Output: digits, or the text when the value is not 4 digits. Side effects: none. Role: review N13, N15."""
    value = int(_UNITS[m.group(1).lower()]) * 1000
    if m.group(2):
        value += int(_UNITS[m.group(2).lower()]) * 100
    if m.group(3):
        value += int(_TENS[m.group(3).lower()]) * 10 + (int(_UNITS[m.group(4).lower()]) if m.group(4) else 0)
    elif m.group(5):
        value += int(_TEENS[m.group(5).lower()])
    elif m.group(6):
        value += int(_UNITS[m.group(6).lower()])
    return str(value) if 1000 <= value <= 9999 else m.group(0)


def normalize_spoken_digits(text: str, loose: bool = False) -> str:
    """What: collapse number runs: four digits become "4821"; "QS" plus five (or nine) digits becomes "QS-10423"
    (plus " 4821"); a longer run becomes a marked long run (a candidate only under context or while awaiting); a
    shorter run stays as written. A leading "oh" is a word, not a zero. loose also joins items across full stops,
    slashes and newlines and accepts 3-digit numerals.
    Use when: scan_pins, before the candidate rule. Inputs: text, loose flag. Output: text. Side effects: none.
    Role: doc 07 s11.1 input ("four eight two one" becomes "4821"), pairs, grouped digits, "double four"."""
    run_re, item_re = (_RUN_LOOSE, _ITEM_LOOSE) if loose else (_RUN_STRICT, _ITEM_STRICT)

    def collapse(m: re.Match[str]) -> str:
        """What: replace one run. Use when: re.sub. Inputs: match. Output: text. Side effects: none. Role: helper."""
        items = list(item_re.finditer(m.group("run")))
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

    return run_re.sub(collapse, text)


def _normalize(text: str, loose: bool = False) -> str:
    """What: the full normalization: ASCII digits, "P.I.N." as PIN, an account id glued to four digits split,
    cardinals, number runs collapsed. Use when: scan_pins. Inputs: text, loose flag. Output: text.
    Side effects: none. Role: helper."""
    norm = _ascii_digits(text)
    norm = _PIN_DOTS_RE.sub("PIN", norm)
    norm = _GLUED_RE.sub(r"\1 \2", norm)
    norm = _CARDINAL_RE.sub(_cardinal, norm)
    return normalize_spoken_digits(norm, loose)


def _fallback(text: str) -> PinScan:
    """What: the result when scanning failed: every digit group and every number word masked.
    Use when: scan_pins caught an exception. Inputs: text. Output: PinScan. Side effects: none.
    Role: review round 2 N-I1: the scanner never raises and never lets text through unscanned."""
    words = "|".join([_W_TENS, _W_TEENS, _W_UNITS, _W_ZERO, "double", "triple", "thousand", "hundred"])
    out, n = re.subn(rf"\d+|[^\W\d_]*(?:{words})[^\W\d_]*", PIN_TOKEN, text, flags=re.IGNORECASE)
    return PinScan(out, max(n, 1), False)


def scan_pins(text: str, context: RedactContext, issued: frozenset[str] = frozenset()) -> PinScan:
    """What: the doc 07 s11.1 scanner (fix rounds 1 and 2, D-060). A candidate is a 4-digit group, a spoken or grouped
    number run, or a cardinal, after normalization. It is redacted when: a strong credential word (PIN, passcode,
    password, secret number...) is anywhere in the message (a far year after "in", "since"... is still a date); "code"
    is within 3 tokens; the session is unverified and the message has an account id (verified: only when the number
    sits right next to it); the session awaits verification or a step-up; or the message is only the number and
    fillers. Without a credential word, amounts, dates and decimals are kept.
    Use when: ingress step 9a, POST /transcript, and every string inside redact.
    Inputs: text, context; issued: exact ids our code received from core for the session (ticket short ids,
    invoice endings), kept by provenance (D-071) only inside a span scan_relayed marked as one of our own lines;
    empty for customer text.
    Output: PinScan (the original text when nothing was redacted). Side effects: none; never
    raises (on an internal error every number is masked). Role: the one free-text PIN rule."""
    try:
        return _scan(text, context, issued)
    except Exception:  # noqa: BLE001 - D-060: never crash, never pass unscanned text
        return _fallback(text)


def _scan(text: str, context: RedactContext, issued: frozenset[str] = frozenset()) -> PinScan:
    """What: scan_pins without the safety net. Use when: scan_pins. Inputs: text, context, issued numbers.
    Output: PinScan. Side effects: none. Role: helper."""
    strict = _normalize(text)
    words = [m.group().lower() for m in _TOKEN_RE.finditer(strict)]
    strong = any(_is_strong(w) for w in words) or any(b in _STRONG_BIGRAMS for b in zip(words, words[1:]))
    weak = any(w in _WEAK_WORDS and not (i and words[i - 1] in _NOT_PIN_CODE) for i, w in enumerate(words))
    awaiting = context.awaiting_verification or context.step_up_pending
    norm = _normalize(text, loose=True) if (strong or weak or awaiting) else strict
    tokens = [(m.start(), m.group().lower()) for m in _TOKEN_RE.finditer(norm)]
    account = [m.span() for m in _ACCOUNT_RE.finditer(norm)]
    cands = list(_CANDIDATE_RE.finditer(norm))
    cred_idx = [i for i, (_, w) in enumerate(tokens) if _is_strong(w) or _is_code_word(tokens, i, context, norm)]
    filler_only = all(w in _FILLERS for w in _TOKEN_RE.findall(_CANDIDATE_RE.sub(" ", norm).lower()))
    if not cands:
        return PinScan(_unmark(text), 0, False, _expects_pin(tokens, cred_idx, account, context),
                       _moved_on(tokens, cred_idx, account, norm))
    dates = [m.span() for rx in _DATES for m in rx.finditer(norm)]
    index = {s: i for i, (s, _) in enumerate(tokens)}
    hits: list[re.Match[str]] = []
    cred = False
    for m in cands:
        a, b = m.span()
        if issued and m.group().strip(_LONG_OPEN + _LONG_CLOSE) in issued and _in_span(norm, a):
            continue  # D-071: an id core issued, inside one of our own sentences (ticket ending 8352)
        i = index.get(a + (1 if norm[a] == _LONG_OPEN else 0), 0)
        near = [j for j in cred_idx if abs(j - i) <= _NEAR]
        strong_here = strong and not _is_far_year(m.group(), i, tokens, near)
        weak_here = any(tokens[j][1] in _WEAK_WORDS for j in near) or (weak and awaiting)
        adjacent = any(_adjacent(norm, (a, b), span, gap_words=not context.verified) for span in account)
        kept_shape = bool(_AMOUNT_BEFORE_RE.search(norm[:a])) or any(x <= a < y for x, y in dates)
        decimal = bool(re.search(r"\d[.,]$", norm[:a])) or bool(re.match(r"[.,]\d", norm[b:]))
        if strong_here or weak_here:
            hits.append(m)
            cred = True
        elif account and adjacent and not kept_shape:
            hits.append(m)
            cred = True
        elif (awaiting or filler_only) and not kept_shape and not (decimal and not account):
            hits.append(m)
            cred = cred or filler_only
    if not hits:
        return PinScan(_unmark(text), 0, False, _expects_pin(tokens, cred_idx, account, context), False)
    out = norm
    for m in reversed(hits):
        out = out[:m.start()] + PIN_TOKEN + out[m.end():]
    out = out.replace(_LONG_OPEN, "").replace(_LONG_CLOSE, "")
    return PinScan(_unmark(out), len(hits), cred)


def _is_far_year(group: str, i: int, tokens: list[tuple[int, str]], near: list[int]) -> bool:
    """What: a 19xx or 20xx group far from every credential word and next to a date word ("back in 2023").
    Use when: _scan. Inputs: candidate text, its token index, tokens, credential words near it. Output: bool.
    Side effects: none. Role: review round 2 N-I2 (PIN plus a year is not a PIN)."""
    if near or not re.fullmatch(_YEAR, group):
        return False
    before = tokens[i - 1][1] if i > 0 else ""
    after = tokens[i + 1][1] if i + 1 < len(tokens) else ""
    return before in _YEAR_BEFORE or after in _YEAR_AFTER


def _adjacent(norm: str, cand: tuple[int, int], account: tuple[int, int], gap_words: bool = False) -> bool:
    """What: only separators between the number and the account id (with gap_words, also "and", "my", "is" and the
    like). Use when: the account id rule (s11.1 condition 3, narrowed by the PR #57 follow-up so "QS-20318 ... from
    1200 to 1500 seats" is kept). Inputs: text, candidate span, account span, gap_words flag. Output: bool.
    Side effects: none. Role: helper."""
    gap = norm[account[1]:cand[0]] if account[1] <= cand[0] else norm[cand[1]:account[0]]
    if re.search(r"\d", gap):
        return False
    return all(w.lower() in _ACCOUNT_GAP for w in _TOKEN_RE.findall(gap)) if gap_words else not re.search(
        r"[A-Za-z]", gap)


def _is_strong(word: str) -> bool:
    """What: a strong credential word: the listed words, or "pin" misspelled or joined ("pni", "mypin", "pinnumber").
    Use when: _scan. Inputs: one lowercased token. Output: bool. Side effects: none.
    Role: PR #57 follow-up (re-review r2 I1)."""
    return word in _STRONG_WORDS or bool(_PIN_TOKEN_RE.fullmatch(word))


def _is_code_word(tokens: list[tuple[int, str]], i: int, context: RedactContext, norm: str) -> bool:
    """What: token i is "code" meaning a PIN: not "error code" and the like; on a verified session only when announced
    ("my code", "code is", "code: 4821"). Use when: _scan. Inputs: tokens, index, context, normalized text. Output: bool. Side effects: none.
    Role: weak credential word (re-review r2 m2: verified "The code 4012 keeps showing up" is kept)."""
    if tokens[i][1] not in _WEAK_WORDS or (i and tokens[i - 1][1] in _NOT_PIN_CODE):
        return False
    if not context.verified:
        return True
    before = tokens[i - 1][1] if i else ""
    after = tokens[i + 1][1] if i + 1 < len(tokens) else ""
    end = tokens[i][0] + len(tokens[i][1])
    return before in _CODE_CUE_BEFORE or after in _CUE_TAIL or bool(re.match(r"\s*[:#=]", norm[end:]))


def _expects_pin(tokens: list[tuple[int, str]], cred_idx: list[int], account: list[tuple[int, int]],
                 context: RedactContext) -> bool:
    """What: an unverified message that ends by announcing a PIN ("my PIN is") or is only an account id; a question
    about the PIN ("I forgot my PIN", "where do I enter my PIN") does not count.
    Use when: _scan, when nothing was redacted. Inputs: tokens, credential word indexes, account spans, context.
    Output: bool. Side effects: none. Role: review round 2 N18, N20 (a PIN split over two messages)."""
    if context.verified:
        return False
    if cred_idx and all(w in _CUE_TAIL for _, w in tokens[cred_idx[-1] + 1:]):
        return not any(w in _ABOUT_PIN for _, w in tokens[:cred_idx[-1]])
    if account:
        rest = [w for s, w in tokens if not any(a <= s < b for a, b in account)]
        return all(w in _ACCOUNT_ONLY for w in rest)
    return False


def _moved_on(tokens: list[tuple[int, str]], cred_idx: list[int], account: list[tuple[int, int]], norm: str) -> bool:
    """What: a substantive message with no number group, credential word or account id (the topic moved on).
    Use when: _scan, when there is no candidate. Inputs: tokens, credential indexes, account spans, text.
    Output: bool. Side effects: none. Role: clears the awaiting flag (fix round 2)."""
    if cred_idx or account or re.search(r"\d", norm):
        return False
    return sum(1 for _, w in tokens if w not in _FILLERS) >= 3


def scan_text(text: str, context: RedactContext, issued: frozenset[str] = frozenset()) -> str:
    """What: free-text redaction of one customer-originated string: the s11.1 PIN scanner, then the email mask.
    Use when: inside redact, and wherever one customer string goes to a sink. Inputs: text, context, issued numbers
    (see scan_pins). Output: redacted text. Side effects: none. Role: the single customer text hook."""
    return mask_email(scan_pins(text, context, issued).text)


_SENTENCE_RE = re.compile(r"[^.!?\n]+(?:[.!?]+|$)")
_SPAN_OPEN, _SPAN_CLOSE = "\uf8f2", "\uf8f3"  # private-use span markers (not the long-run markers); no word chars


def _unmark(text: str) -> str:
    """What: drop the span markers. Use when: every scan_pins result. Inputs: text. Output: text.
    Side effects: none. Role: helper (markers never leave the scanner)."""
    return text.replace(_SPAN_OPEN, "").replace(_SPAN_CLOSE, "")


def _in_span(norm: str, pos: int) -> bool:
    """What: position pos lies inside a marked span (the last open marker before it has no close marker after it).
    Use when: _scan, before keeping an issued id. Inputs: normalized text, position. Output: bool.
    Side effects: none. Role: review-pr76-r1 N1: the exemption covers only the exact matched sentence."""
    return norm.rfind(_SPAN_OPEN, 0, pos) > norm.rfind(_SPAN_CLOSE, 0, pos)


def _sentence_key(sentence: str) -> str:
    """What: a sentence compared by meaning of its characters: scanner normalization (spoken digits as numerals),
    lower case, letters and digits only. Use when: scan_relayed matches a relayed sentence to one of our lines.
    Inputs: sentence. Output: key. Side effects: none. Role: helper."""
    norm = _unmark(_normalize(sentence).replace(_LONG_OPEN, "").replace(_LONG_CLOSE, "")).lower()
    return " ".join(re.findall(r"[a-z0-9]+", norm))


def scan_relayed(text: str, context: RedactContext, lines: Any, issued: frozenset[str]) -> str:
    """What: redact text the realtime voice model said aloud. That model heard the customer, so the whole item is
    scanned once as one message with the session context (every credential word in it counts, also one inside our
    own sentence). The only exemption: an id in `issued` (the exact ids core gave this session) that lies inside a
    sentence that IS one of our own lines (same key as a sentence of `lines`) and has words, not only a number.
    Use when: POST /transcript, for every posted item (the browser's role label is not trusted).
    Inputs: text, session context, our lines (the agent answers handed to the relay), exact issued ids.
    Output: redacted text (emails masked). Side effects: none; never raises (scan_pins never raises).
    Role: D-071 part 3 (review-pr76 I1; rereview-pr76-r1 N1, N2): "Thanks, you said your PIN is 8352" matches none
    of our lines and is masked; "Ticket no. 8352." never makes a bare "8352" ours."""
    text = _unmark(text)  # posted marker characters must never open or close a span
    ours = {k for line in lines if line for sent in _SENTENCE_RE.findall(line)
            if re.search(r"[a-z]", k := _sentence_key(sent))}

    def mark(m: re.Match[str]) -> str:
        """What: wrap one of our sentences in span markers. Use when: re.sub. Inputs: match. Output: text.
        Side effects: none. Role: helper."""
        return _SPAN_OPEN + m.group() + _SPAN_CLOSE if _sentence_key(m.group()) in ours else m.group()

    marked = _SENTENCE_RE.sub(mark, text) if ours and issued else text
    return mask_email(scan_pins(marked, context, issued).text)


# Customer frames inside system text: the prompt frame (agent.prompt) and merged voice chatter (contracts.trust).
_CUSTOMER_FRAME_RE = re.compile(r"(<customer_message>)(.*?)(</customer_message>)"
                                r"|(<voice_transcript\b[^>]*>)(.*?)(</voice_transcript>)", re.DOTALL)


def _system_text(text: str) -> str:
    """What: redaction of one system-originated string: emails masked; the PIN scanner only inside customer frames
    (the whole <customer_message>; the "customer" lines of a <voice_transcript>, whose "voice assistant" lines were
    redacted when posted) with the default context (unverified, nothing awaiting). Use when: redact_system. Inputs: text. Output: text.
    Side effects: none. Role: D-071: our own ids and lines keep their numbers; customer frames get a second net."""

    def frame(m: re.Match[str]) -> str:
        """What: scan one frame body. Use when: re.sub. Inputs: match. Output: text. Side effects: none.
        Role: helper."""
        if m.group(1) is not None:
            return m.group(1) + scan_pins(m.group(2), _TRACE_CONTEXT).text + m.group(3)
        body = "".join(scan_pins(ln, _TRACE_CONTEXT).text if ln.lstrip().startswith("customer") else ln
                       for ln in m.group(5).splitlines(keepends=True))  # review-pr76 M1: customer lines only
        return m.group(4) + body + m.group(6)

    return mask_email(_CUSTOMER_FRAME_RE.sub(frame, text))


_SCALARS = (int, float, bool, type(None), date, datetime, time, UUID, Decimal, Enum)


def _walk(obj: Any, text: Callable[[str], str]) -> Any:
    """What: a redacted copy of data: values of PIN fields become "[PIN]", every string (and bytes, decoded as UTF-8)
    goes through `text`; Pydantic models and dataclasses are dumped to dicts first; lists, tuples, sets and
    frozensets keep their container type. Use when: redact and redact_system. Inputs: data, string function.
    Output: a new copy. Side effects: none (input not mutated); raises TypeError for any other non-scalar type, so an
    unknown object can never pass through unredacted (review PR4 I1). Role: the shared walker."""
    if isinstance(obj, str):
        return text(obj)
    if isinstance(obj, (bytes, bytearray, memoryview)):
        return text(bytes(obj).decode("utf-8", errors="replace"))
    if isinstance(obj, BaseModel):
        return _walk(obj.model_dump(mode="python"), text)
    if is_dataclass(obj) and not isinstance(obj, type):
        return _walk(asdict(obj), text)
    if isinstance(obj, Mapping):
        return {k: (PIN_TOKEN if isinstance(k, str) and k.lower() in PIN_FIELD_NAMES and v is not None
                    else _walk(v, text))
                for k, v in obj.items()}
    if isinstance(obj, list):
        return [_walk(v, text) for v in obj]
    if isinstance(obj, tuple):
        return tuple(_walk(v, text) for v in obj)
    if isinstance(obj, frozenset):
        return frozenset(_walk(v, text) for v in obj)
    if isinstance(obj, set):
        return {_walk(v, text) for v in obj}
    if isinstance(obj, _SCALARS):
        return obj
    raise TypeError(f"redact cannot inspect {type(obj).__name__}; convert it to plain data first")


def redact(obj: Any, context: RedactContext) -> Any:
    """What: redact customer-originated data: PIN fields replaced, and every string through scan_text (the s11.1
    scanner with the session's context, then the email mask).
    Use when: text the customer typed or said, at the point it enters (ingress step 9a, POST /transcript user items,
    the covered voice item, customer lines of the voice summary), and data of unknown provenance (core audit args).
    Inputs: data, context. Output: a new redacted copy. Side effects: none; raises TypeError on an object it cannot
    inspect. Role: doc 07 s11 redact(obj, context), scoped by provenance (D-071 part 3)."""
    return _walk(obj, lambda s: scan_text(s, context))


def redact_system(obj: Any) -> Any:
    """What: redact system-originated data: PIN fields replaced, emails masked, and the PIN scanner only inside
    customer frames (<customer_message>, <voice_transcript>), so code-generated ids ("short_id": "8352") and
    code-written lines ("ticket ending 8352") keep their numbers.
    Use when: turn_events payloads, LangSmith run inputs and outputs, agent lines of the voice summary.
    Inputs: data. Output: a new redacted copy. Side effects: none; raises TypeError on an object it cannot inspect.
    Role: D-071 part 3: customer text was already redacted where it entered, so system data is not scanned again
    (the frame scan is the second net for a raw customer string that reached a frame by mistake)."""
    return _walk(obj, _system_text)


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


# Provenance labels our code writes at the source (CS-E-02.F14 fix round 1, default deny). A customer can author
# neither a message id nor a state key, so these labels cannot be forged from customer text.
OURS_ID_PREFIX = "ours:"
# Agent state fields that hold code-held records (agent.state.SupportState; a test there keeps this list complete).
OURS_FIELDS = frozenset({"verified", "claims_snapshot", "context_snapshot", "pending", "weak_streak",
                         "grounding_failures", "last_turn_delivered"})
_SYSTEM_KINDS = frozenset({"ai", "tool", "system", "function", "assistant", "AIMessage", "AIMessageChunk",
                           "ToolMessage", "ToolMessageChunk", "SystemMessage", "FunctionMessage", "ChatGeneration",
                           "ChatGenerationChunk", "Generation"})
_HUMAN_KINDS = frozenset({"human", "user", "HumanMessage", "HumanMessageChunk"})


def ours_id(kind: str) -> str:
    """What: a unique message id that labels a message our code wrote ("ours:<kind>:<hex>").
    Use when: building a HumanMessage whose text code composes (the turn message, the merged voice transcript, the
    Gate 2 verifier input). Inputs: a short kind name. Output: id. Side effects: none.
    Role: the explicit provenance label hide_for_langsmith reads; unique, so the thread never merges two messages.
    The random part has letters only, so an id can never look like (or be searched as) a PIN."""
    from uuid import uuid4

    return f"{OURS_ID_PREFIX}{kind}:{uuid4().hex.translate(_NO_DIGITS)}"


_NO_DIGITS = str.maketrans("0123456789", "ghijklmnop")


def _message_kind(obj: Mapping[Any, Any]) -> str | None:
    """What: "ours" for a message labelled with an ours_id, "system" for a model, tool or system message (or a model
    generation), "human" for any other human message, else None. Reads the plain dump ({"type": "ai", "id": ...}),
    the OpenAI shape ({"role": "assistant"}) and the LangChain serialized shape ({"lc": 1, "id": [..., "AIMessage"],
    "kwargs": {"id": ...}}). Use when: _trace. Inputs: mapping. Output: kind or None. Side effects: none.
    Role: helper."""
    names = [obj.get("type"), obj.get("role")]
    ident, kwargs = obj.get("id"), obj.get("kwargs")
    msg_id = kwargs.get("id") if isinstance(kwargs, Mapping) else ident
    if obj.get("lc") is not None and isinstance(ident, list) and ident:
        names.append(ident[-1])
    if not any(isinstance(n, str) and (n in _SYSTEM_KINDS or n in _HUMAN_KINDS) for n in names):
        return None
    if isinstance(msg_id, str) and msg_id.startswith(OURS_ID_PREFIX):
        return "ours"
    return "system" if any(isinstance(n, str) and n in _SYSTEM_KINDS for n in names) else "human"


def _trace(obj: Any) -> Any:
    """What: redact a trace payload by provenance, default deny (D-078): text our code labelled "ours" (a message
    with an ours_id, a value under an OURS_FIELDS key) and model, tool and system messages are system data
    (redact_system: frames scanned, numbers kept); every other string, including any unlabeled human message in or
    outside a frame, gets the full scan with the default context. PIN fields are replaced everywhere.
    Use when: hide_for_langsmith. Inputs: payload. Output: a redacted copy. Side effects: none; raises TypeError on an
    object it cannot inspect.
    Role: CS-E-02.F14: our records keep their numbers because they say so at the source, never because of the
    message role or the payload shape; a missing label only over-masks."""
    if isinstance(obj, BaseModel):
        return _trace(obj.model_dump(mode="python"))
    if is_dataclass(obj) and not isinstance(obj, type):
        return _trace(asdict(obj))
    if isinstance(obj, Mapping):
        kind = _message_kind(obj)
        if kind in ("ours", "system"):
            return redact_system(obj)
        if kind == "human":
            return redact(obj, _TRACE_CONTEXT)
        return {k: (PIN_TOKEN if isinstance(k, str) and k.lower() in PIN_FIELD_NAMES and v is not None
                    else redact_system(v) if k in OURS_FIELDS else _trace(v))
                for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return type(obj)(_trace(v) for v in obj)
    return redact(obj, _TRACE_CONTEXT)


def hide_for_langsmith(payload: Any) -> dict[str, Any]:
    """What: the LangSmith Client hide_inputs and hide_outputs function, by provenance, default deny (_trace): text
    labelled ours keeps its numbers, everything unlabeled gets the full scan. A payload it cannot walk (a middleware
    request holding a runtime object) is flattened to plain data first, which keeps its structure and labels, and
    redacted by the same rule; if even that fails, nothing is sent.
    Use when: langsmith.Client(hide_inputs=hide_for_langsmith, hide_outputs=hide_for_langsmith).
    Inputs: a run's inputs or outputs dict. Output: a redacted dict (never the raw payload).
    Side effects: none. Role: doc 07 s11.1 second net by provenance (D-071); ingress already redacted customer text
    with the session's context."""
    try:
        out = _trace(payload)
    except Exception:  # noqa: BLE001 - review round 2 N-I1: any failure falls back, never raises into tracing
        try:
            out = _trace(_plain(payload))  # CS-E-02.F14: the same provenance rule, never a scan of our records
        except Exception:  # noqa: BLE001 - never send what could not be inspected
            return {"redaction": "failed"}
    return out if isinstance(out, dict) else {"output": out}
