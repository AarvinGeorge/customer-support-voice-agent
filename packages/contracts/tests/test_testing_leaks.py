"""What: tests for pin_probe.leaks, the chance-proof PIN probe.
Use when: CI. Inputs: none. Output: pass or fail. Side effects: none.
Role: reproduces the CS-E-03.S13 flake (a UUID group equal to the PIN) and proves real leaks are still caught."""
import uuid
from pathlib import Path

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


@pytest.mark.parametrize("text", ["QS-10423-4821", "pin4821", "4821x", "pin-4821", "verify-4821-failed", "pin=4821-ok",
                                  "PIN4821", "x-4821-y", "ab4821cd", "a14821b"])
def test_pin_touching_letters_digits_or_hyphens_is_still_a_leak(text):
    """What: a PIN glued to letters, digits or hyphens is found. Use when: CI. Inputs: text. Output: assertion.
    Side effects: none. Role: the probe must not be weaker than the old substring check for real leaks."""
    assert leaks(text, "4821")


@pytest.mark.parametrize("text", [
    "0f8e4821-1111-4821-8000-4821abcdef01",
    "4821" * 0 + "a3f94821c0de" * 3,
    "sha256=" + "ab4821" * 11,
    "\\x" + "4821" * 16,
])
def test_uuid_and_long_hex_runs_are_not_leaks(text):
    """What: UUIDs and 32+ char hex digests that contain the digits are ignored. Use when: CI. Inputs: text.
    Output: assertion. Side effects: none. Role: the no-false-positive half (the original flake)."""
    assert "4821" in text
    assert not leaks(text, "4821")


def test_pin_probe_copies_are_identical():
    """What: the three pin_probe.py copies are byte-identical. Use when: CI. Inputs: none. Output: assertion.
    Side effects: reads files. Role: holds the keep-identical promise."""
    root = Path(__file__).resolve().parents[3]
    copies = [root / d / "tests" / "pin_probe.py" for d in ("packages/contracts", "services/core", "services/agent")]
    assert len({c.read_bytes() for c in copies}) == 1
