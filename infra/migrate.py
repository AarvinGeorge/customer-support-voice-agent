"""What: apply a service's migrations as that service's owner role: SQL files first, then Python hooks.
Use when: the migrate_<service> Compose job runs (SERVICE and <SERVICE>_DB_OWNER_URL set).
Inputs: env SERVICE; env <SERVICE>_DB_OWNER_URL; files /app/migrations/*.sql (each in one transaction, run once)
and /app/migrations/*.py (hooks, run after all SQL, outside any transaction, on every run).
Output: one log line per applied, skipped or hook file; exit 0 on success, non-zero on any failure.
Side effects: creates <schema>.schema_migrations (app role has no access to it); DDL and hook work as the owner role.
Role: the single migration wiring for all three services (doc 07 s15.1 row 3). A hook is a module with
`run(url)` (sync) or `async def run(url)`; it receives the owner URL and must be idempotent. Lane B uses one to call
AsyncPostgresSaver.setup() in schema `agent` (it cannot run inside a SQL transaction)."""
from __future__ import annotations

import asyncio
import importlib.util
import inspect
import os
import sys
from collections.abc import Awaitable
from pathlib import Path
from typing import Any


async def _await(awaitable: Awaitable[Any]) -> Any:
    """What: await any awaitable so asyncio.run can drive it (asyncio.run accepts only coroutines).
    Use when: a hook's run(url) returns an awaitable. Inputs: the awaitable. Output: its result.
    Side effects: whatever the awaitable does. Role: adapter between the hook contract and asyncio.run."""
    return await awaitable


def run_hooks(url: str, directory: Path) -> list[str]:
    """What: run every migrations/*.py hook in filename order, outside any transaction.
    Use when: after the SQL files are applied. Inputs: owner database URL, migrations directory.
    Output: names of hooks run. Side effects: whatever each hook does (the hook opens its own connection).
    Role: escape hatch for setup that is Python or cannot run in a transaction (CREATE INDEX CONCURRENTLY)."""
    ran: list[str] = []
    for path in sorted(directory.glob("*.py")):
        spec = importlib.util.spec_from_file_location(f"migration_hook_{path.stem}", path)
        if spec is None or spec.loader is None:
            raise RuntimeError(f"cannot load hook {path.name}")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        run = getattr(module, "run", None)
        if run is None:
            raise RuntimeError(f"hook {path.name} has no run(url) function")
        result = run(url)
        if inspect.isawaitable(result):
            asyncio.run(_await(result))
        print(f"hook {path.name}")
        ran.append(path.name)
    return ran


def apply_migrations(url: str, schema: str, directory: Path) -> list[str]:
    """What: run pending SQL files (one transaction each), record them, then run the Python hooks.
    Use when: called by main. Inputs: owner database URL, schema name, migrations directory.
    Output: names of SQL files applied this run. Side effects: DDL as the owner role; the ledger table.
    Role: idempotent migration runner (a SQL file never runs twice; hooks must be idempotent)."""
    import psycopg  # imported here so run_hooks can be unit tested without the driver
    from psycopg import sql

    applied: list[str] = []
    with psycopg.connect(url, autocommit=True) as conn:
        # Names go through sql.Identifier (quoted by the driver), never into the query text.
        ledger = sql.Identifier(schema, "schema_migrations")
        conn.execute(sql.SQL("CREATE TABLE IF NOT EXISTS {} "
                             "(name text PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now())").format(ledger))
        # default privileges would hand the ledger to the app role; it must not be able to rewrite history
        conn.execute(sql.SQL("REVOKE ALL ON {} FROM {}").format(ledger, sql.Identifier(f"{schema}_app")))
        done = {r[0] for r in conn.execute(sql.SQL("SELECT name FROM {}").format(ledger))}
        for path in sorted(directory.glob("*.sql")):
            if path.name in done:
                print(f"skip {path.name}")
                continue
            with conn.transaction():
                # A migration file is trusted repo text, not a literal; the driver accepts bytes for such queries.
                conn.execute(path.read_text().encode())
                conn.execute(sql.SQL("INSERT INTO {} (name) VALUES (%s)").format(ledger), (path.name,))
            print(f"applied {path.name}")
            applied.append(path.name)
    run_hooks(url, directory)
    return applied


def main() -> int:
    """What: read env and run the migrations. Use when: container entrypoint.
    Inputs: env SERVICE, <SERVICE>_DB_OWNER_URL. Output: exit code. Side effects: see apply_migrations.
    Role: entry point; fails loudly if the URL is missing instead of doing nothing."""
    service = os.environ.get("SERVICE", "")
    if service not in {"agent", "core", "knowledge"}:
        print(f"SERVICE must be agent, core or knowledge, got {service!r}", file=sys.stderr)
        return 2
    url = os.environ.get(f"{service.upper()}_DB_OWNER_URL", "")
    if not url:
        print(f"{service.upper()}_DB_OWNER_URL is not set", file=sys.stderr)
        return 2
    applied = apply_migrations(url, service, Path(__file__).parent / "migrations")
    print(f"migrate {service}: {len(applied)} applied")
    return 0


if __name__ == "__main__":
    sys.exit(main())
