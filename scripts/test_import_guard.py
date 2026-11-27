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


def _bad(tmp_path: Path, body: str, name: str = "t.py") -> list[str]:
    """What: write one tool module with `body` and return the guard findings. Use when: one test per forbidden path.
    Inputs: tmp_path, source, file name. Output: findings list. Side effects: creates files. Role: test helper."""
    _tool(tmp_path, name, body)
    return guard.find_violations(tmp_path)


def test_ingress_confirm_module_fails(tmp_path: Path) -> None:
    """What: doc 07 s1.4 commit code `agent.ingress.confirm` is forbidden to tools (from-import of a name).
    Use when: pytest. Inputs: tmp_path. Output: assert. Side effects: none. Role: ingress path, absolute."""
    assert len(_bad(tmp_path, "from agent.ingress.confirm import commit_action\n")) == 1


def test_ingress_package_then_confirm_name_fails(tmp_path: Path) -> None:
    """What: `from agent.ingress import confirm` resolves to the same module and is caught.
    Use when: pytest. Inputs: tmp_path. Output: assert. Side effects: none. Role: ingress path, name form."""
    assert len(_bad(tmp_path, "from agent.ingress import confirm\n")) == 1


def test_relative_ingress_import_fails(tmp_path: Path) -> None:
    """What: `from ..ingress import confirm` and `from ..ingress.confirm import x` resolve against the file's package.
    Use when: pytest. Inputs: tmp_path. Output: assert. Side effects: none. Role: ingress path, relative."""
    assert len(_bad(tmp_path, "from ..ingress import confirm\n")) == 1
    assert len(_bad(tmp_path, "from ..ingress.confirm import x\n", "u.py")) == 1 + 1  # t.py and u.py both now violate


def test_services_prefixed_path_fails(tmp_path: Path) -> None:
    """What: the doc form `services.agent.ingress.confirm` is caught. Use when: pytest. Inputs: tmp_path.
    Output: assert. Side effects: none. Role: ingress path, prefixed."""
    assert len(_bad(tmp_path, "import services.agent.ingress.confirm\n")) == 1


def test_ops_router_fails(tmp_path: Path) -> None:
    """What: `from agent.ops import router` and `import agent.ops.routes` are caught. Use when: pytest.
    Inputs: tmp_path. Output: assert. Side effects: none. Role: ops path."""
    assert len(_bad(tmp_path, "from agent.ops import router\nimport agent.ops.routes\n")) == 2


def test_commit_or_ops_token_name_fails(tmp_path: Path) -> None:
    """What: the literal CORE_TOKEN_COMMIT or CORE_TOKEN_OPS anywhere in a tool module is a violation.
    Use when: pytest. Inputs: tmp_path. Output: assert. Side effects: none. Role: token path."""
    found = _bad(tmp_path, "import os\nx = os.environ['CORE_TOKEN_COMMIT']\ny = 'CORE_TOKEN_OPS'\n")
    assert len(found) == 2


def test_agent_token_name_is_allowed(tmp_path: Path) -> None:
    """What: CORE_TOKEN_AGENT is the tools token and is fine. Use when: pytest. Inputs: tmp_path.
    Output: assert. Side effects: none. Role: no false positive on the allowed token."""
    assert _bad(tmp_path, "import os\nx = os.environ['CORE_TOKEN_AGENT']\n") == []


def test_dynamic_import_of_forbidden_module_fails(tmp_path: Path) -> None:
    """What: importlib.import_module and __import__ with a forbidden literal, or with a non-literal, are violations.
    Use when: pytest. Inputs: tmp_path. Output: assert. Side effects: none. Role: dynamic import path."""
    body = "import importlib\nimportlib.import_module('agent.clients.core_commit')\n__import__('agent.ops')\nimportlib.import_module(name)\n"
    assert len(_bad(tmp_path, body)) == 3


def test_flat_tools_module_is_scanned(tmp_path: Path) -> None:
    """What: a single file `agent/tools.py` (no tools directory) is in scope. Use when: pytest. Inputs: tmp_path.
    Output: assert. Side effects: none. Role: scope path."""
    d = tmp_path / "services" / "agent" / "src" / "agent"
    d.mkdir(parents=True)
    (d / "tools.py").write_text("from agent.ops import router\n")
    assert len(guard.find_violations(tmp_path)) == 1


def test_devops_client_is_not_a_false_positive(tmp_path: Path) -> None:
    """What: `devops_client` contains `ops_client` as a substring but is a different name.
    Use when: pytest. Inputs: tmp_path. Output: assert. Side effects: none. Role: word-boundary regression."""
    assert _bad(tmp_path, "from agent.devops_client import X\nimport operations\n") == []


def test_camel_case_client_class_fails(tmp_path: Path) -> None:
    """What: `from agent.clients import OpsClient` is caught by the imported class name. Use when: pytest.
    Inputs: tmp_path. Output: assert. Side effects: none. Role: class-name path."""
    assert len(_bad(tmp_path, "from agent.clients import OpsClient\n")) == 1


def test_main_exit_codes(tmp_path: Path) -> None:
    """What: main returns 0 for a clean tree and 1 for a violation. Use when: pytest. Inputs: tmp_path.
    Output: assert. Side effects: prints. Role: the CI exit status."""
    assert guard.main([str(tmp_path)]) == 0
    _tool(tmp_path, "bad.py", "from agent.clients.core_commit import CoreCommitClient\n")
    assert guard.main([str(tmp_path)]) == 1
