"""What: fails if a tool module of services/agent reaches the commit or ops code, client or token (doc 07 s1.4).
Use when: CI (and locally). Tool wrappers hold only the agent token; commit code lives in `ingress/confirm.py`,
ops code in `ops/`, and the commit and ops tokens are `CORE_TOKEN_COMMIT` and `CORE_TOKEN_OPS`.
Inputs: argv[0] = repo root (default: the parent of scripts/). Scope: every .py under services/agent that sits in a
`tools` directory or is named `tools.py`.
Output: exit 0 and "import guard: N tool modules scanned, 0 violations"; exit 1 and one `path:line` per violation.
Forbidden: (1) any import (absolute or relative, plain, from, or aliased name) that resolves to `ingress.confirm` or to
an `ops` package; (2) an import path or imported name that is a commit or ops client (`commit_client`, `OpsClient`,
`core_commit`, `core_ops`; whole snake-case words, so `devops_client` is fine); (3) `importlib.import_module` or
`__import__` with such a literal, or with a non-literal argument; (4) the strings CORE_TOKEN_COMMIT or CORE_TOKEN_OPS.
Side effects: reads source files; prints. Role: the import guard gate of CS-E-01.S5.T1 (passes trivially while the agent has no tools)."""
import ast
import re
import sys
from pathlib import Path

CLIENT_WORD = re.compile(r"(^|_)((commit|ops)_?client|core_(commit|ops))($|_)")
TOKEN_NAME = re.compile(r"CORE_TOKEN_(COMMIT|OPS)")


def _snake(segment: str) -> str:
    """What: lower snake-case a dotted-path segment (`CoreCommitClient` -> `core_commit_client`).
    Use when: matching names by whole words. Inputs: one segment. Output: str. Side effects: none. Role: helper."""
    return re.sub(r"(?<!^)(?=[A-Z])", "_", segment).lower()


def _forbidden_path(dotted: str) -> bool:
    """What: decide whether a dotted import path or imported name is forbidden to tool modules.
    Use when: checking every import candidate. Inputs: dotted string. Output: bool. Side effects: none.
    Role: rules (1) and (2) of the module docstring."""
    parts = [p for p in dotted.split(".") if p]
    if "ops" in parts:
        return True
    if any(a == "ingress" and b == "confirm" for a, b in zip(parts, parts[1:])):
        return True
    return any(CLIENT_WORD.search(_snake(p)) for p in parts)


def _package_of(path: Path, agent: Path) -> list[str]:
    """What: the dotted package a module lives in, relative to services/agent (a leading `src` is dropped).
    Use when: resolving relative imports. Inputs: module path, services/agent path. Output: package parts.
    Side effects: none. Role: helper for relative imports."""
    parts = list(path.relative_to(agent).parts[:-1])
    return parts[1:] if parts[:1] == ["src"] else parts


def _candidates(node: ast.AST, pkg: list[str]) -> list[str]:
    """What: every dotted path an import statement can refer to (module, and module.name per alias).
    Use when: scanning one statement. Inputs: AST node, the file's package. Output: dotted strings.
    Side effects: none. Role: normalises import forms."""
    if isinstance(node, ast.Import):
        return [a.name for a in node.names]
    if isinstance(node, ast.ImportFrom):
        base = pkg[: len(pkg) - (node.level - 1)] if node.level else []
        mod = ".".join([*base, *([node.module] if node.module else [])])
        return [mod] + [f"{mod}.{a.name}" for a in node.names]
    return []


def _dynamic_import_violation(node: ast.AST) -> bool:
    """What: true for importlib.import_module(x) or __import__(x) with a forbidden literal or a non-literal x.
    Use when: scanning calls. Inputs: AST node. Output: bool. Side effects: none. Role: rule (3)."""
    if not (isinstance(node, ast.Call) and node.args):
        return False
    fn = node.func
    name = fn.attr if isinstance(fn, ast.Attribute) else fn.id if isinstance(fn, ast.Name) else ""
    if name not in ("import_module", "__import__"):
        return False
    arg = node.args[0]
    if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
        return _forbidden_path(arg.value)
    return True


def tool_modules(root: Path) -> list[Path]:
    """What: find every .py file in scope under services/agent (in a `tools` directory, or named tools.py).
    Use when: choosing what to scan. Inputs: repo root. Output: sorted paths (possibly empty).
    Side effects: reads the directory tree. Role: defines the scope of the rule (the commit flow may import the client)."""
    agent = root / "services" / "agent"
    if not agent.is_dir():
        return []
    return sorted(
        p for p in agent.rglob("*.py")
        if "tools" in p.relative_to(agent).parts[:-1] or p.name == "tools.py"
    )


def find_violations(root: Path) -> list[str]:
    """What: scan the tool modules for forbidden imports, dynamic imports and token names.
    Use when: CI or tests. Inputs: repo root. Output: sorted unique `path:line` strings, empty when clean.
    Side effects: reads files. Role: the guard's core."""
    agent = root / "services" / "agent"
    found: set[str] = set()
    for path in tool_modules(root):
        text = path.read_text()
        rel = path.relative_to(root)
        pkg = _package_of(path, agent)
        for node in ast.walk(ast.parse(text, filename=str(path))):
            if any(_forbidden_path(c) for c in _candidates(node, pkg)) or _dynamic_import_violation(node):
                found.add(f"{rel}:{node.lineno}")  # type: ignore[attr-defined]
        for no, line in enumerate(text.splitlines(), 1):
            if TOKEN_NAME.search(line):
                found.add(f"{rel}:{no}")
    return sorted(found)


def main(argv: list[str]) -> int:
    """What: CLI entry. Use when: `python scripts/check_import_guard.py [root]`. Inputs: argv without program name.
    Output: exit code 0 or 1. Side effects: prints the result. Role: CI step."""
    root = Path(argv[0]) if argv else Path(__file__).resolve().parent.parent
    violations = find_violations(root)
    for v in violations:
        print(f"FORBIDDEN commit/ops access in a tool module: {v}")
    print(f"import guard: {len(tool_modules(root))} tool modules scanned, {len(violations)} violations")
    return 1 if violations else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
