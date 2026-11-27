"""What: tests that the core stub answers every slice path with a contract-valid body and records
the caller's token and headers.
Use when: CI. Inputs: none. Output: pass or fail. Side effects: none (in-process app).
Role: lets lane B build tools and ingress against core before lane A's core exists."""
import pytest
from fastapi.testclient import TestClient

from contracts import core_api as c
from contracts.errors import ErrorEnvelope
from contracts.stubs.core_stub import STUB_PENDING_ID, create_app

HEAD = {"Authorization": "Bearer test-agent-token", "X-Session-Id": "sess-1", "X-Trace-Id": "trace-1",
        "X-Request-Id": "req-1", "X-Mode": "chat"}

CASES = [
    ("POST", "/v1/identity/verify", {"account_id": "QS-10423", "pin": "4821"}, c.Claims),
    ("GET", "/v1/sessions/sess-1/claims", None, c.Claims),
    ("GET", "/v1/me/context", None, c.MeContext),
    ("GET", "/v1/me/profile", None, c.Profile),
    ("GET", "/v1/me/charges?ending=0412", None, c.ChargesResponse),
    ("GET", "/v1/me/tickets", None, c.TicketsResponse),
    ("POST", "/v1/actions/refund/prepare",
     {"charge_ref": {"ending": "0412", "on": "2026-11-21"}, "reason": "duplicate", "note": "charged twice"},
     c.PrepareResult),
    ("GET", f"/v1/actions/pending/{STUB_PENDING_ID}", None, c.PendingStatus),
    ("POST", f"/v1/actions/pending/{STUB_PENDING_ID}/commit", {}, c.CommitResult),
    ("POST", f"/v1/actions/pending/{STUB_PENDING_ID}/submit", {}, c.SubmitResult),
    ("POST", f"/v1/actions/pending/{STUB_PENDING_ID}/cancel", {}, c.CancelResult),
    ("POST", "/v1/tickets", {"category": "human_requested", "summary": "Wants a person", "origin": "model"},
     c.TicketCreateResponse),
    ("GET", "/readyz", None, c.CoreReady),
]


@pytest.fixture
def client():
    """What: a fresh stub app and test client. Use when: each test. Inputs: none.
    Output: (TestClient, app). Side effects: none. Role: test fixture."""
    app = create_app()
    return TestClient(app), app


@pytest.mark.parametrize("method,path,body,model", CASES)
def test_slice_paths_return_valid_bodies(client, method, path, body, model):
    """What: each slice path returns 200 and a body that validates. Use when: CI.
    Inputs: case row. Output: assertion. Side effects: none. Role: stub conformance."""
    tc, _ = client
    r = tc.request(method, path, json=body, headers={**HEAD, "Idempotency-Key": "k1", "X-Turn-Id": "turn-2"})
    assert r.status_code == 200, r.text
    model.model_validate(r.json())


def test_records_token_and_headers(client):
    """What: the stub records token, session, turn and idempotency headers per call.
    Use when: CI. Inputs: none. Output: assertion. Side effects: none. Role: lets tool tests
    assert the right token per tool (CS-E-03.S8.T6)."""
    tc, app = client
    tc.post(f"/v1/actions/pending/{STUB_PENDING_ID}/commit", json={},
            headers={**HEAD, "Authorization": "Bearer commit-token", "Idempotency-Key": STUB_PENDING_ID,
                     "X-Turn-Id": "turn-3"})
    call = app.state.calls[-1]
    assert call.method == "POST" and call.path.endswith("/commit")
    assert call.token == "commit-token"
    assert call.headers["x-turn-id"] == "turn-3"
    assert call.headers["idempotency-key"] == STUB_PENDING_ID
    assert call.headers["x-session-id"] == "sess-1"


def test_missing_token_is_401_envelope(client):
    """What: no bearer token gives 401 AUTH_INVALID in the envelope. Use when: CI.
    Inputs: none. Output: assertion. Side effects: none. Role: auth shape."""
    tc, _ = client
    r = tc.get("/v1/me/profile")
    assert r.status_code == 401
    assert ErrorEnvelope.model_validate(r.json()).error.code == "AUTH_INVALID"


def test_wrong_pin_is_verify_failed(client):
    """What: a wrong PIN gives 401 VERIFY_FAILED with attempts_left. Use when: CI.
    Inputs: none. Output: assertion. Side effects: none. Role: verification failure path."""
    tc, _ = client
    r = tc.post("/v1/identity/verify", json={"account_id": "QS-10423", "pin": "0000"}, headers=HEAD)
    assert r.status_code == 401
    err = ErrorEnvelope.model_validate(r.json()).error
    assert err.code == "VERIFY_FAILED" and err.details["attempts_left"] == 2


def test_bad_body_is_422_envelope(client):
    """What: an invalid body gives 422 VALIDATION_ERROR in the envelope. Use when: CI.
    Inputs: none. Output: assertion. Side effects: none. Role: validation shape."""
    tc, _ = client
    r = tc.post("/v1/actions/refund/prepare", json={"reason": "because"}, headers=HEAD)
    assert r.status_code == 422
    assert ErrorEnvelope.model_validate(r.json()).error.code == "VALIDATION_ERROR"


@pytest.mark.parametrize("path,body", [
    ("/v1/actions/refund/prepare", {"charge_ref": None, "reason": "duplicate", "note": ""}),
    (f"/v1/actions/pending/{STUB_PENDING_ID}/commit", {}),
])
def test_tool_error_fault(client, path, body):
    """What: X-Demo-Fault tool_error on prepare and commit gives 503 TOOL_ERROR, changed false,
    fault_injected true. Use when: CI. Inputs: path, body. Output: assertion. Side effects: none.
    Role: doc 07 s10 tool_error behaviour for S5 tests."""
    tc, _ = client
    r = tc.post(path, json=body, headers={**HEAD, "X-Demo-Fault": "tool_error"})
    assert r.status_code == 503
    err = ErrorEnvelope.model_validate(r.json()).error
    assert (err.code, err.retryable, err.changed, err.fault_injected) == ("TOOL_ERROR", True, False, True)


def test_fault_ignored_on_reads(client):
    """What: tool_error is ignored on status reads. Use when: CI. Inputs: none.
    Output: assertion. Side effects: none. Role: doc 07 s10 scope."""
    tc, _ = client
    r = tc.get(f"/v1/actions/pending/{STUB_PENDING_ID}", headers={**HEAD, "X-Demo-Fault": "tool_error"})
    assert r.status_code == 200
