"""What: tests for scripts/check_descriptors.py (the D-015 descriptor gate: six labelled lines on every module, class and function).
Use when: CI runs `pytest scripts`, or after editing the checker.
Inputs: small Python and TypeScript sources and temporary git repos built per test.
Output: pass or fail.
Side effects: writes and runs git only inside pytest tmp dirs.
Role: proves the gate can fail (a zero must be shown possible to be non-zero) and passes documented code."""
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import check_descriptors as cd  # noqa: E402

GOOD = (
    "What: does a thing. Use when: tests. Inputs: none. Output: none. "
    "Side effects: none. Role: fixture."
)
GOOD_ML = "What: x.\nUse when: y.\nInputs: none.\nOutput: none.\nSide effects: none.\nRole: z."


def _ts_block(indent: str = "") -> str:
    """What: a full JSDoc descriptor block. Use when: building TS fixtures. Inputs: indent.
    Output: the block text. Side effects: none. Role: test helper."""
    lines = ["/**", " * What: x.", " * Use when: y.", " * Inputs: none.", " * Output: none.", " * Side effects: none.", " * Role: z.", " */"]
    return "\n".join(indent + line for line in lines) + "\n"


def test_python_fully_documented_passes() -> None:
    """What: a documented module, class, function and method pass. Use when: pytest. Inputs: none.
    Output: assert. Side effects: none. Role: no false positives."""
    src = f'"""{GOOD_ML}"""\n\n\nclass A:\n    """{GOOD}"""\n\n    def m(self) -> None:\n        """{GOOD}"""\n\n\ndef f() -> None:\n    """{GOOD}"""\n'
    assert cd.check_python(src, "a.py") == []


def test_python_undocumented_function_is_a_gap() -> None:
    """What: a function with no docstring is reported with its line. Use when: pytest. Inputs: none.
    Output: assert. Side effects: none. Role: the gate can go red."""
    src = f'"""{GOOD}"""\n\n\ndef f() -> None:\n    return None\n'
    gaps = cd.check_python(src, "a.py")
    assert [(g.line, g.name) for g in gaps] == [(4, "f")]


def test_python_missing_module_class_and_label() -> None:
    """What: no module docstring, no class docstring, and a docstring missing a label are each reported.
    Use when: pytest. Inputs: none. Output: assert. Side effects: none. Role: every kind and every label is checked."""
    src = 'class A:\n    pass\n\n\ndef f() -> None:\n    """What: x. Use when: y. Inputs: none. Output: none. Role: z."""\n'
    gaps = cd.check_python(src, "a.py")
    kinds = {(g.name, g.problem) for g in gaps}
    assert ("<module>", "missing") in kinds
    assert ("A", "missing") in kinds
    assert ("f", "labels: Side effects:") in kinds


def test_python_async_and_nested_functions_are_checked() -> None:
    """What: an async def and a nested def without docstrings are reported. Use when: pytest. Inputs: none.
    Output: assert. Side effects: none. Role: no function kind escapes."""
    src = f'"""{GOOD}"""\n\n\nasync def f() -> None:\n    """{GOOD}"""\n\n    def inner() -> None:\n        return None\n\n    inner()\n\n\nasync def g() -> None:\n    return None\n'
    names = sorted(g.name for g in cd.check_python(src, "a.py"))
    assert names == ["g", "inner"]


def test_ts_fully_documented_passes() -> None:
    """What: a documented TS module, function, const, type pass. Use when: pytest. Inputs: none.
    Output: assert. Side effects: none. Role: no false positives."""
    src = (
        _ts_block()
        + 'import { a } from "a";\n\n'
        + _ts_block()
        + "export function f(): void {}\n\n"
        + _ts_block()
        + "export const g = (): void => {};\n\n"
        + _ts_block()
        + "export type T = string;\n"
    )
    assert cd.check_ts(src, "a.ts") == []


def test_ts_undocumented_function_is_a_gap() -> None:
    """What: an exported function with no JSDoc is reported. Use when: pytest. Inputs: none.
    Output: assert. Side effects: none. Role: the gate can go red for TS."""
    src = _ts_block() + 'import { a } from "a";\n\nexport function f(): void {}\n'
    gaps = cd.check_ts(src, "a.ts")
    assert [(g.line, g.name) for g in gaps] == [(11, "f")]


def test_ts_non_descriptor_comment_and_missing_label_are_gaps() -> None:
    """What: a plain JSDoc with no labels, and one missing a label, are reported. Use when: pytest. Inputs: none.
    Output: assert. Side effects: none. Role: a comment is not a descriptor."""
    src = (
        _ts_block()
        + "/** Just a note. */\nexport const a = 1;\n\n"
        + "/**\n * What: x.\n * Use when: y.\n * Inputs: none.\n * Output: none.\n * Role: z.\n */\nexport const b = 2;\n"
    )
    got = {(g.name, g.problem) for g in cd.check_ts(src, "a.ts")}
    assert got == {("a", "missing"), ("b", "labels: Side effects:")}


def test_ts_missing_module_block() -> None:
    """What: a file whose only block documents its first declaration has no module descriptor. Use when: pytest.
    Inputs: none. Output: assert. Side effects: none. Role: the module needs its own block."""
    src = 'import { a } from "a";\n\n' + _ts_block() + "export function f(): void {}\n"
    got = {(g.name, g.problem) for g in cd.check_ts(src, "a.ts")}
    assert got == {("<module>", "missing")}


def test_ts_ignores_imports_reexports_nested_and_use_client() -> None:
    """What: imports, `export {..}`, `export * from`, a "use client" directive, and indented code are not declarations.
    Use when: pytest. Inputs: none. Output: assert. Side effects: none. Role: no false positives."""
    src = (
        '"use client";\n'
        + _ts_block()
        + 'import { a } from "a";\nexport { a };\nexport * from "b";\n\n'
        + _ts_block()
        + "export function f(): void {\n  const inner = 1;\n  function g(): void {}\n  void inner;\n}\n"
    )
    assert cd.check_ts(src, "a.tsx") == []


def test_ts_decorators_and_line_comments_between_block_and_declaration() -> None:
    """What: a `//` line between the JSDoc and the declaration does not detach it. Use when: pytest. Inputs: none.
    Output: assert. Side effects: none. Role: robustness to style."""
    src = _ts_block() + 'import { a } from "a";\n\n' + _ts_block() + "// eslint-disable-next-line x\nexport const a1 = 1;\n"
    assert cd.check_ts(src, "a.ts") == []


def test_excluded_files() -> None:
    """What: generated files are excluded by the explicit list. Use when: pytest. Inputs: none.
    Output: assert. Side effects: none. Role: generated code is not hand-documented."""
    assert cd.is_excluded("apps/web/next-env.d.ts")
    assert cd.is_excluded("packages/contracts/ts/contracts.ts")
    assert cd.is_excluded("apps/web/pnpm-lock.yaml")
    assert not cd.is_excluded("apps/web/src/lib/utils.ts")
    assert not cd.is_excluded("services/core/src/core/main.py")


def _git(repo: Path, *args: str) -> str:
    """What: run git in a repo. Use when: building fixture repos. Inputs: repo, args. Output: stdout.
    Side effects: runs git. Role: test helper."""
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args], cwd=repo, check=True, capture_output=True, text=True
    ).stdout


def test_changed_files_only_and_all_mode(tmp_path: Path) -> None:
    """What: changed mode checks only files changed since the merge base; --all checks every tracked file.
    Use when: pytest. Inputs: tmp_path. Output: assert. Side effects: git in tmp_path. Role: proves the diff scoping."""
    _git(tmp_path, "init", "-q", "-b", "main")
    (tmp_path / "old.py").write_text("def old() -> None:\n    return None\n")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-qm", "base")
    _git(tmp_path, "update-ref", "refs/remotes/origin/main", "HEAD")
    _git(tmp_path, "switch", "-qc", "feature")
    (tmp_path / "new.py").write_text("def new() -> None:\n    return None\n")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-qm", "feature")
    assert cd.run(tmp_path, all_files=False, base="origin/main") == 1
    changed = cd.collect_files(tmp_path, all_files=False, base="origin/main")
    assert changed == ["new.py"]
    assert sorted(cd.collect_files(tmp_path, all_files=True, base="origin/main")) == ["new.py", "old.py"]


def test_run_passes_when_changed_file_is_documented(tmp_path: Path) -> None:
    """What: a PR that only adds documented code exits 0, even if an old file has gaps. Use when: pytest.
    Inputs: tmp_path. Output: assert. Side effects: git in tmp_path. Role: main passes while the backlog is open."""
    _git(tmp_path, "init", "-q", "-b", "main")
    (tmp_path / "old.py").write_text("def old() -> None:\n    return None\n")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-qm", "base")
    _git(tmp_path, "update-ref", "refs/remotes/origin/main", "HEAD")
    _git(tmp_path, "switch", "-qc", "feature")
    (tmp_path / "ok.py").write_text(f'"""{GOOD}"""\n\n\ndef ok() -> None:\n    """{GOOD}"""\n')
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-qm", "feature")
    assert cd.run(tmp_path, all_files=False, base="origin/main") == 0
    assert cd.run(tmp_path, all_files=True, base="origin/main") == 1


def test_ts_unexported_plain_constants_are_not_declarations() -> None:
    """What: a non-exported plain const (a fixture value) needs no descriptor, but a non-exported arrow function does.
    Use when: pytest. Inputs: none. Output: assert. Side effects: none.
    Role: keeps the gate on behaviour, not on test fixture values (exported values and every function and class stay in scope)."""
    src = (
        _ts_block()
        + 'import { a } from "a";\n\nconst s1 = "x";\nconst helper = (): void => {};\nexport const E = 1;\nfunction g(): void {}\n'
    )
    got = {g.name for g in cd.check_ts(src, "a.ts")}
    assert got == {"helper", "E", "g"}
