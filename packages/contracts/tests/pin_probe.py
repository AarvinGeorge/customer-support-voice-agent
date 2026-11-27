"""What: test helper that finds a secret digit string (a PIN) in text without matching it by chance.
Use when: a test asserts a PIN is absent from rows, logs, responses or events. Plain `"4821" in text` is flaky:
random UUIDs and ids can contain those digits (a UUID group can be exactly 4821).
Inputs: none.
Output: leaks.
Side effects: none.
Role: PIN-absence probe. Three identical copies exist (services/core/tests, services/agent/tests, packages/contracts/tests)
because the services install contracts non-editable; keep them identical (packages/contracts/tests/test_testing_leaks.py guards the logic)."""
import re


def leaks(text: str, secret: str) -> bool:
    """What: True when `secret` appears as a standalone token, not inside a longer alphanumeric run and not as a
    hyphen-delimited group (UUID or id segment). Use when: PIN-absence assertions, always with a first half that
    proves the PIN is present via leaks(fixture, pin). Inputs: text, secret. Output: bool.
    Side effects: none. Role: chance-proof secret match."""
    return re.search(rf"(?<![0-9A-Za-z-]){re.escape(secret)}(?![0-9A-Za-z-])", text) is not None
