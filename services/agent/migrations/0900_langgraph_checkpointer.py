"""What: migrate hook that creates the LangGraph checkpointer tables with AsyncPostgresSaver.setup().
Use when: run by infra/migrate.py after the agent SQL files, as agent_owner (AGENT_DB_OWNER_URL), on every
migrate run (setup() is idempotent: it records applied steps in checkpoint_migrations).
Inputs: the owner database URL. agent_owner's role search_path is `agent` (infra/postgres/init), so the
tables land in schema agent; default privileges give agent_app DML on them.
Output: none.
Side effects: creates or upgrades agent.checkpoints, checkpoint_blobs, checkpoint_writes, checkpoint_migrations;
revokes every agent_app privilege on checkpoint_migrations (review PR #14 I1: the app role must not rewrite
the ledger; the runtime saver never reads it). agent_app keeps DML on the three data tables.
Role: doc 07 s12 "LangGraph checkpointer tables"; spike 8 (07 s14 row 8). A hook because setup() runs
CREATE INDEX CONCURRENTLY, which cannot run inside the SQL runner's transaction (D-047).
"""
from __future__ import annotations

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver


async def run(url: str) -> None:
    """What: open an autocommit saver on the owner URL and call setup().
    Use when: infra/migrate.py run_hooks. Inputs: owner URL. Output: none.
    Side effects: DDL and one REVOKE in schema agent. Role: hook entry point."""
    async with AsyncPostgresSaver.from_conn_string(url) as saver:
        await saver.setup()
        await saver.conn.execute("REVOKE ALL ON agent.checkpoint_migrations FROM agent_app")
