"""What: ingest sanitizer: strip comments, raw HTML and invisible characters; escape agent wrapper tags; flag injection phrases.
Use when: every article block is cleaned before chunking (draft A s2.9, doc 07 s5.3).
Inputs: text. Output: cleaned text; is_suspect(text) gives the injection flag.
Side effects: none. Role: article text is untrusted data. Order matters: invisible characters go first (so '<d<ZWSP>oc>'
cannot dodge), then wrapper tags are escaped (so '</doc>' is neutralized, not deleted), then remaining raw HTML tags go."""
import re

from contracts.trust import escape_untrusted

_INVISIBLE = re.compile("[​-‏‪-‮⁠-⁤⁦-⁩﻿\x00]")
COMMENT = re.compile(r"<!--.*?-->", re.S)
# a tag name must be followed by whitespace, "/" or ">" so autolinks such as <https://x> and "1 < 2" survive
_TAG = re.compile(r"</?[A-Za-z][A-Za-z0-9-]*(?:\s[^<>]*)?/?>")
_SUSPECT = re.compile(
    r"ignore\s+(?:all\s+|any\s+|the\s+)?(?:previous|prior|above)\s+(?:instructions|prompts?|rules)"
    r"|disregard\s+(?:all\s+|any\s+|the\s+)?(?:previous|prior|above)?\s*(?:instructions|rules)"
    r"|you\s+are\s+now\b|system\s+prompt|refund\s+the\s+full\s+amount",
    re.I)


def sanitize(text: str) -> str:
    """What: clean one piece of article text. Use when: after rendering, before chunking.
    Inputs: text. Output: text without comments, invisible characters and raw HTML tags, with wrapper
    tags escaped ("<" to "&lt;"). Side effects: none. Role: draft A s2.9 sanitization."""
    text = COMMENT.sub("", text)
    text = _INVISIBLE.sub("", text)
    text = escape_untrusted(text)
    return _TAG.sub("", text)


def is_suspect(text: str) -> bool:
    """What: does the text contain an instruction-like phrase. Use when: setting injection_suspect on a chunk.
    Inputs: text. Output: bool. Side effects: none. Role: flag, never drop (the poisoned-doc eval tests the
    real defense)."""
    return _SUSPECT.search(text) is not None
