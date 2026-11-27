"""What: query normalizer: text hygiene plus typed and spoken error-code recognition against a closed vocabulary.
Use when: search prepares the text that is embedded and fed to the keyword leg (draft A s2.5, decision A-8).
Inputs: raw query, the set of codes found in the corpus (e.g. {"E-SSO-401"}). Output: NormalizedQuery(text, codes,
unknown_codes). Side effects: none. Role: deterministic and unit-tested; only a vocabulary hit is rewritten, so ordinary
speech ("I paid four hundred one dollars") never becomes a code. A hyphenated code-shaped string that is not in the
vocabulary goes to unknown_codes and the text is left alone."""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field

MAX_CHARS = 2000
_DIGIT_WORDS = {"zero": "0", "one": "1", "two": "2", "three": "3", "four": "4", "five": "5", "six": "6",
                "seven": "7", "eight": "8", "nine": "9"}
_TYPED = re.compile(r"\bE[\s-]?([A-Z]{2,6})[\s-]?(\d{3})\b", re.I)
_HYPHENATED = re.compile(r"E-[A-Z]{2,6}-\d{3}", re.I)
_TOKEN = re.compile(r"(\W*)(\w+)(\W*)")
_MAX_WINDOW = 10


@dataclass(frozen=True)
class NormalizedQuery:
    """What: result of normalize_query. Use when: search builds its legs and the response.
    Inputs: text, codes found in the vocabulary (order of appearance, no repeats), unknown code-shaped strings.
    Output: frozen record. Side effects: none. Role: doc 07 s4.1 query block."""

    text: str
    codes: list[str] = field(default_factory=list)
    unknown_codes: list[str] = field(default_factory=list)


def _clean(raw: str) -> str:
    """What: NFKC, drop control and zero-width characters, collapse whitespace, cap length.
    Use when: first step. Inputs: raw text. Output: clean text of at most 2000 characters. Side effects: none.
    Role: draft A s2.5 step 1."""
    text = unicodedata.normalize("NFKC", raw)
    text = "".join(" " if ch.isspace() else ch for ch in text if unicodedata.category(ch) not in ("Cc", "Cf") or ch.isspace())
    return re.sub(r"\s+", " ", text).strip()[:MAX_CHARS].strip()


def _typed(text: str, vocab: set[str], codes: list[str], unknown: list[str]) -> str:
    """What: rewrite typed forms (e sso 401, ESSO401, E-SSO 401) that hit the vocabulary to the canonical code.
    Use when: after cleaning. Inputs: text, vocabulary, lists to append to. Output: rewritten text.
    Side effects: appends to codes and unknown_codes. Role: step 2 and 4; a miss is unknown only when written E-AREA-NNN."""
    def sub(m: re.Match) -> str:
        """What: handle one match. Use when: re.sub. Inputs: match. Output: replacement text.
        Side effects: appends to the outer lists. Role: vocabulary check."""
        canon = f"E-{m.group(1)}-{m.group(2)}".upper()
        if canon in vocab:
            if canon not in codes:
                codes.append(canon)
            return canon
        if _HYPHENATED.fullmatch(m.group(0)) and canon not in unknown:
            unknown.append(canon)
        return m.group(0)

    return _TYPED.sub(sub, text)


def _items(text: str) -> list[dict]:
    """What: split text into tokens classified for spoken-code matching.
    Use when: the spoken pass. Inputs: text. Output: list of {pre, core, post, comp} where comp is the letters or
    digits the token contributes (None when it cannot be part of a code). Side effects: none.
    Role: handles number words, 'oh' or 'o' as zero only between digits, and 'four hundred one' style numbers."""
    toks = [m.groups() for m in _TOKEN.finditer(text)]
    items: list[dict] = []
    for pre, core, post in toks:
        low = core.lower()
        if "-" in core or "_" in core:
            comp = None
        elif core.isdigit():
            comp = core
        elif low in _DIGIT_WORDS:
            comp = _DIGIT_WORDS[low]
        elif core.isalpha():
            comp = core.upper()
        else:
            comp = None
        items.append({"pre": pre, "core": core, "post": post, "comp": comp, "low": low,
                      "digit": comp is not None and comp.isdigit()})
    merged: list[dict] = []
    i = 0
    while i < len(items):
        it = items[i]
        nxt = items[i + 1] if i + 1 < len(items) else None
        if it["digit"] and len(it["comp"]) == 1 and nxt and nxt["low"] == "hundred":
            j, tail = i + 2, "00"
            skip = items[j] if j < len(items) else None
            if skip and skip["low"] == "and":
                skip = items[j + 1] if j + 1 < len(items) else None
                j += 1
            if skip and skip["digit"] and len(skip["comp"]) == 1 and skip["low"] in _DIGIT_WORDS:
                tail, j = "0" + skip["comp"], j + 1
            elif j > i + 2:
                j = i + 2
            merged.append({"pre": it["pre"], "core": " ".join(x["core"] for x in items[i:j]), "post": items[j - 1]["post"],
                           "comp": it["comp"] + tail, "low": "", "digit": True, "span": j - i})
            i = j
            continue
        it["span"] = 1
        merged.append(it)
        i += 1
    for k, it in enumerate(merged):  # 'oh' and 'o' are zero only between two digit tokens
        if it["low"] in ("oh", "o") and 0 < k < len(merged) - 1 and merged[k - 1]["digit"] and merged[k + 1]["digit"]:
            it["comp"], it["digit"] = "0", True
    return merged


def _spoken(text: str, vocab: set[str], codes: list[str]) -> str:
    """What: find spoken or spaced codes ("E S S O four oh one") and replace them by the canonical code.
    Use when: after the typed pass. Inputs: text, vocabulary, codes list to append to. Output: rewritten text.
    Side effects: appends to codes. Role: step 3; the longest window whose compact form equals a vocabulary code wins."""
    compact = {c.replace("-", ""): c for c in vocab}
    if not compact:
        return text
    items = _items(text)
    out: list[str] = []
    i = 0
    while i < len(items):
        hit = None
        for j in range(min(len(items), i + _MAX_WINDOW), i, -1):
            window = items[i:j]
            if any(w["comp"] is None for w in window):
                continue
            canon = compact.get("".join(w["comp"] for w in window))
            if canon:
                hit = (j, canon)
                break
        if hit:
            j, canon = hit
            if canon not in codes:
                codes.append(canon)
            out.append(items[i]["pre"] + canon + items[j - 1]["post"])
            i = j
        else:
            out.append(items[i]["pre"] + items[i]["core"] + items[i]["post"])
            i += 1
    return "".join(out)


def normalize_query(raw: str, vocabulary: set[str]) -> NormalizedQuery:
    """What: normalize a user query. Use when: search starts. Inputs: raw text, set of known codes.
    Output: NormalizedQuery. Side effects: none. Role: the text embedded and keyword-searched, plus the code lists."""
    codes: list[str] = []
    unknown: list[str] = []
    text = _typed(_clean(raw), vocabulary, codes, unknown)
    text = _spoken(text, vocabulary, codes)
    return NormalizedQuery(text=text, codes=codes, unknown_codes=unknown)
