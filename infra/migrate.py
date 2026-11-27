"""What: apply a service's SQL migrations in filename order as that service's owner role.
Use when: the migrate_<service> Compose job runs (SERVICE and <SERVICE>_DB_OWNER_URL set).
Inputs: env SERVICE; env <SERVICE>_DB_OWNER_URL; files /app/migrations/*.sql.
Output: one log line per applied or skipped file; exit 0 on success, non-zero on any failure.
Side effects: creates <schema>.schema_migrations; runs each new file in its own transaction and records it.
Role: the single migration wiring for all three services (doc 07 s15.1 row 3); each lane writes its own SQL files."""
from __future__ import annotations

import os
import sys
from pathlib import Path

import psycopg


def apply_migrations(url: str, schema: str, directory: Path) -> list[str]:
    """What: run pending migration files and record them.
    Use when: called by main. Inputs: owner database URL, schema name, migrations directory.
    Output: names of files applied this run. Side effects: DDL as the owner role; the ledger table.
    Role: idempotent migration runner (a file never runs twice)."""
    applied: list[str] = []
    with psycopg.connect(url, autocommit=True) as conn:
        conn.execute(f"CREATE TABLE IF NOT EXISTS {schema}.schema_migrations "
                     "(name text PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now())")
        done = {r[0] for r in conn.execute(f"SELECT name FROM {schema}.schema_migrations")}
        for path in sorted(directory.glob("*.sql")):
            if path.name in done:
                print(f"skip {path.name}")
                continue
            with conn.transaction():
                conn.execute(path.read_text())
                conn.execute(f"INSERT INTO {schema}.schema_migrations (name) VALUES (%s)", (path.name,))
            print(f"applied {path.name}")
            applied.append(path.name)
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
