"""What: fixtures for the Postgres-backed core tests (marker `pg`), run against the Compose Postgres.
Use when: running `uv run --env-file ../../.env pytest -m pg` with the postgres service up (see compose.pgtest.yaml).
Inputs: env CORE_DB_OWNER_URL and CORE_DB_URL (compose hostnames), PG_TEST_HOST (default 127.0.0.1),
PG_TEST_PORT (default 55432).
Output: fixtures `owner_url`, `app_url`, `migrated`, `app_conn`, `owner_conn`, `seed`.
Side effects: applies the core migrations (idempotent); every test works inside a transaction that is rolled back.
Role: the DB harness. A missing variable FAILS the test (a skipped DB test would be a green run that tested nothing)."""
from __future__ import annotations

import os
import sys
import uuid
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import psycopg
import pytest

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "infra"))
import migrate  # noqa: E402  (infra/migrate.py, the same runner the migrate_core job uses)

MIGRATIONS = ROOT / "services" / "core" / "migrations"


def _to_host(url: str) -> str:
    """What: swap the compose hostname in a database URL for the published test host and port.
    Use when: building owner_url and app_url. Inputs: URL with host postgres. Output: URL for the host machine.
    Side effects: none. Role: host-run pytest reaches the stack through compose.pgtest.yaml's published port."""
    parts = urlsplit(url)
    netloc = f"{parts.username}:{parts.password}@" if parts.username else ""
    netloc += f"{os.environ.get('PG_TEST_HOST', '127.0.0.1')}:{os.environ.get('PG_TEST_PORT', '55432')}"
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


def _need(name: str) -> str:
    """What: read a required env var or fail the test loudly. Use when: building URLs. Inputs: name.
    Output: value. Side effects: pytest.fail when unset. Role: never skip silently."""
    value = os.environ.get(name, "")
    if not value:
        pytest.fail(f"{name} is not set; run: uv run --env-file ../../.env pytest -m pg (stack up, see compose.pgtest.yaml)")
    return value


@pytest.fixture(scope="session")
def owner_url() -> str:
    """What: core_owner URL reachable from the host. Use when: migrations and owner-level assertions.
    Inputs: env. Output: URL. Side effects: none. Role: see module."""
    return _to_host(_need("CORE_DB_OWNER_URL"))


@pytest.fixture(scope="session")
def app_url() -> str:
    """What: core_app URL reachable from the host. Use when: runtime-role assertions. Inputs: env.
    Output: URL. Side effects: none. Role: see module."""
    return _to_host(_need("CORE_DB_URL"))


@pytest.fixture(scope="session")
def migrated(owner_url: str) -> list[str]:
    """What: apply core migrations once per run. Use when: any pg test. Inputs: owner_url.
    Output: names applied this run (empty when already current). Side effects: DDL as core_owner.
    Role: tests run on the same schema the migrate job builds."""
    return migrate.apply_migrations(owner_url, "core", MIGRATIONS)


@pytest.fixture
def app_conn(app_url: str, migrated):
    """What: a core_app connection in a transaction that is rolled back at the end. Use when: runtime-role tests.
    Inputs: app_url. Output: connection. Side effects: none persist. Role: isolation without cleanup code."""
    with psycopg.connect(app_url) as conn:
        yield conn
        conn.rollback()


@pytest.fixture
def owner_conn(owner_url: str, migrated):
    """What: a core_owner connection, rolled back at the end. Use when: owner-level assertions (the audit trigger).
    Inputs: owner_url. Output: connection. Side effects: none persist. Role: see module."""
    with psycopg.connect(owner_url) as conn:
        yield conn
        conn.rollback()


class Seed:
    """What: inserts minimal rows (account, user, charge) with unique ids inside the caller's transaction.
    Use when: a pg test needs foreign-key parents. Inputs: a connection. Output: ids as attributes.
    Side effects: INSERTs (rolled back by the fixture). Role: test data builder."""

    def __init__(self, conn: psycopg.Connection) -> None:
        """What: create account, user and charge. Use when: constructing. Inputs: connection. Output: none.
        Side effects: three INSERTs. Role: parents for pending_actions, action_history, tickets."""
        n = uuid.uuid4().int % 90000 + 10000
        self.account_id = f"QS-{n}"
        self.user_id = f"U-{uuid.uuid4().int % 9000 + 1000}"
        self.charge_id = f"CH-{uuid.uuid4().int % 90000000 + 10000000}"
        self.conn = conn
        conn.execute("INSERT INTO accounts (id, name, tier, contract_status, seats) VALUES (%s,'Acme','pro','active',5)",
                     (self.account_id,))
        conn.execute("INSERT INTO users (id, account_id, display_name, role, pin_hmac) VALUES (%s,%s,'Priya','owner',%s)",
                     (self.user_id, self.account_id, uuid.uuid4().bytes))
        conn.execute("INSERT INTO charges (id, account_id, charged_at, amount_cents, description, status) "
                     "VALUES (%s,%s, now(), 9600, 'Pro plan', 'paid')", (self.charge_id, self.account_id))

    def pending(self, session_id: uuid.UUID, target_key: str, state: str = "prepared") -> str:
        """What: insert a pending_actions row. Use when: testing the unique indexes. Inputs: session id, target key,
        state. Output: the new id. Side effects: one INSERT. Role: concise row builder."""
        pid = f"PA-{uuid.uuid4().hex[:8].upper()}"
        self.conn.execute(
            "INSERT INTO pending_actions (id, session_id, account_id, user_id, action_type, pending_kind, target_key, "
            "params, decision, reason_codes, policy_version, state, prepare_turn_id) "
            "VALUES (%s,%s,%s,%s,'refund','commit',%s,'{}','allowed','{PRO_WITHIN_LIMITS}','v',%s,'turn-1')",
            (pid, session_id, self.account_id, self.user_id, target_key, state))
        return pid


@pytest.fixture
def seed(app_conn) -> Seed:
    """What: a Seed bound to app_conn. Use when: foreign-key parents are needed. Inputs: app_conn.
    Output: Seed. Side effects: INSERTs rolled back. Role: see Seed."""
    return Seed(app_conn)
