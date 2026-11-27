"""What: checks that every (method, path) an agent HTTP client can call is served by the target service, and that the
caller's token holds the scope doc 07 names for that route.
Use when: CI (repo tests job: uv run --no-project --with pytest pytest tests).
Inputs: source of services/agent/src/agent/clients/{core_agent,knowledge}.py, structured/confirm.py (the callers), of
services/core/src/core/{routes,auth}.py (core's route table and token scopes) and of services/knowledge/src/knowledge/{main,auth}.py.
Output: pass, or a failure naming the caller file, the method, the path and what is missing.
Side effects: none (reads source with ast, imports no service code, so it needs no service dependencies).
Role: root cause of the get_account_summary 404 (GET /v1/me/profile was designed only): each service passed against
its own fakes and nothing compared the two sides. This is that comparison."""
from __future__ import annotations

import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
AGENT = ROOT / "services" / "agent" / "src" / "agent"
CORE = ROOT / "services" / "core" / "src" / "core"
KNOW = ROOT / "services" / "knowledge" / "src" / "knowledge"

# (client source file, target service, the caller name in the target's token table)
CLIENTS = [(AGENT / "clients" / "core_agent.py", "core", "agent_tools"),
           (AGENT / "structured" / "confirm.py", "core", "agent_commit"),
           (AGENT / "clients" / "knowledge.py", "knowledge", "agent")]
HTTP_CALLS = {"call_json", "call_json_within"}


def norm(path: str) -> str:
    """What: replace every {param} in a path with {}. Use when: comparing a client path with a route template.
    Inputs: path. Output: path. Side effects: none. Role: parameter names differ between client and route."""
    return re.sub(r"\{[^}]*\}", "{}", path)


def _call_name(node: ast.AST) -> str | None:
    """What: the helper name of a call to call_json or call_json_within, else None. Use when: scanning a module.
    Inputs: any node. Output: name or None. Side effects: none. Role: one definition of "an HTTP call site"."""
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in HTTP_CALLS:
        return node.func.id
    return None


def client_calls(source: Path) -> set[tuple[str, str]]:
    """What: every (METHOD, path) a client file passes to call_json or call_json_within. The method must be a string
    literal and the url an f-string starting with self._base, inline or held in a variable assigned once in the file;
    any call site that cannot be resolved that way raises AssertionError ("cannot resolve") instead of being skipped.
    Use when: building the caller side. Inputs: client source path. Output: set of pairs.
    Side effects: reads the file. Role: the agent's side of the contract."""
    tree = ast.parse(source.read_text())
    names: dict[str, list[ast.expr]] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name):
                    names.setdefault(t.id, []).append(node.value)
    out: set[tuple[str, str]] = set()
    for node in ast.walk(tree):
        helper = _call_name(node)
        if helper is None:
            continue
        assert isinstance(node, ast.Call)
        first = 1 if helper == "call_json" else 2  # (http, method, url, ...) or (timeout, http, method, url, ...)
        where = f"{source.name}:{node.lineno}"
        if len(node.args) < first + 2:
            raise AssertionError(f"cannot resolve the call at {where}: method and url must be positional")
        method, url = node.args[first], node.args[first + 1]
        if isinstance(url, ast.Name) and len(names.get(url.id, [])) == 1:
            url = names[url.id][0]
        if not (isinstance(method, ast.Constant) and isinstance(method.value, str)):
            raise AssertionError(f"cannot resolve the method of the call at {where}: use a string literal")
        first_part = url.values[0] if isinstance(url, ast.JoinedStr) and url.values else None
        if not (isinstance(first_part, ast.FormattedValue) and isinstance(first_part.value, ast.Attribute)
                and first_part.value.attr == "_base"):
            raise AssertionError(f"cannot resolve the url of the call at {where}: use an f-string starting with {{self._base}}")
        parts = [v.value if isinstance(v, ast.Constant) else "{}" for v in url.values][1:]  # type: ignore[union-attr]
        out.add((method.value.upper(), norm("".join(parts))))
    return out


def discover_clients(root: Path) -> list[Path]:
    """What: every module under root that calls call_json or call_json_within (the module defining them is skipped).
    Use when: checking that no client file is missing from CLIENTS. Inputs: a source root. Output: sorted paths.
    Side effects: reads files. Role: replaces a hand-written file list."""
    found = []
    for f in sorted(root.rglob("*.py")):
        tree = ast.parse(f.read_text())
        defines = any(isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name in HTTP_CALLS for n in ast.walk(tree))
        if not defines and any(_call_name(n) for n in ast.walk(tree)):
            found.append(f)
    return found


def uncovered(root: Path, covered: list[Path]) -> list[Path]:
    """What: modules that call the shared http helper but are not in the covered list. Use when: the coverage test.
    Inputs: source root, covered paths. Output: the missing paths. Side effects: reads files.
    Role: a new client that calls core or knowledge must be given a target service and caller before CI passes."""
    have = {p.resolve() for p in covered}
    return [p for p in discover_clients(root) if p.resolve() not in have]


def _assigned(tree: ast.Module, name: str) -> ast.expr:
    """What: the value assigned to a module-level name. Use when: reading a table out of source. Inputs: tree, name.
    Output: expression node (AssertionError if absent). Side effects: none. Role: helper."""
    for node in tree.body:
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.target.id == name and node.value:
            return node.value
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == name for t in node.targets):
            return node.value
    raise AssertionError(f"{name} not found")


def _scopes(call: ast.expr) -> set[str]:
    """What: the strings of a frozenset({...}) literal. Use when: reading a caller's scope set. Inputs: expression.
    Output: set. Side effects: none. Role: helper."""
    assert isinstance(call, ast.Call) and call.args, ast.dump(call)
    return set(ast.literal_eval(call.args[0]))


def core_table(core_dir: Path = CORE) -> tuple[dict[tuple[str, str], str], dict[str, set[str]]]:
    """What: core's route table ((METHOD, path) to scope) and its caller scope sets.
    Use when: building the server side. Inputs: none. Output: (routes, scopes by caller). Side effects: reads files.
    Role: core's side of the contract (SLICE_ROUTES in routes.py, CALLER_SCOPES in auth.py). A listed route whose name
    has no handler in register_routes' `built` map answers 501 and is NOT counted as served."""
    tree = ast.parse((core_dir / "routes.py").read_text())
    built: set[str] = set()
    for n in ast.walk(tree):
        value = n.value if isinstance(n, (ast.Assign, ast.AnnAssign)) else None
        targets = n.targets if isinstance(n, ast.Assign) else [n.target] if isinstance(n, ast.AnnAssign) else []
        if isinstance(value, ast.Dict) and any(isinstance(t, ast.Name) and t.id == "built" for t in targets):
            built |= {ast.literal_eval(k) for k in value.keys if k is not None}
    assert built, "register_routes has no `built` handler map"
    routes = {(m, norm(p)): s for m, p, s, name in ast.literal_eval(_assigned(tree, "SLICE_ROUTES")) if name in built}
    callers = _assigned(ast.parse((core_dir / "auth.py").read_text()), "CALLER_SCOPES")
    assert isinstance(callers, ast.Dict)
    return routes, {ast.literal_eval(k): _scopes(v) for k, v in zip(callers.keys, callers.values)}


def knowledge_table() -> tuple[dict[tuple[str, str], str], dict[str, set[str]]]:
    """What: knowledge's decorated routes ((METHOD, path) to scope) and its caller scope sets.
    Use when: building the server side. Inputs: none. Output: (routes, scopes by caller). Side effects: reads files.
    Role: knowledge's side of the contract (app.get/post decorators with require_scope in main.py, CALLERS in auth.py)."""
    routes: dict[tuple[str, str], str] = {}
    for node in ast.walk(ast.parse((KNOW / "main.py").read_text())):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for dec in node.decorator_list:
            if not (isinstance(dec, ast.Call) and isinstance(dec.func, ast.Attribute) and dec.func.attr in {"get", "post", "put", "delete"}):
                continue
            scope = [c.args[0].value for c in ast.walk(dec) if isinstance(c, ast.Call) and isinstance(c.func, ast.Attribute)
                     and c.func.attr == "require_scope" and c.args and isinstance(c.args[0], ast.Constant)]
            if scope:
                routes[(dec.func.attr.upper(), norm(ast.literal_eval(dec.args[0])))] = scope[0]
    table = _assigned(ast.parse((KNOW / "auth.py").read_text()), "CALLERS")
    assert isinstance(table, ast.Dict)
    return routes, {ast.literal_eval(k): _scopes(v.elts[1]) for k, v in zip(table.keys, table.values) if isinstance(v, ast.Tuple)}


def test_the_extractors_find_what_they_should():
    """What: prove the checks below can see something. Use when: CI. Inputs: none. Output: pass/fail.
    Side effects: none. Role: a green result that compared nothing is the worst kind (a zero must be provably possible)."""
    assert ("GET", "/v1/me/context") in client_calls(CLIENTS[0][0])
    assert ("POST", "/v1/actions/pending/{}/commit") in client_calls(CLIENTS[1][0])
    assert client_calls(CLIENTS[2][0]) == {("POST", "/v1/search")}
    routes, scopes = core_table()
    assert routes[("GET", "/v1/me/context")] == "context.read" and "context.read" in scopes["agent_tools"]
    routes, scopes = knowledge_table()
    assert routes[("POST", "/v1/search")] == "search" and "search" in scopes["agent"]


def test_every_path_an_agent_client_calls_is_served_with_a_scope_the_token_holds():
    """What: for every client call, the target has that (method, path) and the caller's token has the route's scope.
    Use when: CI. Inputs: none. Output: pass/fail listing every drifted call. Side effects: none.
    Role: stops an agent tool calling a route that is not built (the get_account_summary 404)."""
    tables = {"core": core_table(), "knowledge": knowledge_table()}
    problems: list[str] = [f"{p.relative_to(ROOT)} calls the http helper but is not in CLIENTS" for p in uncovered(AGENT, [c[0] for c in CLIENTS])]
    for source, service, caller in CLIENTS:
        routes, scopes = tables[service]
        for call in sorted(client_calls(source)):
            scope = routes.get(call)
            if scope is None:
                problems.append(f"{source.name}: {call[0]} {call[1]} is not served by {service}")
            elif scope not in scopes[caller]:
                problems.append(f"{source.name}: {call[0]} {call[1]} needs scope {scope}, token {caller} lacks it")
    assert not problems, "\n".join(problems)


# ---- fix round 1: the guard must not be silent, hand-listed or fooled by 501 stubs (review of PR 79) ----

import pytest  # noqa: E402


def _client_file(tmp_path: Path, body: str) -> Path:
    """What: write a tiny client module. Use when: probing the extractor. Inputs: tmp dir, the method body lines.
    Output: its path. Side effects: writes a file. Role: synthetic client."""
    f = tmp_path / "extra.py"
    f.write_text("class C:\n    async def m(self, control):\n" + body)
    return f


def test_a_call_whose_url_is_a_variable_is_resolved_or_fails_loudly(tmp_path):
    """What: a url held in a local variable is followed to its f-string; one that cannot be resolved raises.
    Use when: CI. Inputs: tmp_path. Output: assertions. Side effects: none. Role: review F1, never skip silently."""
    ok = _client_file(tmp_path, '        url = f"{self._base}/v1/me/tickets"\n        return await call_json(self._http, "GET", url, {})\n')
    assert client_calls(ok) == {("GET", "/v1/me/tickets")}
    bad = _client_file(tmp_path, '        return await call_json(self._http, "GET", self.make_url(), {})\n')
    with pytest.raises(AssertionError, match="cannot resolve"):
        client_calls(bad)
    dyn = _client_file(tmp_path, '        return await call_json(self._http, self.verb, f"{self._base}/v1/x", {})\n')
    with pytest.raises(AssertionError, match="cannot resolve"):
        client_calls(dyn)


def test_every_module_that_calls_the_http_helper_is_covered(tmp_path):
    """What: discovery finds every agent module calling call_json*, and one that is not in the covered list is reported.
    Use when: CI. Inputs: tmp_path. Output: assertions. Side effects: none. Role: review F2, no hand-written file list."""
    (tmp_path / "clients").mkdir()
    (tmp_path / "clients" / "new_client.py").write_text("async def f():\n    return await call_json_within(1, h, 'GET', u, {})\n")
    (tmp_path / "clients" / "http.py").write_text("async def call_json(): ...\n")
    (tmp_path / "other.py").write_text("x = 1\n")
    assert [p.name for p in discover_clients(tmp_path)] == ["new_client.py"]
    assert [p.name for p in uncovered(tmp_path, [])] == ["new_client.py"]
    assert uncovered(tmp_path, [tmp_path / "clients" / "new_client.py"]) == []
    real = {p.resolve() for p, _, _ in CLIENTS}
    assert {p.resolve() for p in discover_clients(AGENT)} == real


def test_a_listed_route_without_a_built_handler_is_not_served(tmp_path):
    """What: a SLICE_ROUTES row with no handler in register_routes (it answers 501) is left out of the served table.
    Use when: CI. Inputs: tmp_path. Output: assertions. Side effects: none. Role: review F3."""
    (tmp_path / "routes.py").write_text(
        'SLICE_ROUTES = [("GET", "/v1/a", "s.a", "a"), ("GET", "/v1/b", "s.b", "b")]\n'
        "def register_routes(app, overrides=None):\n"
        '    built = {"a": a, **(overrides or {})}\n')
    (tmp_path / "auth.py").write_text('CALLER_SCOPES = {"x": frozenset({"s.a"})}\n')
    routes, _ = core_table(tmp_path)
    assert routes == {("GET", "/v1/a"): "s.a"}
