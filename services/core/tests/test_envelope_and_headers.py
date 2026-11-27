"""What: auth failures, error envelope shape, header handling, request ids, validation and 404 shape.
Use when: changing core/auth.py, core/errors.py or the header dependency. Inputs: none.
Output: pytest results. Side effects: none. Role: CS-E-03.S13.T1 (doc 07 s1.2 to s1.4)."""
import uuid

import pytest

from contracts.errors import CORE_ERROR_CODES, ErrorEnvelope

ROUTE = "/v1/me/context"
AGENT = {"Authorization": "Bearer tok-agent"}


def test_missing_token_is_401_auth_invalid(client):
    """What: no Authorization header gives 401 AUTH_INVALID in the contract envelope.
    Use when: always. Inputs: client. Output: assertions. Side effects: none. Role: s1.4."""
    r = client.get(ROUTE)
    assert r.status_code == 401
    env = ErrorEnvelope.model_validate(r.json())
    assert env.error.code == "AUTH_INVALID" and env.error.class_ == "access_denied"
    assert env.error.fault_injected is False and env.error.retryable is False


def test_wrong_token_and_wrong_scheme_are_401(client):
    """What: an unknown token and a non-Bearer scheme are both 401. Use when: always.
    Inputs: client. Output: assertions. Side effects: none. Role: constant-time compare path."""
    assert client.get(ROUTE, headers={"Authorization": "Bearer nope"}).status_code == 401
    assert client.get(ROUTE, headers={"Authorization": "Basic tok-agent"}).status_code == 401
    assert client.get(ROUTE, headers={"Authorization": "Bearer "}).status_code == 401


def test_error_table_matches_contract(client):
    """What: the HTTP status, retryable and changed of AUTH_INVALID and SCOPE_DENIED equal CORE_ERROR_CODES.
    Use when: always. Inputs: client. Output: assertions. Side effects: none. Role: one source of error facts."""
    r = client.get(ROUTE)
    status, retryable, changed = CORE_ERROR_CODES["AUTH_INVALID"]
    assert (r.status_code, r.json()["error"]["retryable"], r.json()["error"]["changed"]) == (status, retryable, changed)


def test_request_and_trace_ids_are_echoed_or_minted(client):
    """What: X-Request-Id and X-Trace-Id are echoed when sent and minted (uuid) when absent, on success and error.
    Use when: always. Inputs: client. Output: assertions. Side effects: none. Role: s1.2 log correlation."""
    rid, tid = str(uuid.uuid4()), "0197b5a4-7c3e-7f00-8000-000000000001"
    r = client.get("/healthz", headers={"X-Request-Id": rid, "X-Trace-Id": tid})
    assert r.headers["X-Request-Id"] == rid and r.headers["X-Trace-Id"] == tid
    r = client.get(ROUTE)  # error path, nothing sent
    uuid.UUID(r.headers["X-Request-Id"])
    minted = uuid.UUID(r.headers["X-Trace-Id"])
    assert minted.version == 7
    assert r.json()["error"]["trace_id"] == r.headers["X-Trace-Id"]


def test_session_header_only_from_act_as_tokens(client):
    """What: X-Session-Id is refused with 403 SCOPE_DENIED from the ops token (no session.act_as).
    Use when: always. Inputs: client. Output: assertions. Side effects: none. Role: s1.2 X-Session-Id row."""
    r = client.get("/healthz", headers={"Authorization": "Bearer tok-ops", "X-Session-Id": "s"})
    # health is unauthenticated and ignores the header; a protected route enforces it
    assert r.status_code == 200
    r = client.get(ROUTE, headers={"Authorization": "Bearer tok-ops", "X-Session-Id": "s"})
    assert r.status_code == 403 and r.json()["error"]["code"] == "SCOPE_DENIED"


def test_bad_mode_is_422_validation(client):
    """What: X-Mode other than chat or voice is 422 VALIDATION_ERROR. Use when: always.
    Inputs: client. Output: assertions. Side effects: none. Role: s1.2 X-Mode row."""
    r = client.get(ROUTE, headers={**AGENT, "X-Mode": "fax"})
    assert r.status_code == 422 and r.json()["error"]["code"] == "VALIDATION_ERROR"
    assert r.json()["error"]["class"] == "validation"


def test_unknown_route_is_not_found_envelope(client):
    """What: an unknown path returns the NOT_FOUND envelope, not FastAPI's default body. Use when: always.
    Inputs: client. Output: assertions. Side effects: none. Role: s1.3 (every non-2xx is an envelope)."""
    r = client.get("/v1/nope", headers=AGENT)
    assert r.status_code == 404 and r.json()["error"]["code"] == "NOT_FOUND"
    assert r.json()["error"]["class"] == "not_found"


def test_validation_error_is_envelope_and_never_echoes_input(client):
    """What: a request-validation failure is the VALIDATION_ERROR envelope and the offending input (which could
    be a PIN) is not echoed back. Use when: always. Inputs: client. Output: assertions. Side effects: adds a
    throwaway route to the test app. Role: s1.3 plus s11 (a secret must not leave through an error body)."""
    from pydantic import BaseModel

    class Body(BaseModel):
        pin: int

    def probe(body: Body) -> dict:
        """What: throwaway probe route. Use when: this test. Inputs: body. Output: dict. Side effects: none. Role: test."""
        return {}

    client.app.add_api_route("/__probe", probe, methods=["POST"])
    r = client.post("/__probe", json={"pin": "4821 secret"}, headers=AGENT)
    assert r.status_code == 422 and r.json()["error"]["code"] == "VALIDATION_ERROR"
    assert "4821" not in r.text
    assert r.json()["error"]["details"]["errors"][0]["loc"] == ["body", "pin"]


SESSION = "0197b5a4-7c3e-7f00-8000-0000000000aa"


def test_valid_uuid_session_header_reaches_the_handler(client):
    """What: a uuid X-Session-Id is accepted and exposed (lower case) on the context; no header leaves it None.
    Use when: always. Inputs: client. Output: assertions. Side effects: adds a probe route.
    Role: review PR19 I2, the accepted side of the rule."""
    from fastapi import Request

    from fastapi import Depends

    from core.auth import get_ctx, scoped

    def probe(request: Request) -> dict:
        """What: echo the parsed session id. Use when: this test. Inputs: request. Output: dict.
        Side effects: none. Role: probe."""
        return {"session_id": get_ctx(request).session_id}

    client.app.add_api_route("/__sess", probe, methods=["GET"], dependencies=[Depends(scoped("context.read"))])  # not in ROUTE_SCOPES
    assert client.get("/__sess", headers={**AGENT, "X-Session-Id": SESSION.upper()}).json() == {"session_id": SESSION}
    assert client.get("/__sess", headers=AGENT).json() == {"session_id": None}


@pytest.mark.parametrize("bad", ["S-1", "a b;c", "not-a-uuid", "0197b5a4-7c3e-7f00-8000-00000000zzzz", "' OR 1=1 --", "x" * 200])
def test_non_uuid_session_header_is_422_never_500_never_dropped(client, bad):
    """What: any X-Session-Id that is not a uuid gives 422 VALIDATION_ERROR naming the header, on a route with the
    right scope. Use when: always. Inputs: client, a bad value. Output: assertions. Side effects: none.
    Role: review PR19 I2 (was: 500 INTERNAL retryable, or the header silently dropped)."""
    r = client.get(ROUTE, headers={**AGENT, "X-Session-Id": bad})
    assert r.status_code == 422, r.text
    err = r.json()["error"]
    assert (err["code"], err["class"], err["retryable"]) == ("VALIDATION_ERROR", "validation", False)
    assert err["details"]["errors"][0]["loc"] == ["header", "x-session-id"]
    assert bad not in r.text  # input is not echoed


def test_non_uuid_session_path_parameter_is_422(client):
    """What: GET /v1/sessions/{session_id}/claims with a non-uuid id is 422 (not 503, not 500); a uuid passes the
    gate. Use when: always. Inputs: client. Output: assertions. Side effects: none. Role: review PR19 I2, path side."""
    r = client.get("/v1/sessions/S-1/claims", headers=AGENT)
    assert r.status_code == 422 and r.json()["error"]["details"]["errors"][0]["loc"] == ["path", "session_id"]
    assert client.get(f"/v1/sessions/{SESSION}/claims", headers=AGENT).status_code == 503


def test_scope_is_still_checked_before_the_session_header(client):
    """What: a caller without the scope gets 403 even when the session header is bad. Use when: always.
    Inputs: client. Output: assertion. Side effects: none. Role: the documented order token, scope, headers."""
    r = client.get(ROUTE, headers={"Authorization": "Bearer tok-ops", "X-Session-Id": "S-1"})
    assert r.status_code == 403


def test_require_session_is_422_when_the_header_is_missing(client):
    """What: RequestContext.require_session() returns the id, or raises VALIDATION_ERROR when no header was sent.
    Use when: always. Inputs: client. Output: assertions. Side effects: none.
    Role: review PR19 I2 'decide the missing-header case': the dependency allows it, session handlers must call this."""
    import pytest as _pt

    from core.auth import RequestContext
    from core.errors import CoreError

    def ctx(session_id: str | None) -> RequestContext:
        """What: build a bare context. Use when: this test. Inputs: session id or None. Output: RequestContext.
        Side effects: none. Role: helper."""
        return RequestContext(caller="agent_tools", scopes=frozenset(), request_id="r", trace_id="t",
                              session_id=session_id, mode=None, turn_id=None, idempotency_key=None, demo_fault=None)

    assert ctx(SESSION).require_session() == SESSION
    with _pt.raises(CoreError) as exc:
        ctx(None).require_session()
    assert exc.value.code == "VALIDATION_ERROR"
