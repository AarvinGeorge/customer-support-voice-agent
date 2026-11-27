"""What: every slice route of doc 07 s3 gives 403 SCOPE_DENIED to a caller whose scopes lack the route's scope.
Use when: adding or changing a core route or a token's scopes. Inputs: none.
Output: pytest results. Side effects: none.
Role: CS-E-03.S13.T1 Done-when. The tables below are transcribed from doc 07 s1.4 and s3 on purpose (not
imported from core), so a wrong scope in the code fails here."""
import pytest

from core.auth import ROUTE_SCOPES, caller_scopes

SID = "0197b5a4-7c3e-7f00-8000-000000000001"  # a uuid7 session id
# doc 07 s1.4: caller name -> scopes.
SCOPES = {
    "agent_tools": {"session.act_as", "identity.verify", "claims.read", "me.read", "billing.read",
                    "context.read", "actions.prepare", "actions.status", "prefs.write",
                    "tickets.create", "tickets.read_own"},
    "agent_commit": {"session.act_as", "identity.step_up", "actions.status", "actions.commit",
                     "actions.submit", "actions.cancel"},
    "ops": {"operator.login", "approvals.read", "approvals.decide", "tickets.read_all",
            "tickets.resolve", "tickets.close"},
}
# doc 07 s3 scope column, only the routes the thin slice builds (doc 09 s1.2 lists the rest as designed only).
SLICE_ROUTES = [
    ("POST", "/v1/identity/verify", "identity.verify"),
    ("GET", f"/v1/sessions/{SID}/claims", "claims.read"),
    ("GET", "/v1/me/context", "context.read"),
    ("GET", "/v1/me/charges", "billing.read"),
    ("GET", "/v1/me/tickets", "tickets.read_own"),
    ("POST", "/v1/actions/refund/prepare", "actions.prepare"),
    ("POST", "/v1/actions/grace_extension/prepare", "actions.prepare"),
    ("GET", "/v1/actions/pending/PA-0000000A", "actions.status"),
    ("POST", "/v1/actions/pending/PA-0000000A/commit", "actions.commit"),
    ("POST", "/v1/actions/pending/PA-0000000A/submit", "actions.submit"),
    ("POST", "/v1/actions/pending/PA-0000000A/cancel", "actions.cancel"),
    ("POST", "/v1/tickets", "tickets.create"),
]
TOKEN_OF = {"agent_tools": "tok-agent", "agent_commit": "tok-commit", "ops": "tok-ops"}
# routes whose handler exists, with the status the allowed caller gets here (no session header, no database, empty body):
# POST routes reject the empty body (422); GET claims reaches the missing database (503); the /me reads need X-Session-Id (422).
BUILT = {("POST", "/v1/tickets"): 422, ("POST", "/v1/identity/verify"): 422, ("GET", f"/v1/sessions/{SID}/claims"): 503,
         ("GET", "/v1/me/context"): 422, ("GET", "/v1/me/charges"): 422, ("GET", "/v1/me/tickets"): 422,  # S3.T1, S1.T1
         # S2.T1, T5: prepare rejects the empty body, grace is disabled, the pending routes need X-Session-Id
         ("POST", "/v1/actions/refund/prepare"): 422, ("POST", "/v1/actions/grace_extension/prepare"): 404,
         ("GET", "/v1/actions/pending/PA-0000000A"): 422, ("POST", "/v1/actions/pending/PA-0000000A/commit"): 422,
         ("POST", "/v1/actions/pending/PA-0000000A/submit"): 422, ("POST", "/v1/actions/pending/PA-0000000A/cancel"): 422}
CASES = [(m, p, s, who) for (m, p, s) in SLICE_ROUTES for who in SCOPES]


def test_code_scope_map_equals_doc_07(client):
    """What: the code's token to scope map is exactly doc 07 s1.4. Use when: changing auth.
    Inputs: client. Output: assertions. Side effects: none. Role: guards the three-token least privilege."""
    for who, scopes in SCOPES.items():
        assert caller_scopes(who) == frozenset(scopes), who


def test_registered_routes_are_exactly_the_slice_table(client):
    """What: the app serves the s3 slice routes and no other /v1 route, each with the s3 scope.
    Use when: adding a route. Inputs: client. Output: assertions. Side effects: none.
    Role: stops an unscoped or designed-only route from slipping in."""
    registered = {(m, p): s for (m, p), s in ROUTE_SCOPES.items()}
    expected = {}
    for m, p, s in SLICE_ROUTES:
        template = (p.replace(SID, "{session_id}").replace("PA-0000000A", "{pending_id}"))
        expected[(m, template)] = s
    assert registered == expected
    served = {(m, r.path) for r in client.app.routes if r.path.startswith("/v1") for m in r.methods - {"HEAD"}}
    assert served == set(expected)


@pytest.mark.parametrize("method,path,scope,who", CASES)
def test_scope_gate(client, method, path, scope, who):
    """What: 403 SCOPE_DENIED when the caller lacks the scope, else the stub's 501 (handler not built yet).
    Use when: always. Inputs: one route and one caller. Output: assertions. Side effects: none.
    Role: the Done-when matrix (12 routes x 3 callers = 36 cases)."""
    headers = {"Authorization": f"Bearer {TOKEN_OF[who]}"}
    r = client.request(method, path, headers=headers, json={} if method == "POST" else None)
    if scope in SCOPES[who]:
        # built routes answer per BUILT (proves the scope gate passed); stubs answer 501
        assert r.status_code == BUILT.get((method, path), 501), (r.status_code, r.text)
    else:
        assert r.status_code == 403, (r.status_code, r.text)
        body = r.json()["error"]
        assert body["code"] == "SCOPE_DENIED" and body["class"] == "access_denied"
        assert body["retryable"] is False and body["changed"] is False


def test_matrix_has_both_outcomes():
    """What: the matrix contains denied and allowed cases. Use when: always. Inputs: none.
    Output: assertion. Side effects: none. Role: a gate that can only pass or only fail tests nothing."""
    denied = sum(1 for (_, _, s, who) in CASES if s not in SCOPES[who])
    allowed = len(CASES) - denied
    assert denied >= 20 and allowed == 13  # 12 routes, actions.status is held by two callers
