"""What: the doc 07 s7.3 normalization and the strict yes and strict no grammars.
Use when: ingress step 13 checks typed chat text (and, later, a final voice transcript) against the current
pending action.
Inputs: a raw utterance.
Output: normalize, is_strict_yes, is_strict_no, AFFIRMATIVES, FILLERS, NO_PHRASES, NEGATIONS.
Side effects: none.
Role: the only definition of "explicit yes" in the agent (core enforces "later turn", the agent "explicit yes").
The lists are copied verbatim from doc 07 s7.3; a test runs every example the contract gives.
"""
from __future__ import annotations

from functools import lru_cache

AFFIRMATIVES: tuple[str, ...] = (
    "yes", "yeah", "yep", "yup", "yes please", "go ahead", "go for it", "do it", "please do", "confirm",
    "confirmed", "i confirm", "that's right", "thats right", "correct", "sounds good", "send it", "send it over",
    "approve", "ok do it", "okay do it")
FILLERS: tuple[str, ...] = ("ok", "okay", "sure", "please", "thanks", "thank you", "great", "perfect", "alright",
                            "all right")
NEGATIONS: frozenset[str] = frozenset({"no", "not", "don't", "dont", "wait", "stop", "cancel", "actually", "but",
                                       "change", "instead", "hold", "later", "maybe"})
NO_PHRASES: tuple[str, ...] = ("no", "nope", "cancel", "cancel it", "don't", "dont", "don't do it", "never mind",
                               "no thanks", "no thank you", "stop")


def normalize(text: str) -> str:
    """What: lowercase; curly apostrophes to "'"; drop every character that is not a letter, digit, space or
    apostrophe; collapse spaces. Use when: before either grammar (applied identically to input and lists).
    Inputs: raw text. Output: normalized text ("Yes, go ahead!" -> "yes go ahead"). Side effects: none.
    Role: doc 07 s7.3 Normalization."""
    s = text.lower().replace("’", "'").replace("‘", "'")
    s = "".join(ch if (ch.isalpha() or ch.isdigit() or ch == "'" or ch.isspace()) else "" for ch in s)
    return " ".join(s.split())


def _covers(tokens: tuple[str, ...], phrases: tuple[str, ...], needed: frozenset[str]) -> bool:
    """What: true when the tokens split fully into phrases from `phrases`, with at least one phrase from `needed`.
    Use when: both grammars. Inputs: tokens, allowed phrases, phrases that must appear. Output: bool.
    Side effects: none. Role: exact phrase segmentation (dynamic programming over token positions)."""
    split = [tuple(p.split()) for p in phrases]

    @lru_cache(maxsize=None)
    def walk(i: int, seen: bool) -> bool:
        """What: can tokens[i:] be covered, given whether a needed phrase was seen. Use when: _covers.
        Inputs: position, flag. Output: bool. Side effects: none. Role: recursion."""
        if i == len(tokens):
            return seen
        for words, phrase in zip(split, phrases):
            if tokens[i:i + len(words)] == words and walk(i + len(words), seen or phrase in needed):
                return True
        return False

    return bool(tokens) and walk(0, False)


def is_strict_yes(text: str) -> bool:
    """What: the strict yes grammar: the whole normalized utterance is affirmatives and fillers, at least one
    affirmative, no negation or hedge token, no digit, no other word.
    Use when: ingress yes signal 2 (typed chat) and 3 (final voice transcript). Inputs: raw text. Output: bool.
    Side effects: none. Role: doc 07 s7.3 ("yes 4821" and "yes but make it $50" are rejected)."""
    norm = normalize(text)
    tokens = tuple(norm.split())
    if any(ch.isdigit() for ch in norm) or any(t in NEGATIONS for t in tokens):
        return False
    return _covers(tokens, AFFIRMATIVES + FILLERS, frozenset(AFFIRMATIVES))


def is_strict_no(text: str) -> bool:
    """What: the strict no grammar: the whole normalized utterance is no-phrases and fillers, at least one
    no-phrase, no other word. Use when: ingress, against the current pending. Inputs: raw text. Output: bool.
    Side effects: none. Role: doc 07 s7.3 Strict no grammar."""
    tokens = tuple(normalize(text).split())
    return _covers(tokens, NO_PHRASES + FILLERS, frozenset(NO_PHRASES))
