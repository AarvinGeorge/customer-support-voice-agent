"""What: unit tests (no database) for the account-read labels and the query and header checks of GET /v1/me/context,
/me/charges and /me/tickets that run before any SQL.
Use when: pytest runs services/core/tests. Inputs: the fake-token app from conftest. Output: pass or fail.
Side effects: none.
Role: CS-E-03.S13.T3 rule checks that need no Postgres (the database half is tests/pg/test_reads_pg.py)."""
from __future__ import annotations

import pytest

from core.reads import action_label, charge_label, money
from tests.conftest import TOKENS

SID = "11111111-1111-4111-8111-111111111111"
AGENT = {"Authorization": f"Bearer {TOKENS['agent_tools']}"}
WITH_SESSION = {**AGENT, "X-Session-Id": SID}


@pytest.mark.parametrize("cents,want", [(9600, "$96.00"), (5, "$0.05"), (1152000, "$11,520.00"), (150, "$1.50")])
def test_money(cents, want):
    """What: cents become dollars with two decimals and thousands commas. Use when: pytest runs. Inputs: cents.
    Output: none. Side effects: none. Role: history labels ("Refunded $96.00 ...")."""
    assert money(cents) == want


@pytest.mark.parametrize("invoice,charge_id,want", [("INV-2026-0412", "CH-00000412", "invoice ending 0412"),
                                                    (None, "CH-00000355", "charge ending 0355")])
def test_charge_label(invoice, charge_id, want):
    """What: the speakable label is the invoice's last four digits, else the charge id's. Use when: pytest runs.
    Inputs: invoice number, charge id. Output: none. Side effects: none. Role: doc 07 s3.2 ChargeView.label."""
    assert charge_label(invoice, charge_id) == want


@pytest.mark.parametrize("kind,target,cents,want", [
    ("refund", "refund:CH-00000412", 9600, "Refunded $96.00 for charge ending 0412"),
    ("billing_email", "billing_email:QS-10423", None, "Changed the billing email"),
    ("grace_extension", "grace_extension:QS-50234", None, "Extended the payment grace period"),
    ("notification_prefs", "notification_prefs:U-1001", None, "Updated notification preferences")])
def test_action_label(kind, target, cents, want):
    """What: each action type has a plain label; a refund names its amount and charge ending. Use when: pytest runs.
    Inputs: type, target key, cents. Output: none. Side effects: none. Role: doc 07 s3.1 ActionHistoryItem.label."""
    assert action_label(kind, target, cents) == want


@pytest.mark.parametrize("path", ["/v1/me/context", "/v1/me/charges", "/v1/me/tickets"])
def test_reads_need_a_session_header(client, path):
    """What: no X-Session-Id is 422 naming the header. Use when: pytest runs. Inputs: client, a read path.
    Output: none. Side effects: none. Role: reads act for a session."""
    r = client.get(path, headers=AGENT)
    assert r.status_code == 422 and r.json()["error"]["details"]["errors"][0]["loc"] == ["header", "x-session-id"]


@pytest.mark.parametrize("query", ["ending=12", "ending=abcd", "ending=12345", "on=2026-13-01", "on=yesterday",
                                   "since_days=0", "since_days=401", "since_days=x", "limit=0", "limit=21"])
def test_charges_bad_query_is_422(client, query):
    """What: a malformed ending, on, since_days or limit is 422 in the envelope. Use when: pytest runs.
    Inputs: client, a query string. Output: none. Side effects: none. Role: doc 07 s3.2 query bounds."""
    r = client.get(f"/v1/me/charges?{query}", headers=WITH_SESSION)
    assert r.status_code == 422 and r.json()["error"]["code"] == "VALIDATION_ERROR"


@pytest.mark.parametrize("query", ["status=closed", "ending=12", "ending=abcd"])
def test_tickets_bad_query_is_422(client, query):
    """What: a bad status or ending is 422. Use when: pytest runs. Inputs: client, a query. Output: none.
    Side effects: none. Role: doc 07 s3.2 query bounds."""
    assert client.get(f"/v1/me/tickets?{query}", headers=WITH_SESSION).status_code == 422


@pytest.mark.parametrize("path", ["/v1/me/context", "/v1/me/charges", "/v1/me/tickets"])
def test_reads_without_database_are_503(client, path):
    """What: a valid read with no database is 503 TOOL_ERROR, never a 500. Use when: pytest runs. Inputs: client.
    Output: none. Side effects: none. Role: no 500s."""
    r = client.get(path, headers=WITH_SESSION)
    assert r.status_code == 503 and r.json()["error"]["code"] == "TOOL_ERROR"
