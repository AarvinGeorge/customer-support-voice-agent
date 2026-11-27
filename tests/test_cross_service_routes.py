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


def client_calls(source: Path) -> set[tuple[str, str]]:
    """What: every (METHOD, path) a client file passes to call_json or call_json_within.
    Use when: building the caller side. Inputs: client source path. Output: set of pairs.
    Side effects: reads the file. Role: the agent's side of the contract."""
    out: set[tuple[str, str]] = set()
    for node in ast.walk(ast.parse(source.read_text())):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in HTTP_CALLS):
            continue
        args = [a for a in node.args if not (isinstance(a, ast.Attribute) and a.attr == "_http")]
        strs = [a for a in args if isinstance(a, ast.Constant) and isinstance(a.value, str)]
        urls = [a for a in args if isinstance(a, ast.JoinedStr)]
        if strs and urls:
            parts = [v.value if isinstance(v, ast.Constant) else "{}" for v in urls[0].values][1:]  # first part is self._base
            out.add((strs[0].value.upper(), norm("".join(parts))))
    return out


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


def core_table() -> tuple[dict[tuple[str, str], str], dict[str, set[str]]]:
    """What: core's route table ((METHOD, path) to scope) and its caller scope sets.
    Use when: building the server side. Inputs: none. Output: (routes, scopes by caller). Side effects: reads files.
    Role: core's side of the contract (SLICE_ROUTES in routes.py, CALLER_SCOPES in auth.py)."""
    routes = {(m, norm(p)): s for m, p, s, _ in ast.literal_eval(_assigned(ast.parse((CORE / "routes.py").read_text()), "SLICE_ROUTES"))}
    callers = _assigned(ast.parse((CORE / "auth.py").read_text()), "CALLER_SCOPES")
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
    problems: list[str] = []
    for source, service, caller in CLIENTS:
        routes, scopes = tables[service]
        for call in sorted(client_calls(source)):
            scope = routes.get(call)
            if scope is None:
                problems.append(f"{source.name}: {call[0]} {call[1]} is not served by {service}")
            elif scope not in scopes[caller]:
                problems.append(f"{source.name}: {call[0]} {call[1]} needs scope {scope}, token {caller} lacks it")
    assert not problems, "\n".join(problems)
