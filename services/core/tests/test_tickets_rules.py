"""What: unit tests (no database) for the ticket policy mapping, id format, transition table and the
POST /v1/tickets request checks that run before any SQL.
Use when: pytest runs services/core/tests. Inputs: data/policy.yaml, the fake-token app from conftest.
Output: pass or fail. Side effects: reads data/policy.yaml.
Role: CS-E-03.S3.T1 rule checks that need no Postgres (the database half is tests/pg/test_tickets_pg.py)."""
from __future__ import annotations

import itertools
import re
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from contracts.core_api import TicketCategory
from contracts.policy import load_policy
from typing import get_args

from core.clock import FixedClock
from core.errors import CoreError
from core.tickets import check_transition, new_ticket_id, priority_for, queue_for, target_label_for
from tests.conftest import TOKENS

POLICY = load_policy(Path(__file__).resolve().parents[3] / "data" / "policy.yaml")
CLOCK = FixedClock(datetime(2026, 11, 27, 9, 30, tzinfo=ZoneInfo("America/New_York")))


@pytest.mark.parametrize("tier,blocking,want", [
    ("enterprise", False, "P2"), ("enterprise", True, "P1"), ("business", False, "P2"), ("business", True, "P2"),
    ("pro", False, "P3"), ("pro", True, "P3"), ("free", False, "P4"), ("free", True, "P4")])
def test_priority_by_tier_and_blocking(tier, blocking, want):
    """What: verified priority comes from policy by tier; only Enterprise blocking becomes P1.
    Use when: pytest runs. Inputs: a tier and flag. Output: none. Side effects: none. Role: L1 s5 A3 table."""
    assert priority_for(POLICY, tier=tier, verified=True, blocking=blocking) == want


@pytest.mark.parametrize("tier", ["enterprise", "business", "pro", "free", None])
def test_unverified_is_always_p4(tier):
    """What: an unverified session is P4 whatever the claimed tier or the blocking flag. Use when: pytest runs.
    Inputs: a tier. Output: none. Side effects: none. Role: L1 s5 A3 unverified rule."""
    assert priority_for(POLICY, tier=tier, verified=False, blocking=True) == "P4"


def test_every_category_maps_to_a_queue_from_policy():
    """What: all ten ticket categories have a queue, equal to policy.yaml category_queue.
    Use when: pytest runs. Inputs: none. Output: none. Side effects: none. Role: doc 07 s13.1 tickets."""
    cats = get_args(TicketCategory)
    assert len(cats) == 10
    assert {c: queue_for(POLICY, c) for c in cats} == dict(POLICY.tickets.category_queue)
    assert queue_for(POLICY, "refund") == "billing" and queue_for(POLICY, "sso") == "technical"


@pytest.mark.parametrize("priority,tier,want", [
    ("P1", "enterprise", "1 hour"), ("P2", "enterprise", "4 hours"), ("P2", "business", "8 hours"),
    ("P3", "pro", "24 hours"), ("P4", "free", "72 hours"), ("P4", None, "72 hours")])
def test_target_label(priority, tier, want):
    """What: the first-response label follows priority and, for P2, the tier. Use when: pytest runs.
    Inputs: priority, tier. Output: none. Side effects: none. Role: L1 s5 A3 first-response targets."""
    assert target_label_for(POLICY, priority, tier) == want


def test_ticket_id_format_is_tck_year_and_six_digits():
    """What: ids look like TCK-26118213 (TCK, two-digit business year, six digits) and use the injected rng.
    Use when: pytest runs. Inputs: none. Output: none. Side effects: none. Role: doc 07 s3.2, D-043."""
    tid = new_ticket_id(CLOCK, rng=lambda n: 118213)
    assert tid == "TCK-26118213"
    assert re.fullmatch(r"TCK-[0-9]{8}", new_ticket_id(CLOCK))
    assert new_ticket_id(CLOCK, rng=lambda n: 7) == "TCK-26000007"


ALLOWED = {("open", "resolved"), ("pending_approval", "resolved"), ("resolved", "closed")}


@pytest.mark.parametrize("src,dst", list(itertools.product(["open", "pending_approval", "resolved", "closed"], repeat=2)))
def test_transition_table_is_exactly_doc_07_s7_2(src, dst):
    """What: only the three doc 07 s7.2 stage changes are accepted; every other pair is INVALID_TRANSITION.
    Use when: pytest runs. Inputs: all 16 stage pairs. Output: none. Side effects: none."""
    if (src, dst) in ALLOWED:
        check_transition(src, dst)
    else:
        with pytest.raises(CoreError) as exc:
            check_transition(src, dst)
        assert exc.value.code == "INVALID_TRANSITION"


BODY = {"category": "billing", "summary": "Charged twice", "origin": "model"}


def _post(client, headers_extra=None, body=BODY):
    """What: POST /v1/tickets as the agent. Use when: a test needs the route. Inputs: extra headers, body.
    Output: response. Side effects: none. Role: shorthand."""
    headers = {"Authorization": f"Bearer {TOKENS['agent_tools']}", **(headers_extra or {})}
    return client.post("/v1/tickets", headers=headers, json=body)


def test_missing_idempotency_key_is_422(client):
    """What: a valid body without Idempotency-Key is 422 VALIDATION_ERROR naming the header.
    Use when: pytest runs. Inputs: client. Output: none. Side effects: none. Role: doc 07 s3.4 key required."""
    r = _post(client, {"X-Session-Id": "11111111-1111-4111-8111-111111111111"})
    assert r.status_code == 422
    assert r.json()["error"]["details"]["errors"][0]["loc"] == ["header", "idempotency-key"]


def test_missing_session_is_422(client):
    """What: no X-Session-Id is 422 (a ticket always belongs to a session). Use when: pytest runs.
    Inputs: client. Output: none. Side effects: none. Role: unverified tickets are per session."""
    r = _post(client, {"Idempotency-Key": "k1"})
    assert r.status_code == 422
    assert r.json()["error"]["details"]["errors"][0]["loc"] == ["header", "x-session-id"]


def test_invalid_body_is_422_without_echo(client):
    """What: category outside the ten, empty summary or 9 steps are 422. Use when: pytest runs. Inputs: client.
    Output: none. Side effects: none. Role: TicketCreate bounds, doc 07 s3.4."""
    h = {"X-Session-Id": "11111111-1111-4111-8111-111111111111", "Idempotency-Key": "k1"}
    for bad in ({**BODY, "category": "nonsense"}, {**BODY, "summary": ""}, {**BODY, "steps_tried": ["x"] * 9},
                {**BODY, "summary": "x" * 501}, {**BODY, "confidence": 1.5}):
        r = _post(client, h, bad)
        assert r.status_code == 422, bad
        assert "nonsense" not in r.text


def test_no_database_is_503_tool_error(client):
    """What: with a valid request but no database configured the answer is 503 TOOL_ERROR in the envelope.
    Use when: pytest runs. Inputs: client (db_url None). Output: none. Side effects: none. Role: no 500s."""
    r = _post(client, {"X-Session-Id": "11111111-1111-4111-8111-111111111111", "Idempotency-Key": "k1"})
    assert r.status_code == 503 and r.json()["error"]["code"] == "TOOL_ERROR"
