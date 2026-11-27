"""What: the one guarded core reseed for evals: check that the target is the stack under test, run the DEMO_RESEED
CLI pinned to that Compose project, and prove from the graded core database that the reseed landed there.
Use when: before each case, by the golden CLI (`python -m evals run`) and the S5 e2e tests.
Inputs: env (COMPOSE_PROJECT_NAME, DEMO_FAULTS_ENABLED), EVALS_AGENT_URL, the core DB URL being graded, the repo root.
Output: reseed_command (the argv), guarded_reseed (none), last_reseed_ts; ReseedRefused and ReseedError.
Side effects: `docker compose -p <project> port caddy 80` (read-only), the reseed (TRUNCATE of core tables except
audit_events in that project), SELECTs on audit_events.
Role: the reseed must never reach the VM's demo, the owner's local cs-demo stack (port 3000) or another lane's stack.
It refuses unless COMPOSE_PROJECT_NAME is a lane name (`cs-...`, never naming production or demo), a `cs-smoke-*`
project carries this run's marker from scripts/live_smoke.sh, the agent port is not 3000, DEMO_FAULTS_ENABLED=true, the checkout has no `.deploy` (the VM
marker: deploy.sh's state dir), EVALS_AGENT_URL is http on loopback with a port, and that project's Caddy publishes
that port on loopback (the VM publishes 80 and 443 on all interfaces, with no override).
"""
from __future__ import annotations

import re
import subprocess
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .store import _connect

RESEED_ARGS = ["--profile", "seed", "run", "--rm", "-e", "DEMO_RESEED=true", "seed_core",
               "python", "-m", "core.seeds", "--reseed"]
LANE_PROJECT = re.compile(r"cs-[a-z0-9]+(?:-[a-z0-9]+)*")
LOOPBACK = frozenset({"localhost", "127.0.0.1", "::1"})
LOOPBACK_BIND = frozenset({"127.0.0.1", "[::1]", "::1"})
OWNER_DEMO_PORT = 3000  # cs-demo's edge port on the owner's machine (compose.override.yaml default EDGE_PORT)
SMOKE_PREFIX = "cs-smoke-"


def smoke_marker(root: Path, project: str) -> Path:
    """What: the run marker scripts/live_smoke.sh writes for the project it created (content: SMOKE_RUN_NONCE).
    Use when: the guard checks a cs-smoke-* project; tests write one. Inputs: repo root, project. Output: path.
    Side effects: none. Role: proves the stack was created by the smoke script in this run."""
    return root / ".tmp" / "live-smoke" / f"{project}.run"


def smoke_run_refusal(env: Mapping[str, str], root: Path) -> str | None:
    """What: why env does not name a throwaway stack created by scripts/live_smoke.sh in this run, or None.
    Use when: the smoke entry point (always) and the reseed guard (cs-smoke-* projects). Inputs: env, repo root.
    Output: reason or None. Side effects: reads one marker file. Role: PR #73 review I4."""
    project = env.get("COMPOSE_PROJECT_NAME", "")
    if not project.startswith(SMOKE_PREFIX) or not LANE_PROJECT.fullmatch(project):
        return f"COMPOSE_PROJECT_NAME={project!r} is not a throwaway {SMOKE_PREFIX}* stack (run scripts/live_smoke.sh)"
    nonce = env.get("SMOKE_RUN_NONCE", "")
    marker = smoke_marker(root, project)
    try:
        ok = bool(nonce) and marker.read_text().strip() == nonce
    except OSError:
        ok = False
    return None if ok else (f"{project} has no run marker matching SMOKE_RUN_NONCE ({marker}): "
                            "only the stack scripts/live_smoke.sh created in this run may be used")


class ReseedError(RuntimeError):
    """What: the guarded reseed did not complete or did not reach the graded database. Use when: guarded_reseed fails.
    Inputs: message. Output: exception. Side effects: none. Role: the case cannot run on unknown state."""


class ReseedRefused(ReseedError):
    """What: the reseed target is not provably the stack under test. Use when: reseed_command refuses.
    Inputs: message. Output: exception. Side effects: none. Role: the reseed TRUNCATEs core; refuse rather than guess."""


def reseed_command(env: Mapping[str, str], agent_url: str, root: Path,
                   run: Callable[..., Any] = subprocess.run) -> list[str]:
    """What: the reseed command pinned to the stack under test, after checking that it is that stack.
    Use when: before every reseed (and once up front in the golden CLI). Inputs: env, EVALS_AGENT_URL, repo root,
    a subprocess.run (faked in tests). Output: the docker compose argv with `-p <project>`.
    Side effects: one read-only `docker compose port` call, only after the env checks pass.
    Role: raises ReseedRefused with the reason (see module)."""
    project = env.get("COMPOSE_PROJECT_NAME", "")
    if not project:
        raise ReseedRefused("COMPOSE_PROJECT_NAME must name the stack under test (for example cs-a5)")
    if not LANE_PROJECT.fullmatch(project):
        raise ReseedRefused(f"COMPOSE_PROJECT_NAME={project!r} is not a lane stack name (cs-...)")
    if "prod" in project:
        raise ReseedRefused(f"COMPOSE_PROJECT_NAME={project!r} names production; never reseed it")
    if "demo" in project:
        raise ReseedRefused(f"COMPOSE_PROJECT_NAME={project!r} names a demo stack (the owner's cs-demo); never reseed it")
    if project.startswith(SMOKE_PREFIX):
        why = smoke_run_refusal(env, root)
        if why:
            raise ReseedRefused(why)
    if env.get("DEMO_FAULTS_ENABLED", "").lower() != "true":
        raise ReseedRefused("DEMO_FAULTS_ENABLED=true must be set for the stack under test")
    marker = root / ".deploy"
    if marker.exists() or marker.is_symlink():
        raise ReseedRefused(f"{marker} exists: this checkout is a deploy target (the VM); never reseed it")
    bad_url = ReseedRefused(f"EVALS_AGENT_URL={agent_url!r} must be http on loopback with the stack's EDGE_PORT")
    url = urlsplit(agent_url)
    try:
        port = url.port
    except ValueError:
        raise bad_url from None
    if url.scheme != "http" or url.hostname not in LOOPBACK or port is None:
        raise bad_url
    if port == OWNER_DEMO_PORT:
        raise ReseedRefused(f"EVALS_AGENT_URL={agent_url!r} is port {OWNER_DEMO_PORT}, the owner's cs-demo stack; never reseed it")
    r = run(["docker", "compose", "-p", project, "port", "caddy", "80"], cwd=root, capture_output=True, text=True,
            timeout=60)
    published = (r.stdout or "").strip().splitlines()
    ports = {p.rsplit(":", 1)[-1] for p in published if p.rsplit(":", 1)[0] in LOOPBACK_BIND}
    if r.returncode != 0 or str(port) not in ports:
        raise ReseedRefused(f"project {project} does not serve EVALS_AGENT_URL on loopback port {port} "
                            f"(its Caddy publishes {published or 'nothing'})")
    return ["docker", "compose", "-p", project, *RESEED_ARGS]


def last_reseed_ts(core_db: str) -> Any:
    """What: the newest `reseed` audit row's ts in the graded core database. Use when: around each reseed.
    Inputs: core URL. Output: ts or None. Side effects: one SELECT on a read-only connection.
    Role: proves the reseeded database is the one being graded."""
    conn = _connect(core_db)
    try:
        conn.read_only = True
        row = conn.execute("SELECT max(ts) AS ts FROM audit_events WHERE event = 'reseed'").fetchone()
        return None if row is None else row["ts"]
    finally:
        conn.close()


def guarded_reseed(env: Mapping[str, str], agent_url: str, core_db: str, root: Path, *,
                   run: Callable[..., Any] = subprocess.run, last_ts: Callable[[str], Any] = last_reseed_ts) -> None:
    """What: guard, read the reseed ts, reseed with `-p`, read it again. Use when: before each case.
    Inputs: env, EVALS_AGENT_URL, graded core DB URL, repo root, run and last_ts (faked in tests). Output: none.
    Side effects: the guard's port read, two SELECTs, the reseed (TRUNCATE in that project's core).
    Role: raises ReseedRefused before any read or truncate, ReseedError when the reseed fails or the graded
    database did not record it (EVALS_CORE_DB_URL and COMPOSE_PROJECT_NAME name different stacks)."""
    cmd = reseed_command(env, agent_url, root, run)
    before = last_ts(core_db)
    try:
        r = run(cmd, cwd=root, capture_output=True, text=True, timeout=300)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise ReseedError(f"reseed could not run: {type(e).__name__}") from e
    if r.returncode != 0:
        raise ReseedError(f"reseed exited {r.returncode}: {(r.stderr or '')[-300:]}")
    after = last_ts(core_db)
    if after is None or (before is not None and not after > before):
        raise ReseedError("the reseed did not reach the database in EVALS_CORE_DB_URL: "
                          "it and COMPOSE_PROJECT_NAME name different stacks")
