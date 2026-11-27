"""What: static checks on the agent schema migration against doc 07 s12 (the live run is in the report).
Use when: pytest. Inputs: none. Output: pass/fail. Side effects: none. Role: test for migrations/0001_agent_schema.sql."""
from __future__ import annotations

import re
from pathlib import Path

import pytest

SQL = (Path(__file__).resolve().parents[1] / "migrations" / "0001_agent_schema.sql").read_text()

COLUMNS = {
    "sessions": ["session_id", "visitor_id", "created_at", "last_active_at", "ip_hash", "user_turns",
                 "awaiting_verification", "verified_user_id", "voice_usage"],
    "voice_sessions": ["voice_session_id", "session_id", "minted_at", "expires_at", "ended_at"],
    "turns": ["turn_id", "session_id", "client_turn_id", "mode", "input_kind", "status", "end_reason",
              "deadline_at", "started_at", "ended_at", "delivered", "model_used", "fallback_used",
              "realtime_call_id", "final", "usage", "error", "events_complete", "ls_summary", "eval_run_id"],
    "turn_events": ["turn_id", "seq", "session_id", "ts", "type", "step_id", "payload"],
    "transcript_items": ["id", "session_id", "voice_session_id", "realtime_item_id", "turn_id", "role", "mode",
                         "source", "text", "interrupted", "merged_into_turn_id", "created_at"],
}


def table_body(name: str) -> str:
    """What: the column list of CREATE TABLE agent.<name>. Use when: tests. Inputs: table name.
    Output: text between the parentheses. Side effects: none. Role: helper."""
    m = re.search(rf"CREATE TABLE agent\.{name} \((.*?)\n\);", SQL, re.S)
    assert m, f"agent.{name} missing"
    return m.group(1)


@pytest.mark.parametrize("table", sorted(COLUMNS))
def test_every_doc07_column_exists(table):
    body = table_body(table)
    cols = [line.strip().split()[0] for line in body.splitlines() if line.strip() and
            not line.strip().upper().startswith(("PRIMARY", "UNIQUE", "CHECK", "FOREIGN", "CONSTRAINT", "--", "'"))]
    assert cols == COLUMNS[table]


def test_keys_and_uniques_from_doc07():
    assert "session_id   uuid        PRIMARY KEY" in table_body("sessions")
    assert "UNIQUE (session_id, client_turn_id)" in table_body("turns")
    assert "PRIMARY KEY (turn_id, seq)" in table_body("turn_events")
    assert "UNIQUE (session_id, realtime_item_id)" in table_body("transcript_items")
    assert "REFERENCES agent.turns(turn_id) ON DELETE CASCADE" in table_body("turn_events")
    assert "CREATE INDEX turn_events_session_ts ON agent.turn_events (session_id, ts);" in SQL


def test_turn_events_is_insert_and_select_only_for_the_app_role():
    assert "REVOKE UPDATE, DELETE, TRUNCATE ON agent.turn_events FROM agent_app;" in SQL


def test_no_cross_schema_reference_and_no_checkpointer_ddl():
    code = "\n".join(line for line in SQL.splitlines() if not line.lstrip().startswith("--"))
    assert not re.search(r"\b(core|knowledge)\.", code)
    assert "checkpoint" not in code.lower()  # the 0900 hook owns those tables


def test_contract_enums_are_checked():
    from contracts.events import END_REASONS
    turns = table_body("turns")
    assert "CHECK (mode IN ('chat', 'voice'))" in turns
    assert "CHECK (status IN ('running', 'completed', 'failed'))" in turns
    for reason in END_REASONS:
        assert f"'{reason}'" in turns
