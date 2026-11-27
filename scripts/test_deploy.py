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
               EDGE_SUBNET="172.30.0.0/24", FORWARDED_ALLOW_IPS="172.30.0.0/24", DEMO_FAULTS_ENABLED="true", FEATURE_A4="false")
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
    steps = ["rollback point", "git checkout", "--profile jobs build", "backup before migrate", "migrate_core", "migrate_knowledge", "migrate_agent", "seed_core",
             "ingest_knowledge", "in-flight check", "--force-recreate core", "--force-recreate agent_a", "--force-recreate agent_b", "--force-recreate caddy", "SMOKE"]
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


def test_rollback_needs_a_recorded_tag(tmp_path: Path) -> None:
    """What: rollback with no rollback_to file fails. Use when: pytest. Inputs: tmp. Output: assert.
    Side effects: none. Role: never guess a rollback target."""
    p = run(["rollback"], good_env(), tmp_path)
    assert p.returncode != 0 and "no rollback tag" in p.stderr.lower()


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
    assert all(f"{n}='{SENTINEL}-{n}'\n" in body for n in required_names())
    assert SENTINEL not in p.stdout + p.stderr


def test_required_list_matches_compose() -> None:
    """What: required-env.txt equals the compose.yaml variables that have no default. Use when: pytest. Inputs: none.
    Output: assert. Side effects: reads files. Role: drift guard (a new secret in compose must reach the VM .env)."""
    text = (ROOT / "compose.yaml").read_text()
    no_default = set(re.findall(r"\$\{([A-Z][A-Z0-9_]*)(?::\?[^}]*)?\}", text)) - {"SITE_ADDRESS"}
    assert set(required_names()) == no_default


def test_rollback_with_recorded_tag_prints_checkout_then_restart(tmp_path: Path) -> None:
    """What: rollback with a recorded tag checks it out, builds, restarts a then b, smokes. Use when: pytest.
    Inputs: tmp. Output: assert. Side effects: writes tmp/state. Role: the rollback order."""
    (tmp_path / "state").mkdir()
    (tmp_path / "state" / "rollback_to").write_text("v0\n")
    p = run(["rollback"], good_env(), tmp_path)
    assert p.returncode == 0, p.stderr
    out = p.stdout
    order = [out.index(x) for x in ("checkout --detach refs/tags/v0", "--profile jobs build", "--force-recreate agent_a",
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
    import yaml

    wf = yaml.safe_load((ROOT / ".github" / "workflows" / "deploy.yml").read_text())
    triggers = wf.get("on", wf.get(True))  # YAML 1.1 reads the key `on` as True
    assert set(triggers) == {"workflow_dispatch"}
    assert triggers["workflow_dispatch"]["inputs"]["tag"]["required"] is True


def test_vm_requires_explicit_behaviour_flags(tmp_path: Path) -> None:
    """What: DEMO_FAULTS_ENABLED and FEATURE_A4 must be true or false on the VM. Use when: pytest. Inputs: tmp.
    Output: assert. Side effects: none. Role: no silent compose default for behaviour flags."""
    env = good_env()
    del env["DEMO_FAULTS_ENABLED"]
    p = run(["up", "--tag", "v1"], env, tmp_path)
    assert p.returncode != 0 and "DEMO_FAULTS_ENABLED" in p.stderr


def test_render_env_rejects_unsafe_values(tmp_path: Path) -> None:
    """What: a value with a single quote is refused (Compose env-file parsing). Use when: pytest. Inputs: tmp.
    Output: assert. Side effects: none. Role: M7."""
    env = {n: "x" for n in required_names()}
    env["PIN_PEPPER"] = "a'b"
    out = tmp_path / "r.env"
    p = subprocess.run(["bash", str(SCRIPT), "render-env", "--out", str(out)], env={"PATH": os.environ["PATH"], **env},
                       capture_output=True, text=True, cwd=ROOT)
    assert p.returncode != 0 and "PIN_PEPPER" in p.stderr and not out.exists()


# ---- state machine tests: a stub docker, git and curl on PATH, the real script in non-print mode ----

STUB_DOCKER = r"""#!/bin/bash
a="$*"
echo "docker $a" >> "$STUB_LOG"
case "$a" in
  *pg_dump*) if [ -n "${STUB_SHORT:-}" ]; then echo tiny; else echo "-- fake dump"; for i in $(seq 1 60); do echo "-- padding line $i to make the file longer than one thousand bytes"; done; echo "-- PostgreSQL database dump complete"; fi ;;
  *query_to_xml*) for i in $(seq 1 "${STUB_SCHEMAS:-0}"); do echo "core.t$i 1"; done ;;
  *"-T core python"*) echo "200 {\"status\":\"ready\",\"contract_version\":\"$CV\",\"policy_version\":\"$PV\",\"demo_faults\":true,\"seed_date\":\"2026-11-27\"}" ;;
  *"-T knowledge python"*) echo "200 {\"status\":\"ready\",\"database\":\"ok\",\"chunks\":74,\"embedding_model\":\"$EM\",\"calibration_status\":\"provisional\"}" ;;
  *"ps -q "*) echo "cid-${a##* }" ;;
  *"image inspect"*) echo "sha-${a##*img-}" ;;
  *"inspect -f {{.Image}}"*) echo "sha-${a##*cid-}" ;;
  *"inspect -f {{.Config.Image}}"*) echo "img-${a##*cid-}" ;;
  *--single-transaction*) cat > /dev/null; [ -z "${STUB_RESTORE_FAIL:-}" ] || exit 1 ;;
  *"up -d --no-deps"*) [ -z "${STUB_FAIL_UP:-}" ] || exit 1 ;;
  *"count(*) from information_schema.tables"*) echo "${STUB_SCHEMAS:-0}" ;;
  *"to_regclass"*) echo "${STUB_REGCLASS:-f}" ;;
  *"from agent.turns"*) echo "${STUB_TURNS:-0}" ;;
esac
exit 0
"""
STUB_GIT = r"""#!/bin/bash
echo "git $*" >> "$STUB_LOG"
case "$*" in *"status --porcelain"*) ;; *describe*) echo v-stub ;; *rev-parse*"--short"*) echo abc1234 ;; esac
exit 0
"""
STUB_CURL = r"""#!/bin/bash
echo "curl $*" >> "$STUB_LOG"
case "$*" in
  *api/readyz*) [ -z "${STUB_FAIL_EDGE:-}" ] || exit 22
    echo "{\"status\":\"ready\",\"contract_version\":\"$CV\",\"redis\":\"ok\",\"postgres\":\"ok\",\"core\":\"ok\",\"knowledge\":\"ok\",\"demo_faults\":true}" ;;
esac
exit 0
"""


def stub_env(tmp: Path, **extra: str) -> dict[str, str]:
    """What: build a PATH with stub docker, git and curl and the expected-version values. Use when: state tests.
    Inputs: tmp, extra env. Output: env dict. Side effects: writes executables under tmp/bin. Role: no real docker, git or network."""
    import hashlib

    bin_dir = tmp / "bin"
    bin_dir.mkdir(exist_ok=True)
    for name, body in (("docker", STUB_DOCKER), ("git", STUB_GIT), ("curl", STUB_CURL)):
        f = bin_dir / name
        f.write_text(body)
        f.chmod(0o755)
    pol = (ROOT / "data" / "policy.yaml").read_bytes()
    label = re.search(r'^version_label: *"?([^"\n]*)"?', pol.decode(), re.M).group(1)  # type: ignore[union-attr]
    models = (ROOT / "data" / "models.yaml").read_text()
    em = re.search(r"embeddings:.*?model: *(\S+)", models, re.S).group(1)  # type: ignore[union-attr]
    return {"PATH": f"{bin_dir}:{os.environ['PATH']}", "HOME": str(tmp), "STUB_LOG": str(tmp / "stub.log"),
            "CV": (ROOT / "packages/contracts/VERSION").read_text().strip(), "EM": em,
            "PV": f"{label}+{hashlib.sha256(pol).hexdigest()[:12]}", "SMOKE_CHAT": "off", **extra}


def real(args: list[str], tmp: Path, **extra: str) -> subprocess.CompletedProcess:
    """What: run the real script (no --print) with the stubs. Use when: state tests. Inputs: args, tmp, extra env.
    Output: CompletedProcess. Side effects: writes tmp/state and tmp/.env. Role: exercises begin_up, mark_good, smoke and rollback."""
    f = tmp / ".env"
    f.write_text("".join(f"{k}={v}\n" for k, v in good_env().items()))
    f.chmod(0o600)
    return subprocess.run(["bash", str(SCRIPT), "--env-file", str(f), "--state-dir", str(tmp / "state"), *args],
                          env=stub_env(tmp, **extra), capture_output=True, text=True, cwd=ROOT, timeout=120)


def state(tmp: Path, name: str) -> str:
    """What: read a state file or empty text. Use when: state tests. Inputs: tmp, name. Output: str. Side effects: none. Role: helper."""
    f = tmp / "state" / name
    return f.read_text().strip() if f.exists() else ""


def test_failed_up_then_rollback_restores_last_good_tag(tmp_path: Path) -> None:
    """What: up v1 green, up v2 green, up v3 failing (edge down), rollback restores v2, the last GOOD tag. Use when: pytest.
    Inputs: tmp. Output: assert. Side effects: tmp state. Role: C1 (the review's reproduced Critical)."""
    assert real(["up", "--tag", "v1"], tmp_path).returncode == 0
    assert state(tmp_path, "good") == "v1" and state(tmp_path, "pending") == ""
    assert real(["up", "--tag", "v2"], tmp_path).returncode == 0
    assert state(tmp_path, "good") == "v2" and state(tmp_path, "rollback_to") == "v1"
    bad = real(["up", "--tag", "v3"], tmp_path, STUB_FAIL_EDGE="1")
    assert bad.returncode != 0 and "rollback" in bad.stderr
    assert state(tmp_path, "good") == "v2" and state(tmp_path, "pending") == "v3" and state(tmp_path, "rollback_to") == "v2"
    open(tmp_path / "stub.log", "w").close()
    rb = real(["rollback"], tmp_path)
    assert rb.returncode == 0, rb.stderr
    assert "checkout --detach refs/tags/v2" in (tmp_path / "stub.log").read_text()
    assert state(tmp_path, "good") == "v2" and state(tmp_path, "pending") == "" and state(tmp_path, "rolled_back_from") == "v3"


def test_failed_first_deploy_has_no_rollback_tag(tmp_path: Path) -> None:
    """What: a failing first `up` leaves no rollback target and rollback says so. Use when: pytest. Inputs: tmp. Output: assert.
    Side effects: tmp state. Role: C1 edge case: the first deploy cannot be rolled back, and the script must not guess."""
    assert real(["up", "--tag", "v1"], tmp_path, STUB_FAIL_EDGE="1").returncode != 0
    assert state(tmp_path, "good") == ""
    rb = real(["rollback"], tmp_path)
    assert rb.returncode != 0 and "no rollback tag" in rb.stderr


def test_smoke_rejects_wrong_policy_version(tmp_path: Path) -> None:
    """What: a core body with another policy_version fails the smoke. Use when: pytest. Inputs: tmp. Output: assert.
    Side effects: none. Role: I5 (versions are compared with the checkout, not just present)."""
    p = real(["smoke"], tmp_path, PV="not-the-policy")
    assert p.returncode != 0 and "policy_version" in p.stderr


def test_smoke_rejects_degraded_edge(tmp_path: Path) -> None:
    """What: a good smoke passes and a wrong contract version at the edge or core fails. Use when: pytest. Inputs: tmp.
    Output: assert. Side effects: none. Role: I5 (every field is compared)."""
    ok = real(["smoke"], tmp_path)
    assert ok.returncode == 0, ok.stderr
    assert real(["smoke"], tmp_path, CV="wrong").returncode != 0


def test_inflight_turns_abort_the_switch(tmp_path: Path) -> None:
    """What: open turns after the wait abort before any restart. Use when: pytest. Inputs: tmp. Output: assert.
    Side effects: tmp state. Role: I4."""
    p = real(["up", "--tag", "v1"], tmp_path, STUB_REGCLASS="t", STUB_TURNS="2", INFLIGHT_WAIT_S="0")
    assert p.returncode != 0 and "still running" in p.stderr
    assert "force-recreate" not in (tmp_path / "stub.log").read_text()


def test_backup_read_back_refuses_a_tiny_dump(tmp_path: Path) -> None:
    """What: with existing tables the backup step runs pg_dump and refuses a tiny dump before any migration. Use when: pytest.
    Inputs: tmp. Output: assert. Side effects: tmp state. Role: I3 (the read-back must be able to fail)."""
    p = real(["up", "--tag", "v1"], tmp_path, STUB_SCHEMAS="3", STUB_SHORT="1")
    assert p.returncode != 0 and "backup is only" in p.stderr
    assert "pg_dump" in (tmp_path / "stub.log").read_text()
    assert "migrate_core" not in (tmp_path / "stub.log").read_text()


def test_backup_is_bound_to_base_and_release_and_a_retry_keeps_the_first(tmp_path: Path) -> None:
    """What: each backup records its base good tag and the release it precedes; a retry on the same base keeps the first.
    Use when: pytest. Inputs: tmp. Output: assert. Side effects: tmp state. Role: N2 (last_backup could point at the wrong backup)."""
    assert real(["up", "--tag", "v1"], tmp_path, STUB_SCHEMAS="3").returncode == 0
    assert real(["up", "--tag", "v2"], tmp_path, STUB_SCHEMAS="3").returncode == 0
    rec = (tmp_path / "state" / "backup").read_text().splitlines()
    assert rec[1:] == ["v1", "v2"] and Path(rec[0]).exists() and Path(rec[0] + ".manifest").exists()
    assert real(["up", "--tag", "v3"], tmp_path, STUB_SCHEMAS="3", STUB_FAIL_EDGE="1").returncode != 0
    first = (tmp_path / "state" / "backup").read_text()
    assert first.splitlines()[1:] == ["v2", "v3"]
    retry = real(["up", "--tag", "v3"], tmp_path, STUB_SCHEMAS="3", STUB_FAIL_EDGE="1")
    assert "keeping the backup of the first attempt" in retry.stdout
    assert (tmp_path / "state" / "backup").read_text() == first
    assert len(list((tmp_path / "state" / "backups").glob("*.sql"))) == 3  # v1, v2 and v3 once each: the retry took no extra dump


def test_stale_backup_record_is_cleared_by_a_new_release(tmp_path: Path) -> None:
    """What: a record left from an older cycle is removed when a new `up` starts with no pending attempt. Use when: pytest.
    Inputs: tmp. Output: assert. Side effects: tmp state. Role: N2 (a failure before the backup step left a stale pointer)."""
    st = tmp_path / "state"
    st.mkdir()
    (st / "good").write_text("v2\n")
    (st / "backup").write_text("/nonexistent.sql\nv1\nv2\n")
    assert real(["up", "--tag", "v3"], tmp_path).returncode == 0  # fresh database: no new backup is taken
    assert not (st / "backup").exists()


def test_restore_refuses_a_backup_from_another_release(tmp_path: Path) -> None:
    """What: rollback --restore-db refuses when the recorded backup's base is not the rollback tag. Use when: pytest. Inputs: tmp.
    Output: assert. Side effects: tmp state. Role: N2 (bind the backup to the release it precedes)."""
    st = tmp_path / "state"
    st.mkdir()
    dump = st / "x.sql"
    dump.write_text("-- dump")
    Path(str(dump) + ".manifest").write_text("core.t1 1\n")
    (st / "backup").write_text(f"{dump}\nv1\nv2\n")
    (st / "rollback_to").write_text("v2\n")
    bad = run(["rollback", "--restore-db"], good_env(), tmp_path)
    assert bad.returncode != 0 and "another release" in bad.stderr
    (st / "rollback_to").write_text("v1\n")
    ok = run(["rollback", "--restore-db"], good_env(), tmp_path)
    assert ok.returncode == 0 and "single-transaction" in ok.stdout and "manifest" in ok.stdout


def test_every_job_image_is_built_before_it_runs(tmp_path: Path) -> None:
    """What: the build command carries the seed and jobs profiles and precedes the jobs. Use when: pytest. Inputs: tmp.
    Output: assert. Side effects: tmp state. Role: I1 (stale seed and ingest images) and I2 (caddy is recreated)."""
    assert real(["up", "--tag", "v1"], tmp_path).returncode == 0
    log = (tmp_path / "stub.log").read_text().splitlines()
    b = next(i for i, ln in enumerate(log) if "--profile seed --profile jobs build" in ln)
    assert b < next(i for i, ln in enumerate(log) if "run --rm --no-deps seed_core" in ln)
    assert b < next(i for i, ln in enumerate(log) if "run --rm --no-deps ingest_knowledge" in ln)
    assert any("--force-recreate caddy" in ln for ln in log)


def _restore_state(tmp: Path) -> None:
    """What: write a good state, a recorded backup bound to v1 and rollback_to v1. Use when: restore tests. Inputs: tmp.
    Output: none. Side effects: writes tmp/state. Role: arrange a restore."""
    st = tmp / "state"
    st.mkdir(exist_ok=True)
    dump = st / "x.sql"
    dump.write_text("-- dump\n")
    Path(str(dump) + ".manifest").write_text("core.t1 1\n")
    (st / "backup").write_text(f"{dump}\nv1\nv2\n")
    (st / "rollback_to").write_text("v1\n")
    (st / "good").write_text("v1\n")


def test_failed_restore_starts_the_apps_with_up_not_start(tmp_path: Path) -> None:
    """What: a failed restore restarts core, knowledge and both agents with `up -d --no-deps` (never `compose start`) and says so.
    Use when: pytest. Inputs: tmp. Output: assert. Side effects: tmp state. Role: N3 (`compose start` failed on the real stack and was hidden)."""
    _restore_state(tmp_path)
    p = real(["rollback", "--restore-db"], tmp_path, STUB_RESTORE_FAIL="1")
    log = (tmp_path / "stub.log").read_text()
    assert p.returncode != 0 and "app services are running again" in p.stderr
    assert "up -d --no-deps --wait --wait-timeout 180 core knowledge agent_a agent_b" in log
    assert " start " not in log


def test_failed_restore_with_failed_restart_fails_loudly(tmp_path: Path) -> None:
    """What: when the restart after a failed restore also fails, the script says the site is down and how to start it.
    Use when: pytest. Inputs: tmp. Output: assert. Side effects: tmp state. Role: N3 (no `|| true`)."""
    _restore_state(tmp_path)
    p = real(["rollback", "--restore-db"], tmp_path, STUB_RESTORE_FAIL="1", STUB_FAIL_UP="1")
    assert p.returncode != 0 and "could NOT be restarted" in p.stderr and "site is down" in p.stderr
