"""What: fails if any module under a `tools` directory of services/agent imports the commit or ops client.
Use when: CI (and locally) to enforce doc 07 s1.4 "least privilege per code module": tool wrappers hold only the agent token.
Inputs: argv[1] = repo root (default: the parent of scripts/). Rule: FORBIDDEN matches imported module paths and imported names.
Output: exit 0 and "import guard: N tool modules scanned, 0 violations"; exit 1 and one `path:line` per violation.
Side effects: reads source files; prints. Role: the import guard gate of CS-E-01.S5.T1 (passes trivially until the agent has tools).
Note: FORBIDDEN is name based. When lane B names the real clients, tighten the pattern here and add a test."""
import ast
import re
import sys
from pathlib import Path

FORBIDDEN = re.compile(r"commit_?client|ops_?client|core_commit|core_ops|clients?\.(commit|ops)\b", re.IGNORECASE)


def _imported_names(tree: ast.AST) -> list[tuple[int, str]]:
    """What: list every imported dotted path and imported name with its line. Use when: scanning one module.
    Inputs: parsed AST. Output: (line, text) pairs, `module.name` for from-imports. Side effects: none.
    Role: normalises `import a.b`, `from a import b` and aliases into plain strings to match."""
    out: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            out += [(node.lineno, a.name) for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            out += [(node.lineno, f"{mod}.{a.name}") for a in node.names]
    return out


def tool_modules(root: Path) -> list[Path]:
    """What: find every .py file inside a `tools` directory under services/agent. Use when: choosing what to scan.
    Inputs: repo root. Output: sorted paths (possibly empty). Side effects: reads the directory tree.
    Role: defines the scope of the rule (tools only; the commit flow may import the client)."""
    agent = root / "services" / "agent"
    if not agent.is_dir():
        return []
    return sorted(p for p in agent.rglob("*.py") if "tools" in p.relative_to(agent).parts[:-1])


def find_violations(root: Path) -> list[str]:
    """What: scan the tool modules for forbidden imports. Use when: CI or tests. Inputs: repo root.
    Output: `path:line` strings, empty when clean. Side effects: reads files. Role: the guard's core."""
    found: list[str] = []
    for path in tool_modules(root):
        tree = ast.parse(path.read_text(), filename=str(path))
        for line, name in _imported_names(tree):
            if FORBIDDEN.search(name):
                found.append(f"{path.relative_to(root)}:{line}")
    return found


def main(argv: list[str]) -> int:
    """What: CLI entry. Use when: `python scripts/check_import_guard.py [root]`. Inputs: argv without program name.
    Output: exit code 0 or 1. Side effects: prints the result. Role: CI step."""
    root = Path(argv[0]) if argv else Path(__file__).resolve().parent.parent
    violations = find_violations(root)
    for v in violations:
        print(f"FORBIDDEN commit/ops client import in a tool module: {v}")
    print(f"import guard: {len(tool_modules(root))} tool modules scanned, {len(violations)} violations")
    return 1 if violations else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
