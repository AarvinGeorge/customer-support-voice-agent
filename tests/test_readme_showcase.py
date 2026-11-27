"""What: tests for the README showcase-slot check.
Use when: pytest runs in CI. Inputs: README.md and scripts/check_readme_showcase.py.
Output: pass or fail. Side effects: none. Role: proves every showcase id has a slot and the check can fail."""
import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("check_readme_showcase", ROOT / "scripts" / "check_readme_showcase.py")
chk = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(chk)


def test_every_showcase_id_has_a_slot():
    """What: README has a slot for all 31 ids. Use when: always. Inputs: none. Output: assert. Side effects: none. Role: the Done-when check."""
    assert len(chk.SHOWCASE_IDS) == 31
    assert chk.missing_slots((ROOT / "README.md").read_text(), chk.SHOWCASE_IDS) == []


def test_check_can_fail():
    """What: a README without slot 2.3 is reported. Use when: always. Inputs: none. Output: assert. Side effects: none. Role: proves the zero is possible to be non-zero."""
    text = (ROOT / "README.md").read_text().replace("#### 2.3 ", "#### x ")
    assert chk.missing_slots(text, chk.SHOWCASE_IDS) == ["2.3"]


def test_builtin_ids_match_doc_04_when_present():
    """What: built-in ids equal the ids in planning-docs/04 when that file is reachable. Use when: run inside the project checkout.
    Inputs: none. Output: assert or skip. Side effects: none. Role: keeps the built-in list honest."""
    import pytest
    doc = next((p / "planning-docs" / "04_showcase_list_2026-11-26.md" for p in ROOT.parents
                if (p / "planning-docs" / "04_showcase_list_2026-11-26.md").exists()), None)
    if doc is None:
        pytest.skip("planning-docs not reachable")
    assert chk.ids_in_list(doc.read_text()) == chk.SHOWCASE_IDS
