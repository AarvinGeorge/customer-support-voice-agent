"""What: writes packages/contracts/ts/contracts.ts from the Python contract models.
Use when: after any change to packages/contracts/src/contracts (CI checks the file is current).
Inputs: none. Run: cd packages/contracts && uv run python scripts/gen_ts.py
Output: the TS file on disk and one line naming it.
Side effects: overwrites ts/contracts.ts.
Role: the contract sync generator (doc 07 s8.1 "mirrored to TypeScript")."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from contracts.ts_export import render_ts  # noqa: E402

OUT = Path(__file__).resolve().parent.parent / "ts" / "contracts.ts"


def main() -> None:
    """What: render and write the file. Use when: CLI. Inputs: none. Output: prints the path.
    Side effects: writes OUT. Role: entry point."""
    OUT.write_text(render_ts())
    print(f"wrote {OUT}")


if __name__ == "__main__":
    main()
