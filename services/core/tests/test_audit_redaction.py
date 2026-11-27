"""What: unit tests (no database) for the audit row builder and writer: PIN and email redaction, the hash, the logs.
Use when: changing core/audit.py. Inputs: none. Output: pytest results. Side effects: none.
Role: CS-E-03.S13.T2 Done-when, the part that does not need Postgres: a test proves the PIN is in the fixture,
then absent from the audit row and the logs (doc 07 s11)."""
import hashlib
import json
import logging
import uuid
from datetime import datetime
from zoneinfo import ZoneInfo

from contracts.redact import RedactContext
from psycopg.types.json import Jsonb

from core.audit import AuditEvent, audit_row, write_audit
from core.clock import FixedClock
from tests.pin_probe import leaks

PIN = "4821"
EMAIL = "priya@acme.com"
TS = datetime(2026, 11, 27, 9, 30, tzinfo=ZoneInfo("America/New_York"))


def fixture_args(pin: str = PIN) -> dict:
    """What: audit args that contain a card PIN, a step-up PIN and a full email. Use when: tests.
    Inputs: the PIN text. Output: dict. Side effects: none. Role: the secret-bearing fixture."""
    return {"account_id": "QS-10423", "pin": pin, "nested": {"step_up_pin": pin, "billing_email": EMAIL},
            "list": [{"pin": pin}]}


class FakeConn:
    """What: records execute calls instead of talking to Postgres. Use when: unit-testing write_audit.
    Inputs: none. Output: .calls. Side effects: none. Role: lets the writer be tested without a database."""

    def __init__(self) -> None:
        """What: start empty. Use when: constructing. Inputs: none. Output: none. Side effects: none. Role: setup."""
        self.calls: list[tuple[str, tuple]] = []

    def execute(self, sql: str, params: tuple = ()) -> None:
        """What: record one statement. Use when: the writer inserts. Inputs: sql, params. Output: none.
        Side effects: appends to calls. Role: fake."""
        self.calls.append((sql, params))


def test_the_pin_is_in_the_fixture_and_in_a_raw_dump():
    """What: the raw fixture and its plain JSON contain the PIN and the full email. Use when: always.
    Inputs: none. Output: assertions. Side effects: none.
    Role: proves a zero is possible (a redaction test that cannot fail proves nothing)."""
    raw = json.dumps(fixture_args())
    assert raw.count(PIN) == 3 and EMAIL in raw


def test_audit_row_has_no_pin_and_masked_email():
    """What: the built row keeps account_id, replaces every PIN, masks the email. Use when: always.
    Inputs: none. Output: assertions. Side effects: none. Role: doc 07 s11 rows for audit."""
    row = audit_row(AuditEvent(event="verify", actor="agent", args=fixture_args()), RedactContext(), TS, uuid.uuid4())
    blob = json.dumps(row, default=str)
    assert not leaks(blob, PIN) and EMAIL not in blob
    assert row["args_redacted"]["account_id"] == "QS-10423"
    assert row["args_redacted"]["pin"] == "[PIN]"
    assert row["args_redacted"]["nested"]["billing_email"] == "p***@acme.com"


def test_hash_is_over_redacted_args_so_it_cannot_be_used_to_brute_force_the_pin():
    """What: two events differing only in the PIN get the same args_sha256, and it equals sha256 of the redacted JSON.
    Use when: always. Inputs: none. Output: assertions. Side effects: none.
    Role: a hash of raw args would let anyone with the audit table try all 10,000 PINs."""
    a = audit_row(AuditEvent(event="verify", actor="agent", args=fixture_args("4821")), RedactContext(), TS, uuid.uuid4())
    b = audit_row(AuditEvent(event="verify", actor="agent", args=fixture_args("1234")), RedactContext(), TS, uuid.uuid4())
    assert a["args_sha256"] == b["args_sha256"]
    canonical = json.dumps(a["args_redacted"], sort_keys=True, separators=(",", ":"), default=str).encode()
    assert a["args_sha256"] == hashlib.sha256(canonical).digest()


def test_write_audit_inserts_the_redacted_row_and_logs_nothing_secret(caplog):
    """What: write_audit sends one INSERT whose parameters and the log lines hold no PIN or full email.
    Use when: always. Inputs: caplog. Output: assertions. Side effects: none.
    Role: the 'absent from audit rows and logs' half of the Done-when."""
    conn, clock = FakeConn(), FixedClock(TS)
    with caplog.at_level(logging.DEBUG):
        audit_id = write_audit(conn, AuditEvent(event="verify", actor="agent", session_id=uuid.uuid4(),
                                                trace_id="t-1", args=fixture_args()), clock=clock)
    assert len(conn.calls) == 1 and conn.calls[0][0].lstrip().startswith("INSERT INTO audit_events")
    params = [x.obj if isinstance(x, Jsonb) else x for x in conn.calls[0][1]]  # Jsonb repr is truncated: unwrap it
    assert any(isinstance(x, dict) and x.get("pin") == "[PIN]" for x in params)  # args_redacted is really inspected
    sink = json.dumps(params, default=str) + "\n".join(r.getMessage() for r in caplog.records)
    assert not leaks(sink, PIN) and EMAIL not in sink
    assert any(str(audit_id) in r.getMessage() for r in caplog.records)  # it did log, so the absence means something


def test_free_text_email_in_a_string_value_is_masked():
    """What: an email inside a longer string value is masked too. Use when: always. Inputs: none.
    Output: assertion. Side effects: none. Role: redact applies to every string, not only known keys."""
    row = audit_row(AuditEvent(event="x", actor="system", args={"note": f"changed to {EMAIL} today"}),
                    RedactContext(), TS, uuid.uuid4())
    assert row["args_redacted"]["note"] == "changed to p***@acme.com today"


def test_no_args_means_null_columns():
    """What: an event without args stores NULL args and NULL hash. Use when: always. Inputs: none.
    Output: assertions. Side effects: none. Role: no fake empty hash."""
    row = audit_row(AuditEvent(event="lookup", actor="agent"), RedactContext(), TS, uuid.uuid4())
    assert row["args_redacted"] is None and row["args_sha256"] is None
