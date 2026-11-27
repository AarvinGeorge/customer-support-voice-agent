"""What: the per-case hooks that prepare and clean the stack: reseed core (the DEMO_RESEED CLI) and, for cases that carry
test-only fixture articles, ingest and remove them.
Use when: the runner starts and ends a case. Inputs: shell command strings (shlex-split, no shell), run function.
Output: two callables (before, after). Side effects: runs the given commands. Role: a case with fixtures and no ingest command is
a HookError (the case would otherwise pass without its attack in play); a non-zero exit is a HookError too."""
from __future__ import annotations

import shlex
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

GOLDEN = Path(__file__).resolve().parent / "golden"


class HookError(Exception):
    """What: raised when a reseed or fixture command cannot run. Use when: a hook fails. Inputs: message.
    Output: exception. Side effects: none. Role: the case grades error."""


def make_hooks(reseed_cmd: str, ingest_cmd: str | None, remove_cmd: str | None, run: Callable[..., Any] = subprocess.run
               ) -> tuple[Callable[[dict[str, Any]], None], Callable[[dict[str, Any]], None]]:
    """What: build the before and after case hooks. Use when: the CLI assembles a run. Inputs: reseed command, ingest command
    template with {path}, remove command template with {path}, a subprocess runner. Output: (before, after).
    Side effects: the hooks run subprocesses. Role: stack preparation per case."""

    def call(cmd: str, what: str) -> None:
        """What: run one command and raise HookError on failure. Use when: a hook step. Inputs: command text, label. Output: none. Side effects: runs a subprocess. Role: uniform failure handling."""
        try:
            r = run(shlex.split(cmd), capture_output=True, text=True, timeout=300)
        except (OSError, subprocess.TimeoutExpired) as e:
            raise HookError(f"{what} could not run: {type(e).__name__}") from e
        if r.returncode != 0:
            raise HookError(f"{what} exited {r.returncode}: {(r.stderr or '')[:200]}")

    def fixture_paths(case: dict[str, Any]) -> list[str]:
        """What: absolute paths of the case's fixture articles. Use when: ingest or remove. Inputs: case. Output: list of paths. Side effects: none. Role: path resolution."""
        return [str(GOLDEN / p) for p in (case.get("fixtures") or {}).get("articles", [])]

    def before(case: dict[str, Any]) -> None:
        """What: reseed core then ingest the case's fixtures. Use when: a case is about to run. Inputs: case. Output: none. Side effects: runs the reseed and ingest commands. Role: clean state per case."""
        call(reseed_cmd, "reseed")
        paths = fixture_paths(case)
        if paths and not ingest_cmd:
            raise HookError(f"{case['id']} needs test-only fixture articles but no fixture ingest command is configured")
        for p in paths:
            assert ingest_cmd is not None
            call(ingest_cmd.replace("{path}", p), "fixture ingest")

    def after(case: dict[str, Any]) -> None:
        """What: remove the case's fixtures. Use when: a case finished. Inputs: case. Output: none. Side effects: runs the remove command. Role: cleanup."""
        if remove_cmd:
            for p in fixture_paths(case):
                call(remove_cmd.replace("{path}", p), "fixture remove")

    return before, after
