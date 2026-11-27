"""What: the doc 07 s7.3 normalization and strict yes and no grammars, one test per example in the contract.
Use when: pytest. Inputs: none. Output: pass/fail. Side effects: none.
Role: Done when of CS-E-03.S7.T8 ("every 07 s7.3 grammar example has a passing test")."""
from __future__ import annotations

import pytest

from agent.structured.grammar import AFFIRMATIVES, FILLERS, NO_PHRASES, is_strict_no, is_strict_yes, normalize


@pytest.mark.parametrize("raw,expected", [
    ("Yes, go ahead!", "yes go ahead"),            # s7.3 normalization example 1
    ("Don't", "don't"),                            # s7.3 normalization example 2
    ("Don’t", "don't"),                       # curly apostrophe replaced
    ("  YES   please  ", "yes please"),            # collapse spaces
])
def test_normalization_examples(raw: str, expected: str) -> None:
    assert normalize(raw) == expected


@pytest.mark.parametrize("text", ["yes", "yes, go ahead", "yeah that's right", "okay, confirm", "yes please, send it"])
def test_strict_yes_accepted_examples(text: str) -> None:
    assert is_strict_yes(text)


@pytest.mark.parametrize("text", ["ok", "sure", "yes but make it $50", "yes and change my email too", "yes 4821"])
def test_strict_yes_rejected_examples(text: str) -> None:
    assert not is_strict_yes(text)


@pytest.mark.parametrize("phrase", AFFIRMATIVES)
def test_every_affirmative_alone_is_a_strict_yes(phrase: str) -> None:
    assert is_strict_yes(phrase)


@pytest.mark.parametrize("phrase", FILLERS)
def test_fillers_are_never_enough_alone(phrase: str) -> None:
    assert not is_strict_yes(phrase) and not is_strict_no(phrase)


@pytest.mark.parametrize("word", ["no", "not", "don't", "dont", "wait", "stop", "cancel", "actually", "but", "change",
                                  "instead", "hold", "later", "maybe"])
def test_a_negation_or_hedge_rejects_a_yes(word: str) -> None:
    assert not is_strict_yes(f"yes {word}")


def test_curly_apostrophe_and_punctuation_still_match() -> None:
    assert is_strict_yes("That’s right!") and is_strict_yes("Yes, please. Thanks!")


@pytest.mark.parametrize("phrase", NO_PHRASES)
def test_every_no_phrase_alone_is_a_strict_no(phrase: str) -> None:
    assert is_strict_no(phrase)


@pytest.mark.parametrize("text", ["No thanks!", "nope, never mind", "Cancel it please", "Don’t do it."])
def test_strict_no_with_fillers(text: str) -> None:
    assert is_strict_no(text)


@pytest.mark.parametrize("text", ["no, refund the other one", "cancel my subscription", "no 0412", "yes", ""])
def test_strict_no_rejects_any_other_word(text: str) -> None:
    assert not is_strict_no(text)


def test_yes_and_no_are_disjoint_on_mixed_input() -> None:
    assert not is_strict_yes("yes no") and not is_strict_no("yes no")
