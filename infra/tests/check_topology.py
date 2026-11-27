"""What: static checks on the rendered Compose config (networks, health, env scoping, grace period).
Use when: after editing compose.yaml or compose.override.yaml, and in CI (needs only `docker compose`, no running stack).
Inputs: none (runs `docker compose config --format json` from the repo root; needs the .env names or .env.example copied to .env).
Output: prints PASS lines; exit 1 with the failed check names.
Side effects: runs one docker compose subprocess; no containers started.
Role: proves doc 06 s10 and doc 07 s15.2: core and knowledge are off the edge network, backend is internal, each service gets only its own variables."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

EXPECTED_NETWORKS = {
    "caddy": {"edge"},
    "web": {"edge"},
    "agent_a": {"edge", "backend", "egress"},
    "agent_b": {"edge", "backend", "egress"},
    "core": {"backend"},
    "knowledge": {"backend", "egress"},
    "postgres": {"backend"},
    "redis": {"backend"},
}
# variables that must NOT reach a service (least privilege, doc 07 s15.2 "Read by")
FORBIDDEN_ENV = {
    "core": {"ANTHROPIC_API_KEY", "OPENAI_API_KEY", "LANGSMITH_API_KEY", "KNOWLEDGE_TOKEN_AGENT",
             "SESSION_SIGNING_KEY", "AGENT_DB_URL", "KNOWLEDGE_DB_URL", "AGENT_DB_OWNER_URL"},
    "knowledge": {"ANTHROPIC_API_KEY", "CORE_TOKEN_AGENT", "CORE_TOKEN_COMMIT", "CORE_TOKEN_OPS", "PIN_PEPPER",
                  "SESSION_SIGNING_KEY", "AGENT_DB_URL", "CORE_DB_URL", "CORE_DB_OWNER_URL"},
    "agent_a": {"PIN_PEPPER", "OPERATOR_TOKEN_KEY", "CORE_DB_URL", "KNOWLEDGE_DB_URL", "POSTGRES_PASSWORD",
                "AGENT_DB_OWNER_URL"},
}


def render() -> dict:
    """What: render the merged Compose config as a dict. Use when: every check starts here.
    Inputs: none. Output: parsed JSON. Side effects: one docker compose subprocess. Role: single config source."""
    out = subprocess.run(["docker", "compose", "--profile", "edge", "config", "--format", "json"], cwd=ROOT,
                         check=True, capture_output=True, text=True).stdout
    return json.loads(out)


def main() -> int:
    """What: run all checks. Use when: invoked as a script. Inputs: none.
    Output: exit code (0 all pass). Side effects: prints results. Role: entry point."""
    cfg = render()
    services, networks = cfg["services"], cfg["networks"]
    failures: list[str] = []

    def check(name: str, ok: bool) -> None:
        print(("PASS " if ok else "FAIL ") + name)
        if not ok:
            failures.append(name)

    for svc, expected in EXPECTED_NETWORKS.items():
        check(f"{svc} networks == {sorted(expected)}", set(services[svc].get("networks", {})) == expected)
    check("backend network is internal", networks["backend"].get("internal") is True)
    check("edge network has a pinned subnet", bool(networks["edge"].get("ipam", {}).get("config")))
    check("core and knowledge never on edge",
          all("edge" not in services[s].get("networks", {}) for s in ("core", "knowledge")))
    for agent in ("agent_a", "agent_b"):
        check(f"{agent} stop_grace_period 1m0s", services[agent].get("stop_grace_period") == "1m0s")
    for svc in ("agent_a", "agent_b", "core", "knowledge", "postgres", "redis"):
        check(f"{svc} has a healthcheck", "healthcheck" in services[svc])
    for svc, banned in FORBIDDEN_ENV.items():
        leaked = banned & set(services[svc].get("environment", {}))
        check(f"{svc} env has none of {sorted(banned)[:2]}...", not leaked)
    for svc in ("core", "knowledge", "agent"):
        check(f"migrate_{svc} runs as {svc} owner url",
              f"{svc.upper()}_DB_OWNER_URL" in services[f"migrate_{svc}"].get("environment", {}))
        check(f"migrate_{svc} has no runtime url", f"{svc.upper()}_DB_URL" not in services[f"migrate_{svc}"].get("environment", {}))
    for svc in ("core", "knowledge"):
        check(f"{svc} waits for its migration",
              services[svc]["depends_on"].get(f"migrate_{svc}", {}).get("condition") == "service_completed_successfully")
    for agent in ("agent_a", "agent_b"):
        check(f"{agent} waits for the agent migration",
              services[agent]["depends_on"].get("migrate_agent", {}).get("condition") == "service_completed_successfully")
    if failures:
        print(f"{len(failures)} check(s) failed")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
