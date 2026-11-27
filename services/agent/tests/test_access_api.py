"""What: tests for the access gate, visitor cookie, /api/me, /api/demo/personas, POST /api/sessions,
the Origin check and the session ownership helper (doc 07 s2.1, s2.2).
Use when: pytest (in-process ASGI, in-memory session store, fixture personas file). Inputs: none.
Output: pass or fail. Side effects: none. Role: tests for agent.access, agent.api, agent.personas."""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
import pytest
from contracts.agent_api import Me, Persona, SessionCreated
from contracts.errors import ErrorEnvelope

from agent import access
from agent.ingress import SessionRow
from agent.main import create_app
from agent.personas import load_personas
from agent.settings import Settings

KEY = "k" * 32
CODE = "open-sesame"
ORIGIN = "https://demo.example.com"
FIXTURE = Path(__file__).parent / "fixtures" / "personas.yaml"


def settings(**kw) -> Settings:
    """What: test settings with a signing key, access code and personas fixture.
    Use when: building an app. Inputs: overrides. Output: Settings. Side effects: none. Role: helper."""
    base: dict[str, Any] = dict(served_by="agent_a", agent_db_url="postgresql://x", redis_url="redis://x",
                core_url="http://core:8000", knowledge_url="http://knowledge:8000", demo_faults_enabled=True,
                site_origin=ORIGIN, session_signing_key=KEY, demo_access_code=CODE, personas_path=str(FIXTURE))
    base.update(kw)
    return Settings(**base)


class MemorySessions:
    """What: in-memory SessionStore. Use when: tests. Inputs: none. Output: create_session, get_session.
    Side effects: dict writes. Role: fake of PgSessionStore."""

    def __init__(self) -> None:
        """What: empty store. Use when: tests. Inputs: none. Output: none. Side effects: none. Role: fake."""
        self.rows: dict[str, SessionRow] = {}

    async def create_session(self, visitor_id: str, ip_hash: str | None) -> tuple[str, datetime]:
        """What: add a session. Use when: POST /api/sessions. Inputs: visitor id, ip hash.
        Output: id and time. Side effects: stores. Role: fake."""
        sid = str(uuid.uuid4())
        self.rows[sid] = SessionRow(session_id=sid, visitor_id=visitor_id)
        return sid, datetime(2026, 11, 27, 12, 0, tzinfo=timezone.utc)

    async def get_session(self, session_id: str) -> SessionRow | None:
        """What: lookup. Use when: ownership check. Inputs: id. Output: row or None. Side effects: none. Role: fake."""
        return self.rows.get(session_id)


def make_app(**kw):
    """What: app with fake probes and an in-memory session store. Use when: tests.
    Inputs: settings overrides. Output: FastAPI app. Side effects: none. Role: helper."""
    async def ok() -> str:
        """What: healthy probe. Use when: readyz. Inputs: none. Output: "ok". Side effects: none. Role: fake."""
        return "ok"
    app = create_app(settings(**kw), probes={k: ok for k in ("redis", "postgres", "core", "knowledge")},
                     session_store=MemorySessions())
    return app


def client(app, **kw) -> httpx.AsyncClient:
    """What: in-process client. Use when: tests. Inputs: app, client kwargs. Output: AsyncClient.
    Side effects: none. Role: helper."""
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://test", **kw)


async def login(c: httpx.AsyncClient, code: str = CODE) -> httpx.Response:
    """What: POST /api/access. Use when: tests need the cookie. Inputs: client, code.
    Output: response (cookie lands in the client jar). Side effects: network (in-process). Role: helper."""
    return await c.post("/api/access", json={"code": code})


# ---- cookie primitives ----

def test_cookie_roundtrip_and_payload_shape():
    tok = access.sign_cookie(KEY, now=1000.0)
    payload = access.verify_cookie(KEY, tok, now=1001.0)
    assert payload is not None and set(payload) == {"visitor_id", "iat", "exp"}
    assert payload["exp"] - payload["iat"] == 24 * 3600
    assert len(payload["visitor_id"]) == 22  # 128 bits, base64url without padding


def test_cookie_rejected_when_tampered_expired_wrong_key_or_empty_key():
    tok = access.sign_cookie(KEY, now=1000.0)
    body, sig = tok.split(".")
    assert access.verify_cookie(KEY, body + "." + sig[:-2] + "AA", now=1001.0) is None
    assert access.verify_cookie(KEY, tok, now=1000.0 + 24 * 3600 + 1) is None
    assert access.verify_cookie("other" * 8, tok, now=1001.0) is None
    assert access.verify_cookie("", tok, now=1001.0) is None
    assert access.verify_cookie(KEY, "garbage", now=1001.0) is None
    with pytest.raises(ValueError):
        access.sign_cookie("", now=1000.0)


def test_code_compare_is_exact_and_empty_expected_never_matches():
    assert access.code_matches("abc", "abc")
    assert not access.code_matches("abd", "abc")
    assert not access.code_matches("", "")
    assert not access.code_matches("x", "")


# ---- POST /api/access ----

async def test_wrong_code_gets_401_access_denied_envelope_without_cookie():
    async with client(make_app()) as c:
        r = await login(c, "nope")
    assert r.status_code == 401
    env = ErrorEnvelope.model_validate(r.json())
    assert env.error.code == "ACCESS_DENIED" and env.error.class_ == "access_denied"
    assert "qs_access" not in r.headers.get("set-cookie", "")


async def test_good_code_204_sets_signed_httponly_cookie():
    async with client(make_app()) as c:
        r = await login(c)
    assert r.status_code == 204 and r.content == b""
    cookie = r.headers["set-cookie"]
    assert cookie.startswith("qs_access=")
    low = cookie.lower()
    assert "httponly" in low and "secure" in low and "samesite=lax" in low and "path=/" in low
    assert "max-age=86400" in low
    assert access.verify_cookie(KEY, r.cookies["qs_access"], now=__import__("time").time()) is not None


async def test_cookie_not_secure_over_plain_http_site_origin():
    async with client(make_app(site_origin="http://localhost:3000")) as c:
        r = await login(c)
    assert "secure" not in r.headers["set-cookie"].lower()


async def test_access_body_validation_422_and_extra_field_rejected():
    async with client(make_app()) as c:
        assert (await c.post("/api/access", json={"code": ""})).status_code == 422
        assert (await c.post("/api/access", json={"code": "x" * 65})).status_code == 422
        assert (await c.post("/api/access", json={"code": CODE, "x": 1})).status_code == 422
        assert (await c.post("/api/access", content=b"not json")).status_code == 422


async def test_unset_access_code_or_key_never_lets_anyone_in():
    async with client(make_app(demo_access_code="")) as c:
        assert (await login(c, "anything")).status_code == 401
    async with client(make_app(session_signing_key="")) as c:
        assert (await login(c)).status_code == 500  # fails loudly, no cookie


# ---- gate: every route but access and health needs the cookie ----

def _gated_routes(app) -> list[tuple[str, str]]:
    """What: every (method, path) the app serves except the open ones, with path params filled.
    Use when: the parametrized gate test. Inputs: app. Output: list. Side effects: none. Role: helper."""
    open_paths = {"/api/access", "/api/healthz", "/api/readyz"}
    out = []
    for route in app.routes:
        path = getattr(route, "path", "")
        if not path.startswith("/api") or path in open_paths:
            continue
        for m in sorted(getattr(route, "methods", set()) - {"HEAD", "OPTIONS"}):
            out.append((m, path.replace("{session_id}", str(uuid.uuid4())).replace("{sid}", str(uuid.uuid4()))))
    return out


def test_route_listing_is_not_empty():
    routes = _gated_routes(make_app())
    assert {("GET", "/api/me"), ("GET", "/api/demo/personas"), ("POST", "/api/sessions")} <= set(routes)


@pytest.mark.parametrize("method,path", _gated_routes(make_app()))
async def test_every_gated_route_is_401_without_the_cookie(method, path):
    async with client(make_app()) as c:
        r = await c.request(method, path, json={} if method != "GET" else None)
    assert r.status_code == 401
    assert ErrorEnvelope.model_validate(r.json()).error.class_ == "access_denied"


@pytest.mark.parametrize("path", ["/api/healthz", "/api/readyz"])
async def test_health_routes_need_no_cookie(path):
    async with client(make_app()) as c:
        assert (await c.get(path)).status_code == 200


async def test_unknown_route_without_cookie_is_also_401_not_a_hint():
    async with client(make_app()) as c:
        assert (await c.get("/api/anything-else")).status_code == 401


@pytest.mark.parametrize("cookie", ["garbage", "a.b", ""])
async def test_bad_cookie_is_401(cookie):
    async with client(make_app(), cookies={"qs_access": cookie}) as c:
        assert (await c.get("/api/me")).status_code == 401


async def test_expired_cookie_is_401():
    tok = access.sign_cookie(KEY, now=1000.0)  # long expired
    async with client(make_app(), cookies={"qs_access": tok}) as c:
        assert (await c.get("/api/me")).status_code == 401


# ---- GET /api/me ----

@pytest.mark.parametrize("flag", [True, False])
async def test_me_reports_demo_faults_flag(flag):
    async with client(make_app(demo_faults_enabled=flag)) as c:
        await login(c)
        r = await c.get("/api/me")
    assert r.status_code == 200
    me = Me.model_validate(r.json())
    assert me.visitor is True and me.operator is False and me.demo_faults_enabled is flag


# ---- GET /api/demo/personas ----

async def test_real_seed_file_parses_into_the_contract_list():
    async with client(make_app()) as c:
        await login(c)
        r = await c.get("/api/demo/personas")
    assert r.status_code == 200
    raw = r.json()
    assert len(raw) == 9
    assert all(set(p) == set(Persona.model_fields) for p in raw)  # nothing beyond the contract fields
    people = [Persona.model_validate(p) for p in raw]
    assert people[0].name == "Priya Raman" and people[0].scenarios == ["S1", "S2", "S5"] and people[0].pin == "7342"
    assert people[1].scenarios == ["S3", "rules"] and people[1].role == "member"


def _write(tmp_path, text: str) -> str:
    """What: write a temporary personas file. Use when: malformed-file tests. Inputs: dir, text.
    Output: path string. Side effects: file write. Role: helper."""
    f = tmp_path / "p.yaml"
    f.write_text(text)
    return str(f)


GOOD = ("- {name: A, role: owner, tier: free, contract_status: active, account_id: QS-1, pin: '1234', "
        "scenarios: [S4], description: hi%s}\n")


async def test_unknown_keys_are_ignored_and_never_returned(tmp_path):
    path = _write(tmp_path, GOOD % ", user_id: USR-SECRET-1, pin_hash: deadbeef")
    async with client(make_app(personas_path=path)) as c:
        await login(c)
        r = await c.get("/api/demo/personas")
    assert r.status_code == 200 and len(r.json()) == 1
    assert "USR-SECRET-1" not in r.text and "deadbeef" not in r.text and set(r.json()[0]) == set(Persona.model_fields)


async def test_missing_personas_file_gives_empty_list_and_a_warning(caplog, tmp_path):
    async with client(make_app(personas_path=str(tmp_path / "nope.yaml"))) as c:
        await login(c)
        with caplog.at_level(logging.WARNING, logger="agent.personas"):
            r = await c.get("/api/demo/personas")
    assert r.status_code == 200 and r.json() == []
    assert any("personas file not found" in m for m in caplog.messages)


@pytest.mark.parametrize("text", ["- {name: A\n  : [", "just a string", "key: value", "- 5\n- [1, 2]\n"])
async def test_malformed_file_is_a_logged_error_and_an_empty_list_never_a_500(text, caplog, tmp_path):
    async with client(make_app(personas_path=_write(tmp_path, text))) as c:
        await login(c)
        with caplog.at_level(logging.ERROR, logger="agent.personas"):
            r = await c.get("/api/demo/personas")
    assert r.status_code == 200 and r.json() == []
    assert any(rec.levelno == logging.ERROR for rec in caplog.records)


async def test_one_bad_item_is_skipped_and_logged_without_its_values(caplog, tmp_path):
    bad = ("- {name: B, role: ghost, tier: free, contract_status: active, account_id: QS-2, pin: 'SECRETPIN', "
           "scenarios: [S4], description: hi}\n")
    long = "- {name: C, role: owner, tier: free, contract_status: active, account_id: QS-3, pin: '1', scenarios: [S4], description: '%s'}\n" % ("x" * 141)
    async with client(make_app(personas_path=_write(tmp_path, GOOD % "" + bad + long))) as c:
        await login(c)
        with caplog.at_level(logging.ERROR, logger="agent.personas"):
            r = await c.get("/api/demo/personas")
    assert [p["name"] for p in r.json()] == ["A"]
    assert "SECRETPIN" not in caplog.text and "role" in caplog.text and "description" in caplog.text


def test_personas_accepts_a_personas_mapping_too(tmp_path):
    assert [p.name for p in load_personas(_write(tmp_path, "personas:\n" + "  " + GOOD % ""))] == ["A"]


# ---- POST /api/sessions and ownership ----

async def test_create_session_201_binds_to_the_visitor():
    app = make_app()
    async with client(app) as c:
        await login(c)
        r = await c.post("/api/sessions", json={})
    assert r.status_code == 201
    created = SessionCreated.model_validate(r.json())
    row = app.state.session_store.rows[created.session_id]
    payload = access.verify_cookie(KEY, r.request.headers["cookie"].split("qs_access=")[1].split(";")[0],
                                   now=__import__("time").time())
    assert payload is not None and row.visitor_id == payload["visitor_id"]
    assert created.created_at.startswith("2026-11-27T12:00:00")


async def test_sessions_body_must_be_empty_object():
    async with client(make_app()) as c:
        await login(c)
        assert (await c.post("/api/sessions", json={"x": 1})).status_code == 422


async def test_owned_session_helper_returns_own_and_404s_foreign_and_unknown():
    app = make_app()
    store = app.state.session_store
    async with client(app) as a, client(app) as b:
        await login(a)
        await login(b)
        sid = (await a.post("/api/sessions", json={})).json()["session_id"]
    mine = await access.owned_session(store, sid, visitor_id=store.rows[sid].visitor_id)
    assert mine.session_id == sid
    for visitor, session in (("someone-else", sid), (store.rows[sid].visitor_id, str(uuid.uuid4()))):
        with pytest.raises(access.NotOwned):
            await access.owned_session(store, session, visitor_id=visitor)


# ---- Origin check ----

@pytest.mark.parametrize("path,body", [("/api/access", {"code": CODE}), ("/api/sessions", {})])
async def test_state_changing_route_with_foreign_origin_is_403_origin_mismatch(path, body):
    async with client(make_app()) as c:
        await login(c)
        r = await c.post(path, json=body, headers={"Origin": "https://evil.example"})
    assert r.status_code == 403
    env = ErrorEnvelope.model_validate(r.json())
    assert env.error.code == "ORIGIN_MISMATCH"


async def test_matching_or_absent_origin_passes_and_gets_ignore_origin():
    async with client(make_app()) as c:
        assert (await c.post("/api/access", json={"code": CODE}, headers={"Origin": ORIGIN})).status_code == 204
        assert (await c.post("/api/sessions", json={})).status_code == 201  # no Origin header
        assert (await c.get("/api/me", headers={"Origin": "https://evil.example"})).status_code == 200


async def test_origin_check_runs_before_the_cookie_check():
    async with client(make_app()) as c:
        r = await c.post("/api/sessions", json={}, headers={"Origin": "https://evil.example"})
    assert r.status_code == 403


async def test_not_owned_is_a_404_not_found_envelope_by_default():
    app = make_app()

    @app.get("/api/boom-owned")
    async def boom_owned() -> None:
        """What: route that forgets to catch NotOwned. Use when: this test. Inputs: none.
        Output: raises. Side effects: none. Role: probe."""
        raise access.NotOwned("x")

    async with client(app) as c:
        await login(c)
        r = await c.get("/api/boom-owned")
    assert r.status_code == 404 and ErrorEnvelope.model_validate(r.json()).error.code == "NOT_FOUND"


def test_pg_store_mints_uuid7_session_ids():
    import inspect
    from agent import sessions
    assert "uuid7" in inspect.getsource(sessions.PgSessionStore.create_session)


def test_empty_key_or_code_is_reported_at_startup(caplog):
    with caplog.at_level(logging.ERROR, logger="agent.main"):
        make_app(session_signing_key="", demo_access_code="")
    assert "SESSION_SIGNING_KEY" in caplog.text and "DEMO_ACCESS_CODE" in caplog.text
