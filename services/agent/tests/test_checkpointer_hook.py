"""What: unit tests for the migrate hook that creates the LangGraph checkpointer tables (spike 8).
Use when: pytest (no database; the saver is faked). Inputs: none. Output: pass/fail.
Side effects: none. Role: test for services/agent/migrations/0900_langgraph_checkpointer.py."""
from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any

HOOK = Path(__file__).resolve().parents[1] / "migrations" / "0900_langgraph_checkpointer.py"


def load_hook() -> Any:
    """What: import the hook the way infra/migrate.py does. Use when: tests. Inputs: none.
    Output: module. Side effects: none. Role: helper."""
    spec = importlib.util.spec_from_file_location("hook_under_test", HOOK)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


async def test_hook_runs_setup_on_the_owner_url(monkeypatch):
    calls: list[Any] = []

    class FakeSaver:
        """What: records setup. Use when: this test. Inputs: none. Output: none.
        Side effects: appends. Role: fake AsyncPostgresSaver."""

        async def setup(self) -> None:
            """What: record. Use when: hook calls it. Inputs: none. Output: none.
            Side effects: appends. Role: fake."""
            calls.append("setup")

    class FakeCM:
        """What: async cm yielding FakeSaver. Use when: this test. Inputs: none. Output: saver.
        Side effects: appends on exit. Role: fake."""

        async def __aenter__(self) -> FakeSaver:
            """What: enter. Use when: async with. Inputs: none. Output: saver. Side effects: none. Role: fake."""
            return FakeSaver()

        async def __aexit__(self, *exc: Any) -> None:
            """What: exit. Use when: async with. Inputs: exc. Output: none. Side effects: appends. Role: fake."""
            calls.append("closed")

    hook = load_hook()
    monkeypatch.setattr(hook.AsyncPostgresSaver, "from_conn_string",
                        staticmethod(lambda url: calls.append(url) or FakeCM()))
    await hook.run("postgresql://agent_owner@postgres/quillstack")
    assert calls == ["postgresql://agent_owner@postgres/quillstack", "setup", "closed"]


def test_hook_has_async_run_entry_point():
    import inspect
    hook = load_hook()
    assert inspect.iscoroutinefunction(hook.run)
