"""What: checks that README.md has one slot (a `#### <id> ...` heading) for every showcase item.
Use when: CI or by hand after editing README.md or the showcase list.
Inputs: argv[1] optional path to the showcase list (planning-docs/04); default is the built-in id list below.
Output: exit 0 and "ok: N showcase slots" when complete; exit 1 and the missing ids on stderr otherwise.
Side effects: reads README.md and the optional list; writes nothing.
Role: guard so a README edit cannot silently drop a showcase item (CS-E-08.S1.T1)."""
from __future__ import annotations

import re
import sys
from pathlib import Path

README = Path(__file__).resolve().parent.parent / "README.md"

# Showcase ids from planning-docs/04_showcase_list (section number, then item number).
SHOWCASE_IDS = (
    [f"1.{n}" for n in range(1, 11)]
    + [f"2.{n}" for n in range(1, 7)]
    + [f"3.{n}" for n in range(1, 7)]
    + [f"4.{n}" for n in range(1, 10)]
)


def ids_in_list(text: str) -> list[str]:
    """What: read showcase ids from the table rows of the showcase list.
    Use when: cross-checking the built-in list against doc 04. Inputs: the list's text.
    Output: ids like "1.1" in file order. Side effects: none. Role: keeps SHOWCASE_IDS honest."""
    return re.findall(r"^\| (\d\.\d+) \|", text, flags=re.M)


def missing_slots(readme: str, ids: list[str]) -> list[str]:
    """What: find the ids that have no `#### <id> ` heading in the README.
    Use when: checking README completeness. Inputs: README text, expected ids.
    Output: the missing ids. Side effects: none. Role: the core check."""
    outside_fences = re.sub(r"^```.*?^```", "", readme, flags=re.M | re.S)  # a heading inside a code block is not a slot
    have = set(re.findall(r"^#### (\d\.\d+) ", outside_fences, flags=re.M))
    return [i for i in ids if i not in have]


def main(argv: list[str]) -> int:
    """What: run the check. Use when: invoked as a script. Inputs: argv, optional list path.
    Output: exit code. Side effects: prints one line. Role: CLI entry."""
    ids = SHOWCASE_IDS
    if len(argv) > 1:
        ids = ids_in_list(Path(argv[1]).read_text())
        if not ids:
            print("no showcase ids found in the list file", file=sys.stderr)
            return 1
    gone = missing_slots(README.read_text(), ids)
    if gone:
        print("missing README slots: " + ", ".join(gone), file=sys.stderr)
        return 1
    print(f"ok: {len(ids)} showcase slots")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
