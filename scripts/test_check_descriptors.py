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


# ---------------------------------------------------------------- fix round 1 (review PR #39, D-056)

def test_light_paths_need_only_a_module_descriptor() -> None:
    """What: tests and spikes are classified light, application source strict (D-056). Use when: pytest. Inputs: none.
    Output: assert. Side effects: none. Role: the scope split."""
    for p in ("services/core/tests/test_x.py", "services/core/tests/pg/conftest.py", "scripts/test_keys.py",
              "spikes/agent/spike_01.py", "apps/web/src/lib/utils.test.ts", "apps/web/src/components/a.test.tsx"):
        assert cd.is_light(p), p
    for p in ("services/core/src/core/app.py", "packages/contracts/src/contracts/x.py", "apps/web/src/lib/utils.ts",
              "scripts/check_descriptors.py", "infra/migrate.py"):
        assert not cd.is_light(p), p


def test_light_python_file_reports_only_the_module_gap(tmp_path: Path) -> None:
    """What: a test file with a module descriptor and an undocumented function passes; without the module descriptor
    it fails on the module only. Use when: pytest. Inputs: tmp_path. Output: assert. Side effects: git in tmp_path.
    Role: D-056 for Python, through run()."""
    _git(tmp_path, "init", "-q", "-b", "main")
    (tmp_path / "keep.txt").write_text("x")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-qm", "base")
    _git(tmp_path, "update-ref", "refs/remotes/origin/main", "HEAD")
    _git(tmp_path, "switch", "-qc", "feature")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_a.py").write_text(f'"""{GOOD}"""\n\n\ndef test_a() -> None:\n    return None\n')
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-qm", "feature")
    assert cd.run(tmp_path, all_files=False, base="origin/main") == 0
    (tmp_path / "tests" / "test_a.py").write_text("def test_a() -> None:\n    return None\n")
    assert cd.run(tmp_path, all_files=False, base="origin/main") == 1


def test_light_ts_file_reports_only_the_module_gap() -> None:
    """What: check_ts with light=True ignores declarations. Use when: pytest. Inputs: none. Output: assert.
    Side effects: none. Role: D-056 for TypeScript."""
    src = 'import { a } from "a";\n\nexport function f(): void {}\n'
    assert [g.name for g in cd.check_ts(src, "a.test.ts", light=True)] == ["<module>"]
    assert cd.check_ts(_ts_block() + src, "a.test.ts", light=True) == []
    assert cd.check_python("def f():\n    return 1\n", "tests/test_a.py", light=True)[0].name == "<module>"
    assert len(cd.check_python("def f():\n    return 1\n", "tests/test_a.py", light=True)) == 1


def _repo_with_change(tmp_path: Path, rel: str, text: str) -> None:
    """What: a repo whose feature branch adds one file. Use when: root and path tests. Inputs: tmp_path, relative path,
    text. Output: none. Side effects: git in tmp_path. Role: test helper."""
    _git(tmp_path, "init", "-q", "-b", "main")
    (tmp_path / "keep.txt").write_text("x")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-qm", "base")
    _git(tmp_path, "update-ref", "refs/remotes/origin/main", "HEAD")
    _git(tmp_path, "switch", "-qc", "feature")
    (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
    (tmp_path / rel).write_text(text)
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-qm", "feature")


def test_running_from_a_subdirectory_still_checks_the_changed_files(tmp_path: Path) -> None:
    """What: --root pointing at a subdirectory resolves the repo top level and checks the changed file (it used to
    check 0 files and exit 0). Use when: pytest. Inputs: tmp_path. Output: assert. Side effects: git in tmp_path.
    Role: review PR #39 I2."""
    _repo_with_change(tmp_path, "services/x/src/x.py", "def f() -> None:\n    return None\n")
    assert cd.run(tmp_path / "services" / "x", all_files=False, base="origin/main") == 1
    assert cd.collect_files(tmp_path / "services" / "x", all_files=False, base="origin/main") == ["services/x/src/x.py"]


def test_a_changed_file_that_cannot_be_read_is_an_error(monkeypatch, tmp_path: Path) -> None:
    """What: git lists a code file that is not on disk: exit with a message, never skip it. Use when: pytest.
    Inputs: monkeypatch, tmp_path. Output: assert. Side effects: none. Role: review PR #39 I2 (no vacuous green)."""
    _repo_with_change(tmp_path, "a.py", f'"""{GOOD}"""\n')
    real = cd._git

    def fake(root: Path, *args: str) -> str:
        """What: pretend the diff names a ghost file. Use when: collect_files calls git diff. Inputs: root, args.
        Output: git output with one extra path. Side effects: none. Role: test double."""
        out = real(root, *args)
        return out + "ghost.py\0" if args and args[-1:] != ("--show-toplevel",) and "diff" in args else out

    monkeypatch.setattr(cd, "_git", fake)
    try:
        cd.collect_files(tmp_path, all_files=False, base="origin/main")
    except SystemExit as exc:
        assert "ghost.py" in str(exc)
    else:
        raise AssertionError("a missing changed file must fail loudly")


def test_non_ascii_path_is_checked(tmp_path: Path) -> None:
    """What: a changed file with a non-ASCII name is listed and checked (git quotes such names by default).
    Use when: pytest. Inputs: tmp_path. Output: assert. Side effects: git in tmp_path. Role: review PR #39 M4."""
    _repo_with_change(tmp_path, "services/x/src/naïve.py", "def f() -> None:\n    return None\n")
    assert cd.run(tmp_path, all_files=False, base="origin/main") == 1


def test_ts_unexported_function_shapes_are_declarations() -> None:
    """What: multi-line parameters, generic and async generic arrows, memo and forwardRef wrappers, and a typed const
    are functions and need a block; value constants (a filter call, vi.hoisted) do not. Use when: pytest. Inputs: none.
    Output: assert. Side effects: none. Role: review PR #39 I3."""
    cases = {
        "const h = (\n  a: string,\n  b: number,\n): void => {\n};\n": "h",
        "const h = <T,>(a: T): T => a;\n": "h",
        "const h = async <T,>(a: T): Promise<T> => a;\n": "h",
        "const Card = memo(function Card() {\n  return null;\n});\n": "Card",
        "const Btn = forwardRef((p, r) => null);\n": "Btn",
        "const Btn = React.forwardRef<HTMLDivElement, P>((p, r) => null);\n": "Btn",
        "const h: Handler = (a) => a;\n": "h",
        "const h = function () {};\n": "h",
    }
    for code, name in cases.items():
        src = _ts_block() + 'import { a } from "a";\n\n' + code
        assert [g.name for g in cd.check_ts(src, "a.ts")] == [name], code
    for code in ('const c1 = s1.filter((e) => e.ok);\n', 'const nav = vi.hoisted(() => ({ a: 1 }));\n',
                 'const x: Record<string, () => void> = {};\n'):
        src = _ts_block() + 'import { a } from "a";\n\n' + code
        assert cd.check_ts(src, "a.ts") == [], code


def test_ts_exported_constants_always_need_a_block() -> None:
    """What: an exported const of any value needs a block. Use when: pytest. Inputs: none. Output: assert.
    Side effects: none. Role: review PR #39 I3 (exported consts)."""
    src = _ts_block() + 'import { a } from "a";\n\nexport const LIMITS = { a: 1 };\nexport const f = (\n  a: string,\n): void => {};\n'
    assert [g.name for g in cd.check_ts(src, "a.ts")] == ["LIMITS", "f"]


def test_empty_labels_and_stale_block_are_gaps() -> None:
    """What: a docstring with the six labels but no text is a gap; a plain one-line comment does not borrow an older
    JSDoc above it. Use when: pytest. Inputs: none. Output: assert. Side effects: none. Role: review PR #39 M1, M3."""
    empty = '"""What: Use when: Inputs: Output: Side effects: Role:"""\n'
    assert cd.check_python(empty, "a.py")[0].name == "<module>"
    one = "/** What: x. Use when: y. Inputs: n. Output: n. Side effects: n. Role: z. */\n"
    src = _ts_block() + 'import { a } from "a";\n\n' + one + "export function a1(): void {}\n\n/* plain */\nexport function b1(): void {}\n"
    assert [g.name for g in cd.check_ts(src, "a.ts")] == ["b1"]


def test_a_block_that_mentions_a_glob_inside_its_text_is_still_one_block() -> None:
    """What: a descriptor whose text contains "/api/*" is read as one block (a line holding the characters /* mid-line
    is not a comment start). Use when: pytest. Inputs: none. Output: assert. Side effects: none.
    Role: regression found by running the fixed checker on apps/web/src/lib/api/client.ts."""
    block = _ts_block().replace(" * Use when: y.", " * Use when: any fetch to `/api/*` fails.")
    src = _ts_block() + 'import { a } from "a";\n\n' + block + "export function f(): void {}\n"
    assert cd.check_ts(src, "a.ts") == []


# ---------------------------------------------------------------- fix round 2 (re-review N1)

IMP = 'import { a } from "a";\n'


def test_label_text_may_start_with_a_slash_or_a_star() -> None:
    """What: labels whose text starts with `/` or `*` (route paths, `*args`) are complete, in Python and in JSDoc; a
    label with no text is still a gap. Use when: pytest. Inputs: none. Output: assert. Side effects: none.
    Role: re-review N1 (the four false gaps found on main)."""
    py = ('"""What: /healthz, /readyz and more. Use when: /readyz. Inputs: *args. Output: none. '
          'Side effects: none. Role: route paths."""\n')
    assert cd.check_python(py, "a.py") == []
    block = "/**\n * What: /healthz probe.\n * Use when: /readyz.\n * Inputs: *args.\n * Output: none.\n * Side effects: none.\n * Role: z.\n */\n"
    assert cd.check_ts(block + IMP, "a.ts") == []
    one = "/** What: /a. Use when: /b. Inputs: *c. Output: none. Side effects: none. Role: /z. */\n"
    assert cd.check_ts(one + IMP, "a.ts") == []


def test_truly_empty_labels_are_still_gaps_in_jsdoc() -> None:
    """What: a JSDoc whose last label has only the line prefix or the closing `*/` after it is a gap; so is a label
    followed straight by another label. Use when: pytest. Inputs: none. Output: assert. Side effects: none.
    Role: the star is a line prefix, not content (re-review N1)."""
    block = "/**\n * What: x.\n * Use when: y.\n * Inputs: n.\n * Output: n.\n * Side effects: n.\n * Role:\n */\n"
    assert cd.check_ts(block + IMP, "a.ts")[0].problem == "labels: Role:"
    one = "/** What: x. Use when: y. Inputs: n. Output: n. Side effects: n. Role: */\n"
    assert cd.check_ts(one + IMP, "a.ts")[0].problem == "labels: Role:"
    py = '"""What: Use when: y. Inputs: n. Output: n. Side effects: n. Role: z."""\n'
    assert cd.check_python(py, "a.py")[0].problem == "labels: What:"
