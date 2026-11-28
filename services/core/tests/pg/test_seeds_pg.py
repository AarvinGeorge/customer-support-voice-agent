"""What: Postgres-backed tests of apply_seed: rows land with the right shapes, reseed on two different days keeps
Natasha's duplicate 5 days old, meta.seed_date follows, audit survives, a second plain seed is a no-op.
Use when: changing core/seeds. Inputs: the Compose Postgres (marker pg). Output: pytest results.
Side effects: everything runs in a core_owner transaction that is rolled back (TRUNCATE is transactional).
Role: CS-E-09.S4.T1 Done-when against the real schema."""
from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

from core.clock import FixedClock
from core.seeds.seed import apply_seed, load_seed_data, pin_hmac, plan_rows

pytestmark = pytest.mark.pg
NY = ZoneInfo("America/New_York")
PEPPER = "pg-test-pepper"


def clock_on(day: date) -> FixedClock:
    """What: a clock at noon New York on `day`. Use when: seeding on a chosen day. Inputs: a date.
    Output: FixedClock. Side effects: none. Role: frozen time."""
    return FixedClock(datetime(day.year, day.month, day.day, 12, 0, tzinfo=NY))


def seed(conn, day: date, reseed: bool) -> bool:
    """What: plan and apply the seeds on `day`. Use when: tests. Inputs: connection, day, reseed flag.
    Output: whether rows were written. Side effects: writes through conn. Role: helper."""
    clock = clock_on(day)
    return apply_seed(conn, plan_rows(load_seed_data(), clock.now(), PEPPER), clock, reseed=reseed)


def dup_age(conn, today: date) -> int:
    """What: age in days of Natasha's duplicate charge as stored. Use when: asserting. Inputs: connection, today.
    Output: int. Side effects: one SELECT. Role: the Done-when measurement, read from the database."""
    ts = conn.execute("SELECT charged_at FROM charges WHERE id = 'CH-00000412'").fetchone()[0]
    return (today - ts.astimezone(NY).date()).days


def test_reseed_on_two_different_days_keeps_the_duplicate_five_days_old(owner_conn):
    """What: reseed on 27 Nov, then on 4 Dec (and a DST-adjacent 3 Nov): each time the stored duplicate is 5 days
    old, the row counts are unchanged and meta.seed_date is that day. Use when: always. Inputs: owner_conn.
    Output: assertions. Side effects: rolled back. Role: Done-when 'reseeding on two different days keeps
    Natasha's duplicate charge 5 days old'."""
    counts = []
    for day in (date(2026, 11, 27), date(2026, 12, 4), date(2026, 11, 3)):
        assert seed(owner_conn, day, reseed=True) is True
        assert dup_age(owner_conn, day) == 5
        assert owner_conn.execute("SELECT value FROM meta WHERE key='seed_date'").fetchone()[0] == day.isoformat()
        counts.append(tuple(owner_conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
                            for t in ("accounts", "users", "charges", "action_history")))
    assert len(set(counts)) == 1 and counts[0][0] >= 8


def test_users_carry_a_verifiable_pin_hmac_and_no_plain_pin(owner_conn):
    """What: Natasha's stored pin_hmac equals the HMAC of her persona PIN; the row text holds no plain PIN.
    Use when: always. Inputs: owner_conn. Output: assertions. Side effects: rolled back. Role: draft C C-6."""
    seed(owner_conn, date(2026, 11, 27), reseed=True)
    persona = next(p for p in load_seed_data().personas_raw if p["name"] == "Natasha Romanoff")
    row = owner_conn.execute("SELECT t::text, pin_hmac FROM users t WHERE display_name = 'Natasha Romanoff'").fetchone()
    assert bytes(row[1]) == pin_hmac(PEPPER, persona["account_id"], persona["pin"])
    assert persona["pin"] not in row[0]


def test_plain_seed_is_a_no_op_when_data_exists(owner_conn):
    """What: seeding without reseed on a populated schema returns False and changes nothing (even on a later day).
    Use when: always. Inputs: owner_conn. Output: assertions. Side effects: rolled back.
    Role: a repeated deploy must not wipe a demo in progress."""
    assert seed(owner_conn, date(2026, 11, 27), reseed=True)
    owner_conn.execute("UPDATE accounts SET name = 'Edited' WHERE id = 'QS-10423'")
    assert seed(owner_conn, date(2026, 12, 4), reseed=False) is False
    assert owner_conn.execute("SELECT name FROM accounts WHERE id = 'QS-10423'").fetchone()[0] == "Edited"
    assert owner_conn.execute("SELECT value FROM meta WHERE key='seed_date'").fetchone()[0] == "2026-11-27"


def test_plain_seed_populates_an_empty_schema(owner_conn):
    """What: after emptying the seeded tables, a plain seed writes everything. Use when: always.
    Inputs: owner_conn. Output: assertions. Side effects: rolled back. Role: first deploy path."""
    owner_conn.execute("TRUNCATE accounts, users, charges, session_claims, verify_attempts, pending_actions, "
                       "action_history, tickets, ticket_events, idempotency, meta")
    assert seed(owner_conn, date(2026, 11, 27), reseed=False) is True
    assert owner_conn.execute("SELECT count(*) FROM accounts").fetchone()[0] >= 8


def test_reseed_keeps_audit_rows_and_adds_one(owner_conn):
    """What: reseed does not remove existing audit rows and writes one `reseed` row with counts only (no PINs).
    Use when: always. Inputs: owner_conn. Output: assertions. Side effects: rolled back.
    Role: audit_events is never truncated (draft C reseed note); the trigger would refuse anyway."""
    owner_conn.execute("INSERT INTO audit_events (actor, event) VALUES ('system', 'before')")
    before = owner_conn.execute("SELECT count(*) FROM audit_events").fetchone()[0]
    seed(owner_conn, date(2026, 11, 27), reseed=True)
    after = owner_conn.execute("SELECT count(*) FROM audit_events").fetchone()[0]
    assert after == before + 1
    row = owner_conn.execute("SELECT mode, actor, args_redacted::text FROM audit_events WHERE event = 'reseed' "
                             "ORDER BY ts DESC LIMIT 1").fetchone()
    assert row[0] == "system" and row[1] == "system"
    persona_pins = [p["pin"] for p in load_seed_data().personas_raw]
    assert not any(pin in row[2] for pin in persona_pins)


def test_seeded_rows_satisfy_the_unique_guards(owner_conn):
    """What: seeded refund history respects ah_refund_once, and no seeded pending or ticket rows exist
    (those arrive through the product flow). Use when: always. Inputs: owner_conn. Output: assertions.
    Side effects: rolled back. Role: seeds start every scenario from a clean slate."""
    seed(owner_conn, date(2026, 11, 27), reseed=True)
    for t in ("pending_actions", "tickets", "session_claims"):
        assert owner_conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0] == 0
    assert owner_conn.execute("SELECT count(*) FROM action_history WHERE action_type = 'refund'").fetchone()[0] >= 1
