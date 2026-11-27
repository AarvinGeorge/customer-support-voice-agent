"""What: writes packages/contracts/fixtures/*.jsonl from contracts.fixtures_build.
Use when: after changing the models or the fixture builder (CI checks the files are current).
Inputs: none. Run: cd packages/contracts && uv run python scripts/build_fixtures.py
Output: the JSONL files on disk and one line per file.
Side effects: overwrites fixtures/*.jsonl.
Role: fixture generator for lanes B and C."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from contracts.fixtures_build import render_all  # noqa: E402


def main() -> None:
    """What: render and write every fixture. Use when: CLI. Inputs: none. Output: prints paths.
    Side effects: writes files. Role: entry point."""
    for name, text in render_all().items():
        path = ROOT / "fixtures" / name
        path.write_text(text)
        print(f"wrote {path} ({text.count(chr(10))} events)")


if __name__ == "__main__":
    main()
