"""What: tests for scripts/check_import_guard.py (agent tool modules must not import the commit or ops client).
Use when: CI runs `pytest scripts`, or after editing the guard.
Inputs: temporary source trees built per test. Output: pass or fail.
Side effects: writes only inside pytest tmp dirs.
Role: proves the guard can fail (a zero must be shown possible to be non-zero) and passes clean trees."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import check_import_guard as guard  # noqa: E402


def _tool(root: Path, name: str, body: str) -> None:
    """What: write one module under services/agent/src/agent/tools. Use when: arranging a test tree.
    Inputs: root, file name, source. Output: none. Side effects: creates files. Role: test helper."""
    d = root / "services" / "agent" / "src" / "agent" / "tools"
    (d / name).parent.mkdir(parents=True, exist_ok=True)
    (d / name).write_text(body)


def test_clean_tree_passes(tmp_path: Path) -> None:
    """What: a tool importing only allowed things passes. Use when: pytest. Inputs: tmp_path.
    Output: assert. Side effects: none. Role: no false positives."""
    _tool(tmp_path, "lookup.py", "from agent.clients.core_agent import CoreAgentClient\nimport json\n")
    assert guard.find_violations(tmp_path) == []


def test_no_tools_directory_passes(tmp_path: Path) -> None:
    """What: a tree with no tools directory passes (the guard is trivial before the agent exists).
    Use when: pytest. Inputs: tmp_path. Output: assert. Side effects: none. Role: early-lane behaviour."""
    assert guard.find_violations(tmp_path) == []


def test_from_import_of_commit_client_fails(tmp_path: Path) -> None:
    """What: `from ... import CoreCommitClient` in a tool is a violation. Use when: pytest.
    Inputs: tmp_path. Output: assert. Side effects: none. Role: the red case the plan row requires."""
    _tool(tmp_path, "refund.py", "from agent.clients.core_commit import CoreCommitClient\n")
    found = guard.find_violations(tmp_path)
    assert len(found) == 1 and found[0].endswith("refund.py:1")


def test_plain_import_of_ops_client_fails(tmp_path: Path) -> None:
    """What: `import agent.clients.ops_client` in a nested tool package is a violation.
    Use when: pytest. Inputs: tmp_path. Output: assert. Side effects: none. Role: covers import form and subdirs."""
    _tool(tmp_path, "sub/x.py", "import agent.clients.ops_client as c\n")
    assert len(guard.find_violations(tmp_path)) == 1


def test_name_imported_from_package_fails(tmp_path: Path) -> None:
    """What: `from agent.clients import CommitClient` is caught by the imported name, not only the module.
    Use when: pytest. Inputs: tmp_path. Output: assert. Side effects: none. Role: alias form."""
    _tool(tmp_path, "t.py", "from agent.clients import CommitClient\n")
    assert len(guard.find_violations(tmp_path)) == 1


def test_commit_client_outside_tools_is_allowed(tmp_path: Path) -> None:
    """What: the commit flow itself (outside tools) may import the client. Use when: pytest.
    Inputs: tmp_path. Output: assert. Side effects: none. Role: scope of the rule is tools only."""
    d = tmp_path / "services" / "agent" / "src" / "agent" / "graph"
    d.mkdir(parents=True)
    (d / "commit.py").write_text("from agent.clients.core_commit import CoreCommitClient\n")
    assert guard.find_violations(tmp_path) == []


def test_main_exit_codes(tmp_path: Path) -> None:
    """What: main returns 0 for a clean tree and 1 for a violation. Use when: pytest. Inputs: tmp_path.
    Output: assert. Side effects: prints. Role: the CI exit status."""
    assert guard.main([str(tmp_path)]) == 0
    _tool(tmp_path, "bad.py", "from agent.clients.core_commit import CoreCommitClient\n")
    assert guard.main([str(tmp_path)]) == 1
