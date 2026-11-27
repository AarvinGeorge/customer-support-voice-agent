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


def _verify(tc, session="sess-1"):
    """What: verify a session on the stub. Use when: tests that need a verified session.
    Inputs: client, session id. Output: none (asserts 200). Side effects: stub state. Role: helper."""
    r = tc.post("/v1/identity/verify", json={"account_id": "QS-10423", "pin": "4821"},
                headers={**HEAD, "X-Session-Id": session})
    assert r.status_code == 200, r.text


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
    _verify(tc)
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
    _verify(tc)
    r = tc.get(f"/v1/actions/pending/{STUB_PENDING_ID}", headers={**HEAD, "X-Demo-Fault": "tool_error"})
    assert r.status_code == 200


def test_unverified_until_verify(client):
    """What: a new session is unverified (claims verified false, me/context 401
    SESSION_NOT_VERIFIED) until a successful verify for that session (review PR4 I3).
    Use when: CI. Inputs: none. Output: assertion. Side effects: none. Role: V0 path support."""
    tc, _ = client
    r = tc.get("/v1/sessions/brand-new/claims", headers=HEAD)
    assert r.status_code == 200
    c.UnverifiedClaims.model_validate(r.json())
    for path in ("/v1/me/context", "/v1/me/profile", "/v1/me/charges"):
        r = tc.get(path, headers={**HEAD, "X-Session-Id": "brand-new"})
        assert r.status_code == 401, path
        assert ErrorEnvelope.model_validate(r.json()).error.code == "SESSION_NOT_VERIFIED"
    bad = tc.post("/v1/identity/verify", json={"account_id": "QS-10423", "pin": "0000"},
                  headers={**HEAD, "X-Session-Id": "brand-new"})
    assert bad.status_code == 401
    assert tc.get("/v1/sessions/brand-new/claims", headers=HEAD).json()["failed_verifies"] == 1
    _verify(tc, "brand-new")
    r = tc.get("/v1/sessions/brand-new/claims", headers=HEAD)
    assert c.Claims.model_validate(r.json()).session_id == "brand-new"
    assert tc.get("/v1/me/context", headers={**HEAD, "X-Session-Id": "brand-new"}).status_code == 200
    other = tc.get("/v1/me/context", headers={**HEAD, "X-Session-Id": "someone-else"})
    assert other.status_code == 401


def test_third_failure_locks(client):
    """What: the third failed verify in a session gives 423 VERIFY_LOCKED. Use when: CI.
    Inputs: none. Output: assertion. Side effects: none. Role: lockout path (doc 07 s3.1)."""
    tc, _ = client
    codes = [tc.post("/v1/identity/verify", json={"account_id": "QS-10423", "pin": "0000"},
                     headers=HEAD).status_code for _ in range(3)]
    assert codes == [401, 401, 423]
    claims = tc.get("/v1/sessions/sess-1/claims", headers=HEAD).json()
    assert claims["verify_locked"] is True and claims["attempts_left"] == 0


def test_commit_replays_and_status_follows(client):
    """What: a second commit returns replayed true and the status read says committed.
    Use when: CI. Inputs: none. Output: assertion. Side effects: none. Role: s7.3 status-check path."""
    tc, _ = client
    _verify(tc)
    first = tc.post(f"/v1/actions/pending/{STUB_PENDING_ID}/commit", json={}, headers=HEAD).json()
    second = tc.post(f"/v1/actions/pending/{STUB_PENDING_ID}/commit", json={}, headers=HEAD).json()
    assert (first["replayed"], second["replayed"]) == (False, True)
    st = tc.get(f"/v1/actions/pending/{STUB_PENDING_ID}", headers=HEAD).json()
    assert st["state"] == "committed" and st["result"]["status"] == "committed"


def test_unknown_path_and_bad_query_use_envelope(client):
    """What: 404, 405 and out-of-range query values come back in the error envelope with the
    caller's trace id (review PR4 Minor 5). Use when: CI. Inputs: none. Output: assertion.
    Side effects: none. Role: "every non-2xx" rule."""
    tc, _ = client
    _verify(tc)
    r = tc.get("/v1/nope", headers=HEAD)
    assert r.status_code == 404 and ErrorEnvelope.model_validate(r.json()).error.code == "NOT_FOUND"
    r = tc.delete("/v1/me/profile", headers=HEAD)
    assert r.status_code == 405 and ErrorEnvelope.model_validate(r.json()).error.trace_id == "trace-1"
    r = tc.get("/v1/me/charges?since_days=9999", headers=HEAD)
    err = ErrorEnvelope.model_validate(r.json()).error
    assert r.status_code == 422 and err.code == "VALIDATION_ERROR" and err.trace_id == "trace-1"


PREPARE_BODY = {"charge_ref": {"ending": "0413", "on": "2026-11-21"}, "reason": "duplicate", "note": "charged twice"}


def test_prepare_opens_the_pending_that_me_context_reports_until_commit(client):
    """What: after a prepare, me/context carries the open pending (PendingPublic card fields); a commit in the same
    turn is 409 CONFIRMATION_REQUIRED; a commit in a later turn succeeds and the pending leaves me/context.
    Use when: CI. Inputs: client. Output: assertion. Side effects: none. Role: doc 07 s3.1 MeContext.pending_action,
    s3.3 commit "X-Turn-Id differs from prepare_turn_id"."""
    tc, _ = client
    _verify(tc)
    assert tc.get("/v1/me/context", headers=HEAD).json()["pending_action"] is None
    tc.post("/v1/actions/refund/prepare", json=PREPARE_BODY, headers={**HEAD, "X-Turn-Id": "turn-1",
                                                                      "Idempotency-Key": "k1"})
    pending = c.MeContext.model_validate(tc.get("/v1/me/context", headers=HEAD).json()).pending_action
    assert pending is not None and pending.pending_action_id == STUB_PENDING_ID and pending.state == "prepared"
    same = tc.post(f"/v1/actions/pending/{STUB_PENDING_ID}/commit", json={}, headers={**HEAD, "X-Turn-Id": "turn-1"})
    assert same.status_code == 409 and same.json()["error"]["code"] == "CONFIRMATION_REQUIRED"
    later = tc.post(f"/v1/actions/pending/{STUB_PENDING_ID}/commit", json={}, headers={**HEAD, "X-Turn-Id": "turn-2"})
    assert later.status_code == 200
    assert tc.get("/v1/me/context", headers=HEAD).json()["pending_action"] is None


def test_cancel_closes_the_pending_and_a_second_cancel_is_not_active(client):
    """What: cancel returns changed false and removes the pending from me/context; a second cancel is 409
    PENDING_NOT_ACTIVE. Use when: CI. Inputs: client. Output: assertion. Side effects: none. Role: s3.3 cancel."""
    tc, _ = client
    _verify(tc)
    tc.post("/v1/actions/refund/prepare", json=PREPARE_BODY, headers={**HEAD, "X-Turn-Id": "turn-1",
                                                                      "Idempotency-Key": "k1"})
    first = tc.post(f"/v1/actions/pending/{STUB_PENDING_ID}/cancel", json={}, headers={**HEAD, "X-Turn-Id": "turn-2"})
    assert first.status_code == 200 and first.json()["changed"] is False
    assert tc.get("/v1/me/context", headers=HEAD).json()["pending_action"] is None
    again = tc.post(f"/v1/actions/pending/{STUB_PENDING_ID}/cancel", json={}, headers={**HEAD, "X-Turn-Id": "turn-3"})
    assert again.status_code == 409 and again.json()["error"]["code"] == "PENDING_NOT_ACTIVE"
