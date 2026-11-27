"""What: command line for the seeds: `python -m core.seeds` (seed an empty schema) and
`python -m core.seeds --reseed` (wipe the core tables except audit_events and seed again).
Use when: first deploy and before each demo take (Compose service seed_core). Inputs: env CORE_DB_OWNER_URL,
PIN_PEPPER, DEMO_RESEED (must be 1 or true for --reseed), SEEDS_DIR. Output: exit code 0 done or already seeded,
2 missing configuration, 3 reseed refused. Side effects: see core.seeds.seed.run_seed.
Role: doc 07 s3.6 (reseed is a CLI behind DEMO_RESEED, never an endpoint). Runs as core_owner because the
runtime role may not TRUNCATE or delete append-only history."""
from __future__ import annotations

import os
import sys
from collections.abc import Mapping, Sequence

from core.clock import SystemClock
from core.seeds.seed import SeedError, run_seed


def main(argv: Sequence[str] | None = None, env: Mapping[str, str] | None = None) -> int:
    """What: parse arguments and run the seeds. Use when: the module is executed or tested.
    Inputs: argv (default sys.argv[1:]), env (default os.environ). Output: exit code (see module).
    Side effects: prints one line to stdout or stderr; may write the database. Role: guards come before any connection."""
    args = list(sys.argv[1:] if argv is None else argv)
    e = os.environ if env is None else env
    reseed = "--reseed" in args
    unknown = [a for a in args if a != "--reseed"]
    if unknown:
        print(f"unknown arguments: {unknown}", file=sys.stderr)
        return 2
    for name in ("CORE_DB_OWNER_URL", "PIN_PEPPER"):
        if not e.get(name):
            print(f"{name} is not set", file=sys.stderr)
            return 2
    if reseed and e.get("DEMO_RESEED", "").strip().lower() not in ("1", "true"):
        print("refusing to reseed: DEMO_RESEED is not 1 or true", file=sys.stderr)
        return 3
    try:
        written = run_seed(e["CORE_DB_OWNER_URL"], clock=SystemClock(), pepper=e["PIN_PEPPER"],
                           seeds_dir=e.get("SEEDS_DIR") or None, reseed=reseed)
    except SeedError as exc:
        print(f"seed error: {exc}", file=sys.stderr)
        return 2
    print("reseeded" if reseed else ("seeded" if written else "already seeded, nothing changed"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
