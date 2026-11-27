"""What: Postgres-backed tests of the core migration: unique indexes, append-only audit, grants, ledger.
Use when: changing services/core/migrations. Inputs: the Compose Postgres (marker pg). Output: pytest results.
Side effects: all writes are rolled back. Role: CS-E-03.S13.T2 Done-when (second open pending row fails; audit refuses UPDATE as core_app)."""
import uuid

import psycopg
import pytest

pytestmark = pytest.mark.pg


def test_expected_tables_exist(app_conn):
    """What: the slice's core tables exist and the operator table does not (designed only).
    Use when: always. Inputs: app_conn. Output: assertions. Side effects: none. Role: scope of the migration."""
    rows = app_conn.execute("SELECT table_name FROM information_schema.tables WHERE table_schema = 'core'").fetchall()
    have = {r[0] for r in rows}
    want = {"accounts", "users", "charges", "session_claims", "verify_attempts", "pending_actions",
            "action_history", "tickets", "ticket_events", "idempotency", "audit_events", "meta"}
    assert want <= have, want - have
    assert "operators" not in have


def test_second_open_pending_row_for_one_session_fails(app_conn, seed):
    """What: a second pending row in state prepared or awaiting_customer for one session is rejected by the DB.
    Use when: always. Inputs: app_conn, seed. Output: assertions. Side effects: rolled back.
    Role: doc 07 s7.1 unique index (one open row per session); proves the first insert works so the failure is the index."""
    session = uuid.uuid4()
    seed.pending(session, f"refund:{seed.charge_id}", "prepared")  # the first row is accepted
    with pytest.raises(psycopg.errors.UniqueViolation) as exc, app_conn.transaction():
        seed.pending(session, "refund:CH-99999999", "awaiting_customer")
    assert "pa_one_open_per_session" in str(exc.value)


def test_closed_and_awaiting_approval_rows_do_not_count_as_open(app_conn, seed):
    """What: superseded, committed and awaiting_approval rows may coexist with one open row in a session.
    Use when: always. Inputs: app_conn, seed. Output: assertions. Side effects: rolled back.
    Role: the index is partial (open = prepared or awaiting_customer), so history does not block new prepares."""
    session = uuid.uuid4()
    seed.pending(session, "refund:CH-00000001", "superseded")
    seed.pending(session, "refund:CH-00000002", "committed")
    seed.pending(session, "refund:CH-00000003", "awaiting_approval")
    seed.pending(session, "refund:CH-00000004", "prepared")


def test_second_live_row_for_one_target_fails_across_sessions(app_conn, seed):
    """What: two live rows (prepared, awaiting_customer or awaiting_approval) for one target fail, even in
    different sessions. Use when: always. Inputs: app_conn, seed. Output: assertions. Side effects: rolled back.
    Role: doc 07 s7.1 second unique index."""
    target = f"refund:{seed.charge_id}"
    seed.pending(uuid.uuid4(), target, "awaiting_approval")
    with pytest.raises(psycopg.errors.UniqueViolation) as exc, app_conn.transaction():
        seed.pending(uuid.uuid4(), target, "prepared")
    assert "pa_one_live_per_target" in str(exc.value)
    seed.pending(uuid.uuid4(), target, "cancelled")  # a dead row on the same target is fine


def test_a_charge_can_be_refunded_once(app_conn, seed):
    """What: a second refund history row for one target is rejected. Use when: always. Inputs: app_conn, seed.
    Output: assertions. Side effects: rolled back. Role: ah_refund_once, the DB guard behind the double-refund rule."""
    sql = ("INSERT INTO action_history (account_id, user_id, action_type, target_key, amount_cents, effective_date, "
           "committed_by) VALUES (%s,%s,'refund',%s,9600,current_date,'customer')")
    args = (seed.account_id, seed.user_id, f"refund:{seed.charge_id}")
    app_conn.execute(sql, args)
    with pytest.raises(psycopg.errors.UniqueViolation) as exc, app_conn.transaction():
        app_conn.execute(sql, args)
    assert "ah_refund_once" in str(exc.value)


def _audit_row(conn) -> uuid.UUID:
    """What: insert one audit row and return its id. Use when: append-only tests. Inputs: connection.
    Output: id. Side effects: one INSERT. Role: fixture row."""
    return conn.execute("INSERT INTO audit_events (actor, event) VALUES ('system','probe') RETURNING id").fetchone()[0]


def test_audit_refuses_update_and_delete_as_core_app(app_conn):
    """What: as the runtime role, UPDATE, DELETE and TRUNCATE on audit_events are refused (no privilege).
    Use when: always. Inputs: app_conn. Output: assertions. Side effects: rolled back.
    Role: Done-when 'audit_events refuses UPDATE (as core_app)'; the INSERT first proves the role can write."""
    row = _audit_row(app_conn)
    for sql in ("UPDATE audit_events SET event = 'x' WHERE id = %s", "DELETE FROM audit_events WHERE id = %s"):
        with pytest.raises(psycopg.errors.InsufficientPrivilege), app_conn.transaction():
            app_conn.execute(sql, (row,))
    with pytest.raises(psycopg.errors.InsufficientPrivilege), app_conn.transaction():
        app_conn.execute("TRUNCATE audit_events")


def test_audit_trigger_blocks_even_the_owner(owner_conn):
    """What: the table owner also cannot UPDATE, DELETE or TRUNCATE audit_events (trigger). Use when: always.
    Inputs: owner_conn. Output: assertions. Side effects: rolled back.
    Role: a migration-role mistake must not be able to rewrite the audit trail."""
    row = _audit_row(owner_conn)
    for sql, args in (("UPDATE audit_events SET event = 'x' WHERE id = %s", (row,)),
                      ("DELETE FROM audit_events WHERE id = %s", (row,)), ("TRUNCATE audit_events", ())):
        with pytest.raises(psycopg.errors.RaiseException, match="append-only"), owner_conn.transaction():
            owner_conn.execute(sql, args)


def test_action_history_and_ticket_events_are_append_only_for_core_app(app_conn, seed):
    """What: core_app cannot UPDATE or DELETE action_history and cannot DELETE ticket_events. Use when: always.
    Inputs: app_conn, seed. Output: assertions. Side effects: rolled back. Role: draft C section 2.2 grants."""
    app_conn.execute("INSERT INTO action_history (account_id, user_id, action_type, target_key, effective_date, committed_by) "
                     "VALUES (%s,%s,'refund','refund:CH-1',current_date,'customer')", (seed.account_id, seed.user_id))
    for sql in ("UPDATE action_history SET amount_cents = 1", "DELETE FROM action_history"):
        with pytest.raises(psycopg.errors.InsufficientPrivilege), app_conn.transaction():
            app_conn.execute(sql)
    with pytest.raises(psycopg.errors.InsufficientPrivilege), app_conn.transaction():
        app_conn.execute("DELETE FROM ticket_events")


def test_app_role_cannot_touch_the_migration_ledger(app_conn):
    """What: core_app cannot read core.schema_migrations. Use when: always. Inputs: app_conn. Output: assertion.
    Side effects: none. Role: migrate.py revokes it; the schema keeps that promise."""
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        app_conn.execute("SELECT * FROM schema_migrations")


def test_migration_is_idempotent(owner_url, migrated):
    """What: running the migrations again applies nothing. Use when: always. Inputs: owner_url, migrated.
    Output: assertion. Side effects: none. Role: the migrate job runs on every `up`."""
    import migrate  # pyright: ignore[reportMissingImports]
    from tests.pg.conftest import MIGRATIONS
    assert migrate.apply_migrations(owner_url, "core", MIGRATIONS) == []


def test_database_seed_date_reads_core_meta(app_url, migrated):
    """What: Database.seed_date returns "unseeded" with no meta row and the stored date after one is written.
    Use when: always. Inputs: app_url. Output: assertions. Side effects: commits a meta row and restores the previous one.
    Role: the /readyz seed_date source (doc 07 s3.6)."""
    from core.db import Database

    db = Database(app_url, max_size=2)
    db.open()
    with db.tx() as conn:
        prior = conn.execute("SELECT value FROM meta WHERE key = 'seed_date'").fetchone()
    try:
        with db.tx() as conn:
            conn.execute("DELETE FROM meta WHERE key = 'seed_date'")
        assert db.seed_date() == "unseeded"
        with db.tx() as conn:
            conn.execute("INSERT INTO meta (key, value) VALUES ('seed_date', '2026-11-27')")
        assert db.seed_date() == "2026-11-27"
    finally:
        with db.tx() as conn:  # restore whatever a seeder had written
            conn.execute("DELETE FROM meta WHERE key = 'seed_date'")
            if prior:
                conn.execute("INSERT INTO meta (key, value) VALUES ('seed_date', %s)", (prior[0],))
        db.close()
