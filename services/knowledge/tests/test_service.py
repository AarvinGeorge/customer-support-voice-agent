"""What: tests for the knowledge skeleton (CS-E-02.S1.T1): auth, scopes, envelope, trace ids, health.
Use when: CI. Inputs: the `client` fixture. Output: pass or fail. Side effects: none.
Role: proves the Done-when 'wrong token gets 401 UNAUTHORIZED in the envelope' and doc 07 s1.2 to s1.4."""
import re

from contracts.errors import ErrorEnvelope

from tests.conftest import TOKEN

PROTECTED = "/v1/corpus"


def _envelope(resp):
    """What: parse a response body as the contract envelope. Use when: asserting errors.
    Inputs: response. Output: ErrorEnvelope. Side effects: none. Role: validates against contracts."""
    return ErrorEnvelope.model_validate(resp.json())


def test_wrong_token_is_401_unauthorized_in_envelope(client):
    """What: a wrong bearer token gets 401 UNAUTHORIZED in the envelope. Use when: CI.
    Inputs: bad token. Output: assertion. Side effects: none. Role: Done-when item."""
    r = client.get(PROTECTED, headers={"Authorization": "Bearer nope"})
    assert r.status_code == 401
    err = _envelope(r).error
    assert (err.code, err.class_, err.retryable, err.changed) == ("UNAUTHORIZED", "access_denied", False, False)
    assert err.trace_id == r.headers["X-Trace-Id"]
    assert r.headers["WWW-Authenticate"] == "Bearer"


def test_missing_and_malformed_auth_are_401(client):
    """What: no header, other scheme and empty token all give 401. Use when: CI. Inputs: three requests.
    Output: assertion. Side effects: none. Role: zero-proof for the auth check."""
    for headers in ({}, {"Authorization": "Basic abc"}, {"Authorization": "Bearer "}):
        r = client.get(PROTECTED, headers=headers)
        assert r.status_code == 401, headers
        assert _envelope(r).error.code == "UNAUTHORIZED"


def test_right_token_passes(client):
    """What: the agent token reaches the route. Use when: CI. Inputs: good token.
    Output: assertion. Side effects: none. Role: proves the 401 above is not a blanket failure."""
    r = client.get(PROTECTED, headers={"Authorization": f"Bearer {TOKEN}"})
    assert r.status_code == 200
    assert r.json()["articles"] == []


def test_scope_missing_is_401_not_403(client, monkeypatch):
    """What: a token whose caller lacks the route scope is refused. Use when: CI. Inputs: scope map
    without corpus.read. Output: assertion. Side effects: patches the scope map. Role: scope check proof."""
    from knowledge import auth
    monkeypatch.setitem(auth.CALLERS, "agent", ("KNOWLEDGE_TOKEN_AGENT", frozenset({"search"})))
    r = client.get(PROTECTED, headers={"Authorization": f"Bearer {TOKEN}"})
    assert r.status_code == 401 and _envelope(r).error.code == "UNAUTHORIZED"


def test_health_needs_no_token(client):
    """What: /healthz and /readyz are open. Use when: CI. Inputs: none. Output: assertion.
    Side effects: none. Role: Compose healthcheck target (doc 07 s4.2)."""
    assert client.get("/healthz").json() == {"status": "alive"}
    r = client.get("/readyz")
    assert r.status_code == 200 and r.json()["status"] == "ready" and r.json()["database"] == "ok"


def test_readyz_503_when_db_down(client, monkeypatch):
    """What: readyz is 503 in the envelope when the database does not answer. Use when: CI.
    Inputs: a failing ping. Output: assertion. Side effects: patches the fake. Role: failure path."""
    client.app.state.db.ping = lambda: False
    r = client.get("/readyz")
    assert r.status_code == 503
    err = _envelope(r).error
    assert (err.code, err.class_, err.retryable) == ("RETRIEVAL_UNAVAILABLE", "db_unavailable", True)


def test_trace_ids_minted_and_echoed(client):
    """What: X-Trace-Id (uuid7) and X-Request-Id are minted, or echoed when supplied. Use when: CI.
    Inputs: requests with and without headers. Output: assertion. Side effects: none. Role: doc 07 s1.2."""
    r = client.get("/healthz")
    assert re.fullmatch(r"[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}", r.headers["X-Trace-Id"])
    assert r.headers["X-Request-Id"]
    r = client.get("/healthz", headers={"X-Trace-Id": "0192f1c8-1111-7222-8333-444455556666", "X-Request-Id": "rid-1"})
    assert r.headers["X-Trace-Id"] == "0192f1c8-1111-7222-8333-444455556666"
    assert r.headers["X-Request-Id"] == "rid-1"


def test_unknown_path_and_bad_method_use_envelope(client):
    """What: 404 and 405 come back in the envelope. Use when: CI. Inputs: bad path, bad method.
    Output: assertion. Side effects: none. Role: 'every non-2xx response' rule, review PR4 finding."""
    r = client.get("/nope")
    assert r.status_code == 404 and _envelope(r).error.code == "NOT_FOUND"
    r = client.post("/healthz")
    assert r.status_code == 405 and _envelope(r).error.class_ == "validation"


def test_unhandled_error_uses_envelope(client):
    """What: an unexpected exception becomes a 500 envelope with the trace id. Use when: CI.
    Inputs: a route that raises. Output: assertion. Side effects: adds a route. Role: no raw 500s."""
    @client.app.get("/boom")
    def boom():
        raise RuntimeError("secret detail")
    r = client.get("/boom")
    assert r.status_code == 500
    err = _envelope(r).error
    assert err.code == "INTERNAL" and "secret detail" not in r.text
    assert err.trace_id == r.headers["X-Trace-Id"]


def test_startup_fails_without_token(monkeypatch):
    """What: the app refuses to start with an empty token. Use when: CI. Inputs: unset env.
    Output: assertion. Side effects: env. Role: fail loudly instead of an open service."""
    import pytest
    from fastapi.testclient import TestClient

    from knowledge.main import create_app
    monkeypatch.delenv("KNOWLEDGE_TOKEN_AGENT", raising=False)
    with pytest.raises(RuntimeError, match="KNOWLEDGE_TOKEN_AGENT"):
        with TestClient(create_app()):
            pass
