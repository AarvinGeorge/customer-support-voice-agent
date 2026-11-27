"""What: test helper that finds a secret digit string (a PIN) in text without matching it by chance.
Use when: a test asserts a PIN is absent from rows, logs, responses or events. Plain `"4821" in text` is flaky:
random UUIDs and hex digests can contain those digits (a UUID group can be exactly 4821).
Inputs: none.
Output: leaks.
Side effects: none.
Role: PIN-absence probe. Three identical copies exist (services/core/tests, services/agent/tests, packages/contracts/tests)
because the services install contracts non-editable; packages/contracts/tests/test_testing_leaks.py checks the logic and
that the copies stay byte-identical."""
import re

_NOISE = re.compile(r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}|[0-9a-fA-F]{32,}")


def leaks(text: str, secret: str) -> bool:
    """What: True when `secret` is a substring of `text` after UUIDs and 32+ character hex runs (ids, digests) are
    blanked out. Letter, digit and hyphen neighbours still count as leaks (QS-10423-4821, pin4821, 4821x).
    Use when: PIN-absence assertions, paired with a first half that proves the PIN is present in the fixture.
    Inputs: text, secret. Output: bool. Side effects: none. Role: chance-proof secret match; a standalone number
    such as an amount of 4821 cents would still match, so do not put one in a fixture."""
    return secret in _NOISE.sub(" ", text)
