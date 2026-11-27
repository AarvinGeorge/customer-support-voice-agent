"""What: tests for scripts/deploy/deploy.sh (arguments, preflight, missing secrets, step order, render-env) and a drift guard.
Use when: CI runs `pytest scripts`, or after editing scripts/deploy.
Inputs: the script run with a clean environment and --print (prints docker and git commands, runs none). Output: pass or fail.
Side effects: writes only inside pytest tmp dirs. Never calls docker, git, the network or a provider.
Role: proves the deploy script fails loudly on bad input (a check must be shown able to fail) and runs steps in the doc 09 order."""
import os
import re
import stat
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "deploy" / "deploy.sh"
SENTINEL = "SENTINEL-VALUE-DO-NOT-ECHO"


def required_names() -> list[str]:
    """What: names in required-env.txt. Use when: building a full env. Inputs: none. Output: list of names.
    Side effects: reads a file. Role: single source for the tests."""
    lines = (ROOT / "scripts" / "deploy" / "required-env.txt").read_text().splitlines()
    return [x.strip() for x in lines if x.strip() and not x.startswith("#")]


def good_env() -> dict[str, str]:
    """What: a complete VM-style environment with sentinel secret values. Use when: arranging a test.
    Inputs: none. Output: dict. Side effects: none. Role: baseline that preflight accepts."""
    env = {n: f"{SENTINEL}-{n}" for n in required_names()}
    env.update(SITE_ADDRESS="demo.example.com", SITE_ORIGIN="https://demo.example.com",
               EDGE_SUBNET="172.30.0.0/24", FORWARDED_ALLOW_IPS="172.30.0.0/24")
    return env


def run(args: list[str], env: dict[str, str], tmp: Path, env_file: bool = True) -> subprocess.CompletedProcess:
    """What: run deploy.sh with a clean environment and an env file written from `env`. Use when: every test.
    Inputs: args, env, tmp dir, whether to write the file. Output: CompletedProcess (text). Side effects: writes tmp/.env.
    Role: the file holds the values, the process environment holds only PATH, so the file path is what is tested."""
    f = tmp / ".env"
    if env_file:
        f.write_text("".join(f"{k}={v}\n" for k, v in env.items()))
        f.chmod(0o600)
    clean = {"PATH": os.environ["PATH"], "HOME": str(tmp)}
    return subprocess.run(["bash", str(SCRIPT), "--env-file", str(f), "--state-dir", str(tmp / "state"), "--print", *args], env=clean,
                          capture_output=True, text=True, cwd=ROOT, timeout=60)


def test_no_arguments_prints_usage(tmp_path: Path) -> None:
    """What: no command exits 2 with usage. Use when: pytest. Inputs: tmp. Output: assert. Side effects: none. Role: argument handling."""
    p = subprocess.run(["bash", str(SCRIPT)], capture_output=True, text=True, cwd=ROOT, env={"PATH": os.environ["PATH"]})
    assert p.returncode == 2 and "usage" in (p.stdout + p.stderr).lower()


def test_unknown_command_fails(tmp_path: Path) -> None:
    """What: an unknown command exits 2. Use when: pytest. Inputs: tmp. Output: assert. Side effects: none. Role: argument handling."""
    p = run(["frobnicate"], good_env(), tmp_path)
    assert p.returncode == 2 and "unknown command" in p.stderr


def test_up_requires_tag_on_the_vm(tmp_path: Path) -> None:
    """What: `up` without --tag fails outside --local. Use when: pytest. Inputs: tmp. Output: assert.
    Side effects: none. Role: a deploy must name the tag it ships."""
    p = run(["up"], good_env(), tmp_path)
    assert p.returncode == 2 and "--tag" in p.stderr


def test_missing_env_file_fails(tmp_path: Path) -> None:
    """What: a missing env file fails and names the file. Use when: pytest. Inputs: tmp. Output: assert. Side effects: none. Role: preflight."""
    p = run(["up", "--tag", "v1"], good_env(), tmp_path, env_file=False)
    assert p.returncode != 0 and ".env" in p.stderr


def test_missing_secret_names_it_without_echoing_values(tmp_path: Path) -> None:
    """What: a missing required variable fails, names it, and prints no value. Use when: pytest. Inputs: tmp.
    Output: assert. Side effects: none. Role: the missing-secret rule (fail clearly, never leak)."""
    env = good_env()
    del env["OPENAI_API_KEY"]
    env["PIN_PEPPER"] = ""
    p = run(["up", "--tag", "v1"], env, tmp_path)
    out = p.stdout + p.stderr
    assert p.returncode != 0
    assert "OPENAI_API_KEY" in out and "PIN_PEPPER" in out
    assert SENTINEL not in out


def test_empty_site_address_fails(tmp_path: Path) -> None:
    """What: empty SITE_ADDRESS fails before any step. Use when: pytest. Inputs: tmp. Output: assert.
    Side effects: none. Role: Caddy refuses to start without it (D-049), so the script refuses first."""
    env = good_env()
    env["SITE_ADDRESS"] = ""
    p = run(["up", "--tag", "v1"], env, tmp_path)
    assert p.returncode != 0 and "SITE_ADDRESS" in p.stderr and "docker compose" not in p.stdout


def test_vm_rejects_plain_http_site_and_origin_mismatch(tmp_path: Path) -> None:
    """What: on the VM a port-only address or a mismatched SITE_ORIGIN fails. Use when: pytest. Inputs: tmp.
    Output: assert. Side effects: none. Role: https only on the public demo (D-049, D-051)."""
    env = good_env()
    env["SITE_ADDRESS"] = ":80"
    assert run(["up", "--tag", "v1"], env, tmp_path).returncode != 0
    env = good_env()
    env["SITE_ORIGIN"] = "https://other.example.com"
    p = run(["up", "--tag", "v1"], env, tmp_path)
    assert p.returncode != 0 and "SITE_ORIGIN" in p.stderr


def test_subnet_and_forwarded_ips_must_match(tmp_path: Path) -> None:
    """What: EDGE_SUBNET differing from FORWARDED_ALLOW_IPS fails. Use when: pytest. Inputs: tmp. Output: assert.
    Side effects: none. Role: D-047 (uvicorn trusts X-Forwarded-For only from the edge subnet)."""
    env = good_env()
    env["FORWARDED_ALLOW_IPS"] = "172.31.0.0/24"
    p = run(["up", "--tag", "v1"], env, tmp_path)
    assert p.returncode != 0 and "FORWARDED_ALLOW_IPS" in p.stderr


def test_demo_reseed_flag_left_on_fails(tmp_path: Path) -> None:
    """What: DEMO_RESEED=true in the env file fails a normal deploy. Use when: pytest. Inputs: tmp. Output: assert.
    Side effects: none. Role: reseed wipes the demo; only the reseed command may set it."""
    env = good_env()
    env["DEMO_RESEED"] = "true"
    p = run(["up", "--tag", "v1"], env, tmp_path)
    assert p.returncode != 0 and "DEMO_RESEED" in p.stderr


def test_up_step_order(tmp_path: Path) -> None:
    """What: the printed plan runs the steps in the doc 09 order. Use when: pytest. Inputs: tmp. Output: assert.
    Side effects: none. Role: build, migrate core knowledge agent, seed, ingest, core and knowledge, agent_a then agent_b, edge, smoke."""
    p = run(["up", "--tag", "v1"], good_env(), tmp_path)
    assert p.returncode == 0, p.stderr
    steps = ["git checkout", "build", "migrate_core", "migrate_knowledge", "migrate_agent", "seed_core",
             "ingest_knowledge", "--force-recreate core", "--force-recreate agent_a", "--force-recreate agent_b", "180 caddy", "SMOKE"]
    pos = []
    for s in steps:
        m = [i for i, ln in enumerate(p.stdout.splitlines()) if s in ln]
        assert m, f"step missing: {s}\n{p.stdout}"
        pos.append(m[0])
    assert pos == sorted(pos), p.stdout
    assert "-f compose.yaml" in p.stdout and "compose.local.yaml" not in p.stdout


def test_local_mode_skips_git_and_uses_overlay(tmp_path: Path) -> None:
    """What: --local needs no tag, runs no git, adds the local overlay. Use when: pytest. Inputs: tmp. Output: assert.
    Side effects: none. Role: the laptop dry-run form."""
    env = good_env()
    env.update(SITE_ADDRESS=":80", SITE_ORIGIN="http://localhost:8088")
    p = run(["--local", "up"], env, tmp_path)
    assert p.returncode == 0, p.stderr
    assert "git " not in p.stdout and "compose.local.yaml" in p.stdout


def test_rollback_needs_a_previous_tag(tmp_path: Path) -> None:
    """What: rollback with no recorded previous tag fails. Use when: pytest. Inputs: tmp. Output: assert.
    Side effects: none. Role: never guess a rollback target."""
    p = run(["rollback"], good_env(), tmp_path)
    assert p.returncode != 0 and "previous" in p.stderr.lower()


def test_render_env_missing_secrets(tmp_path: Path) -> None:
    """What: render-env fails listing missing names and writes nothing. Use when: pytest. Inputs: tmp. Output: assert.
    Side effects: none. Role: the workflow's missing-secret failure (names only)."""
    env = {n: SENTINEL for n in required_names() if n != "CORE_TOKEN_OPS"}
    out = tmp_path / "rendered.env"
    p = subprocess.run(["bash", str(SCRIPT), "--print", "render-env", "--out", str(out)],
                       env={"PATH": os.environ["PATH"], **env}, capture_output=True, text=True, cwd=ROOT)
    assert p.returncode != 0 and "CORE_TOKEN_OPS" in p.stderr and SENTINEL not in p.stdout + p.stderr
    assert not out.exists()


def test_render_env_writes_private_file(tmp_path: Path) -> None:
    """What: render-env with every secret writes a mode 600 file holding them and prints no value. Use when: pytest.
    Inputs: tmp. Output: assert. Side effects: writes tmp. Role: the .env the VM receives."""
    env = {n: f"{SENTINEL}-{n}" for n in required_names()}
    out = tmp_path / "rendered.env"
    p = subprocess.run(["bash", str(SCRIPT), "render-env", "--out", str(out)],
                       env={"PATH": os.environ["PATH"], **env}, capture_output=True, text=True, cwd=ROOT)
    assert p.returncode == 0, p.stderr
    assert stat.S_IMODE(out.stat().st_mode) == 0o600
    body = out.read_text()
    assert all(f"{n}={SENTINEL}-{n}\n" in body for n in required_names())
    assert SENTINEL not in p.stdout + p.stderr


def test_required_list_matches_compose() -> None:
    """What: required-env.txt equals the compose.yaml variables that have no default. Use when: pytest. Inputs: none.
    Output: assert. Side effects: reads files. Role: drift guard (a new secret in compose must reach the VM .env)."""
    text = (ROOT / "compose.yaml").read_text()
    no_default = set(re.findall(r"\$\{([A-Z_]+)\}", text)) - {"SITE_ADDRESS"}
    assert set(required_names()) == no_default


def test_rollback_with_previous_tag_prints_checkout_then_restart(tmp_path: Path) -> None:
    """What: rollback with a recorded previous tag checks it out, rebuilds, restarts a then b, smokes. Use when: pytest.
    Inputs: tmp. Output: assert. Side effects: writes tmp/state. Role: the rollback order."""
    (tmp_path / "state").mkdir()
    (tmp_path / "state" / "previous").write_text("v0\n")
    p = run(["rollback"], good_env(), tmp_path)
    assert p.returncode == 0, p.stderr
    out = p.stdout
    order = [out.index(x) for x in ("checkout --detach refs/tags/v0", "compose.yaml build", "--force-recreate agent_a",
                                    "--force-recreate agent_b", "SMOKE")]
    assert order == sorted(order)


def test_world_readable_env_file_fails_on_the_vm(tmp_path: Path) -> None:
    """What: a 644 env file fails outside --local. Use when: pytest. Inputs: tmp. Output: assert.
    Side effects: chmods a tmp file. Role: the VM .env holds every secret."""
    run(["check"], good_env(), tmp_path)
    (tmp_path / ".env").chmod(0o644)
    p = subprocess.run(["bash", str(SCRIPT), "--env-file", str(tmp_path / ".env"), "--print", "check"],
                       env={"PATH": os.environ["PATH"]}, capture_output=True, text=True, cwd=ROOT)
    assert p.returncode != 0 and "chmod 600" in p.stderr


def test_deploy_workflow_is_manual_only() -> None:
    """What: deploy.yml triggers only on workflow_dispatch with a tag input, never on push or pull_request. Use when: pytest.
    Inputs: none. Output: assert. Side effects: reads a file. Role: the owner decision that deploys are never automatic."""
    lines = [ln for ln in (ROOT / ".github" / "workflows" / "deploy.yml").read_text().splitlines() if not ln.lstrip().startswith("#")]
    text = "\n".join(lines)
    head = text.split("jobs:")[0]
    assert "workflow_dispatch:" in head and "tag:" in head
    assert not re.search(r"^\s*(push|pull_request|schedule|workflow_run|release)\s*:", head, re.M)
