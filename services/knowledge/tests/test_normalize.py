"""What: table tests for normalize_query (draft A s2.5): typed codes, spoken codes, unknown codes, text hygiene.
Use when: CI. Inputs: a vocabulary of known codes. Output: pass or fail. Side effects: none.
Role: CS-E-02.S2.T2 normalize_query; spoken code handling against a closed vocabulary (decision A-8)."""
import pytest

from knowledge.normalize import normalize_query

VOCAB = {"E-SSO-401", "E-SSO-403", "E-SYNC-204", "E-LOGIN-429"}


@pytest.mark.parametrize("raw,code", [
    ("E S S O four oh one", "E-SSO-401"),
    ("ESSO 401", "E-SSO-401"),
    ("e-sso-401", "E-SSO-401"),
    ("E-SSO 401", "E-SSO-401"),
    ("e sso 401", "E-SSO-401"),
    ("error E SYNC two oh four", "E-SYNC-204"),
    ("e s s o four zero three", "E-SSO-403"),
    ("E SSO four hundred three", "E-SSO-403"),
    ("my login says e login four two nine", "E-LOGIN-429"),
])
def test_codes_are_recognized_in_all_forms(raw, code):
    """What: each typed or spoken form becomes the canonical code, listed once, and appears in the text.
    Use when: CI. Inputs: parametrized phrase. Output: assertion. Side effects: none. Role: draft A s2.5 tests."""
    n = normalize_query(raw, VOCAB)
    assert n.codes == [code] and n.unknown_codes == []
    assert code in n.text


def test_spoken_code_replaces_only_the_code_words():
    """What: words around the code are kept. Use when: CI. Inputs: a sentence. Output: assertion.
    Side effects: none. Role: the normalized text is what gets embedded."""
    n = normalize_query("Why does E S S O four oh one happen at login?", VOCAB)
    assert n.text == "Why does E-SSO-401 happen at login?"


@pytest.mark.parametrize("raw", [
    "I paid four hundred one dollars",
    "I paid 401 dollars",
    "send an email 123 times",
    "the sso setup page",
    "four oh one",
    "what is the weather",
])
def test_ordinary_speech_is_not_turned_into_a_code(raw):
    """What: number words, digits and 'email 123' without a vocabulary hit stay words. Use when: CI.
    Inputs: phrases. Output: assertion. Side effects: none. Role: 'avoid inventing codes from ordinary speech'."""
    n = normalize_query(raw, VOCAB)
    assert n.codes == [] and n.unknown_codes == [] and n.text == raw


def test_unknown_code_is_reported_not_rewritten():
    """What: E-SSO-999 is code-shaped but not in the vocabulary: listed as unknown, text kept. Use when: CI.
    Inputs: phrase. Output: assertion. Side effects: none. Role: draft A s2.5 step 4."""
    n = normalize_query("what is E-SSO-999", VOCAB)
    assert n.codes == [] and n.unknown_codes == ["E-SSO-999"] and n.text == "what is E-SSO-999"


def test_multiple_codes_keep_order_and_dedupe():
    """What: two codes, one repeated, come out in order of appearance once each. Use when: CI.
    Inputs: phrase. Output: assertion. Side effects: none. Role: codes list contract."""
    n = normalize_query("E-SYNC-204 then e sso 401 and again E-SYNC-204", VOCAB)
    assert n.codes == ["E-SYNC-204", "E-SSO-401"]


def test_empty_vocabulary_still_flags_hyphenated_typed_codes():
    """What: with no vocabulary a hyphenated code is unknown, a spoken one is ignored. Use when: CI.
    Inputs: phrases. Output: assertion. Side effects: none. Role: empty corpus behaviour."""
    assert normalize_query("E-SSO-401", set()).unknown_codes == ["E-SSO-401"]
    assert normalize_query("E S S O four oh one", set()).codes == []


def test_text_hygiene():
    """What: NFKC, zero-width removal, whitespace collapse and the 2,000 character cap. Use when: CI.
    Inputs: messy text. Output: assertion. Side effects: none. Role: draft A s2.5 step 1."""
    n = normalize_query("ＥＳＳＯ ４０１​  hello\n\tworld\x07", VOCAB)
    assert n.codes == ["E-SSO-401"] and n.text.endswith("hello world")
    assert len(normalize_query("a " * 3000, VOCAB).text) <= 2000
