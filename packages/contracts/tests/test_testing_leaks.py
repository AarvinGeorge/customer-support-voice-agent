"""What: tests for pin_probe.leaks, the chance-proof PIN probe.
Use when: CI. Inputs: none. Output: pass or fail. Side effects: none.
Role: reproduces the CS-E-03.S13 flake (a UUID group equal to the PIN) and proves real leaks are still caught."""
import uuid

import pytest
from pin_probe import leaks

UNLUCKY = uuid.UUID("00000000-0000-4821-8000-000000000000")


def test_a_uuid_containing_the_pin_digits_is_not_a_leak():
    """What: the old plain-substring check trips on a UUID, leaks does not. Use when: CI. Inputs: none.
    Output: assertions. Side effects: none. Role: deterministic repro of the flake."""
    text = f'{{"id": "{UNLUCKY}", "session_id": "{UNLUCKY}"}}'
    assert "4821" in text  # the old assertion would fail here
    assert not leaks(text, "4821")


@pytest.mark.parametrize("text", ['"pin": "4821"', "QS-10423 4821", "my PIN is 4821.", "pin=4821", "(4821)", "4821"])
def test_real_leaks_are_caught(text):
    """What: PIN in JSON, account-id form, prose, query and bare forms is found. Use when: CI.
    Inputs: text. Output: assertion. Side effects: none. Role: proves the probe can be non-zero."""
    assert leaks(text, "4821")


@pytest.mark.parametrize("text", ["a14821b", "104821", "48210", "x-4821-y", "ab4821cd"])
def test_embedded_digit_runs_are_not_leaks(text):
    """What: digits inside longer runs or hyphen groups are ignored. Use when: CI. Inputs: text.
    Output: assertion. Side effects: none. Role: the no-false-positive half."""
    assert not leaks(text, "4821")
