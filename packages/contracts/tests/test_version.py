"""What: tests that the contract version string is the one doc 07 s1.1 fixes.
Use when: CI. Inputs: none. Output: pass or fail. Side effects: none.
Role: guards the VERSION file and the Python constant against drift."""
from conftest import PKG_ROOT

import contracts


def test_version_file_matches_doc07():
    """What: VERSION file and constant equal "2026-11-26.r4".
    Use when: CI. Inputs: none. Output: assertion. Side effects: none. Role: version guard."""
    assert (PKG_ROOT / "VERSION").read_text().strip() == "2026-11-26.r4"
    assert contracts.VERSION == "2026-11-26.r4"
