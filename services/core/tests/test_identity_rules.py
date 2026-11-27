"""What: unit tests (no database) for identity input normalization, the role to permissions map, the attempts-left
arithmetic and the request checks of POST /v1/identity/verify and GET /v1/sessions/{id}/claims that run before SQL.
Use when: pytest runs services/core/tests. Inputs: the fake-token app from conftest, data/policy.yaml.
Output: pass or fail. Side effects: reads data/policy.yaml.
Role: CS-E-03.S1.T1 rule checks that need no Postgres (the database half is tests/pg/test_identity_pg.py)."""
from __future__ import annotations

from pathlib import Path

import pytest
from contracts.policy import load_policy

from core.identity import attempts_left, normalize_account_id, normalize_pin, permissions_for
from tests.conftest import TOKENS

POLICY = load_policy(Path(__file__).resolve().parents[3] / "data" / "policy.yaml")
SID = "11111111-1111-4111-8111-111111111111"
AGENT = {"Authorization": f"Bearer {TOKENS['agent_tools']}"}


@pytest.mark.parametrize("raw,want", [
    ("QS-10423", "QS-10423"), ("qs-10423", "QS-10423"), ("  QS 10423 ", "QS-10423"), ("qs10423", "QS-10423"),
    ("Q S - 1 0 4 2 3", "QS-10423"), ("10423", "QS-10423"), ("QS_10423", "QS-10423"),
    ("QS-1042", None), ("QS-104234", None), ("AB-10423", None), ("", None), ("QS-1042x", None)])
def test_normalize_account_id(raw, want):
    """What: spoken or typed forms of an account id become QS-12345; anything else is None. Use when: pytest runs.
    Inputs: a raw string. Output: none. Side effects: none. Role: doc 09 'normalization' of S1.T1."""
    assert normalize_account_id(raw) == want


@pytest.mark.parametrize("raw,want", [
    ("4821", "4821"), (" 4821 ", "4821"), ("4 8 2 1", "4821"), ("48-21", "4821"),
    ("482", None), ("48211", None), ("48a1", None), ("", None)])
def test_normalize_pin(raw, want):
    """What: a PIN with spaces or dashes becomes four digits; any other shape is None. Use when: pytest runs.
    Inputs: a raw string. Output: none. Side effects: none. Role: normalization before the HMAC."""
    assert normalize_pin(raw, POLICY.identity.pin_digits) == want


@pytest.mark.parametrize("role,billing", [("owner", True), ("billing_admin", True), ("member", False)])
def test_permissions_by_role(role, billing):
    """What: billing read and actions follow policy billing_roles; own notification prefs are for everyone.
    Use when: pytest runs. Inputs: a role. Output: none. Side effects: none. Role: doc 07 s3.1 Claims.permissions."""
    p = permissions_for(POLICY, role)
    assert (p.billing_read, p.billing_actions, p.own_notification_prefs) == (billing, billing, True)


@pytest.mark.parametrize("failed,locked,want", [(0, False, 3), (1, False, 2), (2, False, 1), (3, True, 0), (5, False, 0)])
def test_attempts_left(failed, locked, want):
    """What: attempts left is max_failures minus failures, zero when locked. Use when: pytest runs.
    Inputs: counters. Output: none. Side effects: none. Role: doc 07 s3.1 attempts_left."""
    assert attempts_left(POLICY, failed, locked) == want


def test_verify_without_session_header_is_422(client):
    """What: no X-Session-Id is 422 naming the header (verify acts for a session). Use when: pytest runs.
    Inputs: client. Output: none. Side effects: none. Role: doc 07 s1.2."""
    r = client.post("/v1/identity/verify", headers=AGENT, json={"account_id": "QS-10423", "pin": "4821"})
    assert r.status_code == 422 and r.json()["error"]["details"]["errors"][0]["loc"] == ["header", "x-session-id"]


@pytest.mark.parametrize("body", [{"account_id": "QS-10423", "pin": "48"}, {"account_id": "nope", "pin": "4821"},
                                  {"account_id": "QS-10423", "pin": "4821", "extra": 1}, {"pin": "4821"}])
def test_verify_bad_shape_is_422_and_never_echoes_the_pin(client, body):
    """What: a malformed account id or PIN, an unknown field or a missing field is 422 and the PIN is not echoed.
    Use when: pytest runs. Inputs: client, a bad body. Output: none. Side effects: none. Role: doc 07 s11."""
    r = client.post("/v1/identity/verify", headers={**AGENT, "X-Session-Id": SID}, json=body)
    assert r.status_code == 422 and r.json()["error"]["code"] == "VALIDATION_ERROR"
    assert "4821" not in r.text and "QS-10423" not in r.text


def test_verify_without_database_is_503_tool_error(client):
    """What: a valid request with no database configured is 503 TOOL_ERROR in the envelope, never a 500.
    Use when: pytest runs. Inputs: client (db_url None). Output: none. Side effects: none. Role: no 500s."""
    r = client.post("/v1/identity/verify", headers={**AGENT, "X-Session-Id": SID},
                    json={"account_id": "QS-10423", "pin": "4821"})
    assert r.status_code == 503 and r.json()["error"]["code"] == "TOOL_ERROR"


def test_claims_without_database_is_503(client):
    """What: GET claims with no database is 503 TOOL_ERROR. Use when: pytest runs. Inputs: client.
    Output: none. Side effects: none. Role: no 500s."""
    r = client.get(f"/v1/sessions/{SID}/claims", headers=AGENT)
    assert r.status_code == 503 and r.json()["error"]["code"] == "TOOL_ERROR"
