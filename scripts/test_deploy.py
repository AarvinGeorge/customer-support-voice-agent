"""What: tests for scripts/deploy/deploy.sh (arguments, preflight, missing secrets, step order, render-env, deploy by digest, rollback
to the previous release's digests), scripts/deploy/release_manifest.py, the image and release workflows, and drift guards.
Use when: CI runs `pytest scripts`, or after editing scripts/deploy.
Inputs: the script run with a clean environment and --print (prints docker and git commands, runs none). Output: pass or fail.
Side effects: writes only inside pytest tmp dirs. Never calls docker, git, the network or a provider.
Role: proves the deploy script fails loudly on bad input (a check must be shown able to fail), runs steps in the doc 09 order,
and never builds: it runs only the digests CI pushed after the gates (D-072)."""
import hashlib
import json
import os
import re
import stat
import subprocess
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "deploy" / "deploy.sh"
SENTINEL = "SENTINEL-VALUE-DO-NOT-ECHO"
MANIFEST = ROOT / "scripts" / "deploy" / "release_manifest.py"
REPO = "ghcr.io/aarvingeorge/customer-support-voice-agent"
REV = "a" * 40  # the commit every test release was "built" from; the stub git reports it as HEAD
IMAGES = ("agent", "core", "knowledge", "web")


def digest(tag: str, image: str) -> str:
    """What: a deterministic fake sha256 digest per release and image. Use when: arranging releases. Inputs: tag, image.
    Output: "sha256:<64 hex>". Side effects: none. Role: different releases get different digests, so a rollback is visible."""
    return "sha256:" + hashlib.sha256(f"{tag}-{image}".encode()).hexdigest()


def release_doc(tag: str, revision: str = REV, repo: str = REPO) -> dict[str, Any]:
    """What: a release manifest as the release workflow writes it. Use when: arranging a deploy. Inputs: tag, revision, repo.
    Output: dict. Side effects: none. Role: test double of release-<tag>.json."""
    imgs = {i: {"digest": digest(tag, i), "ref": f"{repo}/{i}@{digest(tag, i)}", "tag": f"{repo}/{i}:{revision}"} for i in IMAGES}
    return {"schema": 1, "kind": "release", "tag": tag, "revision": revision, "repo": repo, "run_url": "", "released_at": "x", "images": imgs}


def ref(tag: str, image: str) -> str:
    """What: the digest-pinned reference of one image of one test release. Use when: asserting pulls and read-backs.
    Inputs: tag, image. Output: str. Side effects: none. Role: helper."""
    return f"{REPO}/{image}@{digest(tag, image)}"


def seed_releases(tmp: Path, tags: tuple[str, ...] = ("v0", "v1", "v2", "v3", "local")) -> None:
    """What: write release manifests into the state dir, as a previous deploy or the workflow download leaves them.
    Use when: every deploy test. Inputs: tmp, tags. Output: none. Side effects: writes tmp/state/releases. Role: arrange."""
    d = tmp / "state" / "releases"
    d.mkdir(parents=True, exist_ok=True)
    for t in tags:
        (d / f"release-{t}.json").write_text(json.dumps(release_doc(t)))


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
    seed_releases(tmp)
    # Stub docker, git and gh first on PATH even in --print mode: a refusal or cleanup path must never reach a real stack
    # (standing rule after a test took down the owner's cs-demo stack).
    clean = {"PATH": stub_env(tmp)["PATH"], "HOME": str(tmp), "STUB_LOG": str(tmp / "stub.log"), "REV": REV}
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
    Side effects: none. Role: manifest, pull by digest, migrate core knowledge agent, seed, ingest, core and knowledge, agent_a then agent_b, edge, smoke."""
    p = run(["up", "--tag", "v1"], good_env(), tmp_path)
    assert p.returncode == 0, p.stderr
    steps = ["release manifest", "rollback point", "git checkout", "docker pull " + ref("v1", "agent"), "backup before migrate", "migrate_core", "migrate_knowledge", "migrate_agent", "seed_core",
             "ingest_knowledge", "in-flight check", "--force-recreate core", "--force-recreate agent_a", "--force-recreate agent_b", "--force-recreate caddy", "SMOKE"]
    pos = []
    for s in steps:
        m = [i for i, ln in enumerate(p.stdout.splitlines()) if s in ln]
        assert m, f"step missing: {s}\n{p.stdout}"
        pos.append(m[0])
    assert pos == sorted(pos), p.stdout
    assert "-f compose.yaml -f scripts/deploy/compose.release.yaml" in p.stdout and "compose.local.yaml" not in p.stdout


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
    """What: rollback with a recorded tag checks it out, pulls that release's digests, restarts a then b, smokes. Use when: pytest.
    Inputs: tmp. Output: assert. Side effects: writes tmp/state. Role: the rollback order."""
    (tmp_path / "state").mkdir()
    (tmp_path / "state" / "rollback_to").write_text("v0\n")
    p = run(["rollback"], good_env(), tmp_path)
    assert p.returncode == 0, p.stderr
    out = p.stdout
    order = [out.index(x) for x in ("checkout --detach refs/tags/v0", "docker pull " + ref("v0", "agent"), "--force-recreate agent_a",
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
case "$a" in *compose*) echo "AGENT_IMAGE=${AGENT_IMAGE:-} CORE_IMAGE=${CORE_IMAGE:-} KNOWLEDGE_IMAGE=${KNOWLEDGE_IMAGE:-} WEB_IMAGE=${WEB_IMAGE:-} :: $a" >> "$STUB_LOG.env" ;; esac
case "$a" in
  *pg_dump*) if [ -n "${STUB_SHORT:-}" ]; then echo tiny; else echo "-- fake dump"; for i in $(seq 1 60); do echo "-- padding line $i to make the file longer than one thousand bytes"; done; echo "-- PostgreSQL database dump complete"; fi ;;
  *query_to_xml*) for i in $(seq 1 "${STUB_SCHEMAS:-0}"); do echo "core.t$i 1"; done ;;
  *"-T core python"*) echo "200 {\"status\":\"ready\",\"contract_version\":\"$CV\",\"policy_version\":\"$PV\",\"demo_faults\":true,\"seed_date\":\"2026-11-27\"}" ;;
  *"-T knowledge python"*) echo "200 {\"status\":\"ready\",\"database\":\"ok\",\"chunks\":74,\"embedding_model\":\"$EM\",\"calibration_status\":\"provisional\"}" ;;
  *"ps -q "*) echo "cid-${a##* }" ;;
  *"image inspect -f {{.Id}} "*) echo "id-${a##* }" ;;
  *"image inspect "*) [ -n "${STUB_PRESENT:-}" ] || exit 1 ;;
  *"inspect -f {{.Config.Image}} cid-"*|*"inspect -f {{.Image}} cid-"*)
    svc="${a##*cid-}"
    case "$svc" in agent_a|agent_b) r="$AGENT_IMAGE" ;; core) r="$CORE_IMAGE" ;; knowledge) r="$KNOWLEDGE_IMAGE" ;; web) r="$WEB_IMAGE" ;; esac
    [ "${STUB_WRONG_IMAGE:-}" != "$svc" ] || r="${r%@*}@sha256:0000000000000000000000000000000000000000000000000000000000000000"
    case "$a" in *Config.Image*) echo "$r" ;; *) if [ "${STUB_STALE:-}" = "$svc" ]; then echo id-old; else echo "id-$r"; fi ;; esac ;;
  "pull "*) [ -z "${STUB_FAIL_PULL:-}" ] || exit 1 ;;
  *--single-transaction*) cat > /dev/null; [ -z "${STUB_RESTORE_FAIL:-}" ] || exit 1 ;;
  *"up -d --no-deps"*) [ -z "${STUB_FAIL_UP:-}" ] || exit 1
    [ -z "${STUB_FAIL_RECREATE:-}" ] || case "$a" in *"--force-recreate $STUB_FAIL_RECREATE") exit 1 ;; esac ;;
  *"count(*) from information_schema.tables"*) echo "${STUB_SCHEMAS:-0}" ;;
  *"to_regclass"*) echo "${STUB_REGCLASS:-f}" ;;
  *"from agent.turns"*) echo "${STUB_TURNS:-0}" ;;
esac
exit 0
"""
STUB_GIT = r"""#!/bin/bash
echo "git $*" >> "$STUB_LOG"
case "$*" in *"status --porcelain"*) ;; *describe*) echo v-stub ;; *rev-parse*"--short"*) echo abc1234 ;; *"rev-parse HEAD"*) echo "${STUB_HEAD:-$REV}" ;; esac
exit 0
"""
STUB_GH = r"""#!/bin/bash
echo "gh $*" >> "$STUB_LOG"
exit 1
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
    Inputs: tmp, extra env. Output: env dict. Side effects: writes executables under tmp/bin. Role: no real docker, git, gh or network."""
    bin_dir = tmp / "bin"
    bin_dir.mkdir(exist_ok=True)
    for name, body in (("docker", STUB_DOCKER), ("git", STUB_GIT), ("curl", STUB_CURL), ("gh", STUB_GH)):
        f = bin_dir / name
        f.write_text(body)
        f.chmod(0o755)
    pol = (ROOT / "data" / "policy.yaml").read_bytes()
    label = re.search(r'^version_label: *"?([^"\n]*)"?', pol.decode(), re.M).group(1)  # type: ignore[union-attr]
    models = (ROOT / "data" / "models.yaml").read_text()
    em = re.search(r"embeddings:.*?model: *(\S+)", models, re.S).group(1)  # type: ignore[union-attr]
    return {"PATH": f"{bin_dir}:{os.environ['PATH']}", "HOME": str(tmp), "STUB_LOG": str(tmp / "stub.log"),
            "CV": (ROOT / "packages/contracts/VERSION").read_text().strip(), "EM": em,
            "PV": f"{label}+{hashlib.sha256(pol).hexdigest()[:12]}", "SMOKE_CHAT": "off", "REV": REV, **extra}


def real(args: list[str], tmp: Path, **extra: str) -> subprocess.CompletedProcess:
    """What: run the real script (no --print) with the stubs. Use when: state tests. Inputs: args, tmp, extra env.
    Output: CompletedProcess. Side effects: writes tmp/state and tmp/.env. Role: exercises begin_up, mark_good, smoke and rollback."""
    f = tmp / ".env"
    f.write_text("".join(f"{k}={v}\n" for k, v in good_env().items()))
    f.chmod(0o600)
    seed_releases(tmp)
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
    (tmp_path / "state").mkdir()
    (tmp_path / "state" / "good").write_text("v1\n")  # smoke reads back the running release (v1)
    p = real(["smoke"], tmp_path, PV="not-the-policy")
    assert p.returncode != 0 and "policy_version" in p.stderr


def test_smoke_rejects_degraded_edge(tmp_path: Path) -> None:
    """What: a good smoke passes and a wrong contract version at the edge or core fails. Use when: pytest. Inputs: tmp.
    Output: assert. Side effects: none. Role: I5 (every field is compared)."""
    (tmp_path / "state").mkdir()
    (tmp_path / "state" / "good").write_text("v1\n")  # smoke reads back the running release (v1)
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


def test_every_job_image_is_pulled_by_digest_before_it_runs(tmp_path: Path) -> None:
    """What: the four release images are pulled by digest before the migrate, seed and ingest jobs, which run with them.
    Use when: pytest. Inputs: tmp. Output: assert. Side effects: tmp state. Role: D-072 (the jobs run the tested images) and I2."""
    assert real(["up", "--tag", "v1"], tmp_path).returncode == 0
    log = (tmp_path / "stub.log").read_text().splitlines()
    pulls = [i for i, ln in enumerate(log) if " pull " in ln]
    assert [log[i].split()[-1] for i in pulls] == [ref("v1", i) for i in IMAGES]
    assert max(pulls) < next(i for i, ln in enumerate(log) if "run --rm --no-deps migrate_core" in ln)
    assert max(pulls) < next(i for i, ln in enumerate(log) if "run --rm --no-deps seed_core" in ln)
    want = f"AGENT_IMAGE={ref('v1', 'agent')} CORE_IMAGE={ref('v1', 'core')} KNOWLEDGE_IMAGE={ref('v1', 'knowledge')} WEB_IMAGE={ref('v1', 'web')}"
    env_log = [ln for ln in (tmp_path / "stub.log.env").read_text().splitlines() if ln.startswith("AGENT_IMAGE=")]  # skip probe bodies
    assert env_log and all(ln.startswith(want + " :: ") for ln in env_log)
    assert all("-f compose.yaml -f scripts/deploy/compose.release.yaml" in ln for ln in log if " compose " in ln)
    assert any("--force-recreate caddy" in ln for ln in log)


BUILD = re.compile(r"\b(build|buildx|bake)\b")


def test_deploy_path_never_builds(tmp_path: Path) -> None:
    """What: up, a failed up, rollback (with and without --restore-db), reseed and smoke invoke no docker build of any kind.
    Use when: pytest. Inputs: tmp. Output: assert. Side effects: tmp state. Role: D-072 proof (production runs the artifact the
    gates tested): every docker call the script made is logged by the stub, and none of them builds."""
    assert real(["up", "--tag", "v1"], tmp_path, STUB_SCHEMAS="3").returncode == 0
    assert real(["up", "--tag", "v2"], tmp_path, STUB_SCHEMAS="3", STUB_FAIL_EDGE="1").returncode != 0
    assert real(["rollback", "--restore-db"], tmp_path, STUB_SCHEMAS="3").returncode == 0
    assert real(["reseed"], tmp_path).returncode == 0
    assert real(["smoke"], tmp_path).returncode == 0
    docker = [ln for ln in (tmp_path / "stub.log").read_text().splitlines() if ln.startswith("docker ")]
    assert len(docker) > 50 and any(" pull " in ln for ln in docker)  # the log is shown able to see the calls
    assert [ln for ln in docker if BUILD.search(ln)] == []
    # The planted control: the same filter flags a build line (a check must be shown able to fail).
    assert BUILD.search("docker compose -f compose.yaml --profile seed --profile jobs build")


def test_deploy_script_has_no_build_command() -> None:
    """What: deploy.sh contains no docker build, compose build, buildx or bake command. Use when: pytest. Inputs: none.
    Output: assert. Side effects: reads a file. Role: static half of the no-build proof (covers branches the dry runs miss)."""
    code = [ln for ln in SCRIPT.read_text().splitlines() if not ln.lstrip().startswith("#")]
    assert [ln for ln in code if BUILD.search(ln)] == []


def _yaml_any_tag() -> Any:
    """What: a YAML loader that reads Compose's !reset and !override tags as plain values. Use when: parsing compose files.
    Inputs: none. Output: a loader class. Side effects: none. Role: PyYAML refuses unknown tags."""
    import yaml

    class Loader(yaml.SafeLoader):
        """What: SafeLoader plus Compose tags. Use when: _yaml_any_tag. Inputs: YAML. Output: Python values.
        Side effects: none. Role: helper."""

    def tagged(loader: Any, node: Any) -> Any:
        """What: build the node's plain value, keeping a marker for !reset. Use when: YAML parse. Inputs: loader, node.
        Output: value or "!reset". Side effects: none. Role: helper."""
        if node.tag == "!reset":
            return "!reset"
        if isinstance(node, yaml.SequenceNode):
            return loader.construct_sequence(node)
        if isinstance(node, yaml.MappingNode):
            return loader.construct_mapping(node)
        return loader.construct_scalar(node)

    Loader.add_constructor("!reset", tagged)
    Loader.add_constructor("!override", tagged)
    return Loader


def test_release_override_pins_every_built_service_to_a_release_image() -> None:
    """What: every compose.yaml service with a build section has, in compose.release.yaml, `build: !reset` and the image
    variable of the image built from the same Dockerfile and SERVICE argument. Use when: pytest. Inputs: none. Output: assert.
    Side effects: reads files. Role: drift guard: a new built service cannot be deployed by building, nor with the wrong image."""
    import yaml

    base = yaml.load((ROOT / "compose.yaml").read_text(), Loader=_yaml_any_tag())["services"]
    text = (ROOT / "scripts/deploy/compose.release.yaml").read_text()
    # Compose drops a !reset reached through a YAML alias (found on the first local run), so the file may use none.
    assert not re.search(r"(^|[\s:,{\[])[&*][A-Za-z]", "\n".join(ln for ln in text.splitlines() if not ln.lstrip().startswith("#")))
    rel = yaml.load(text, Loader=_yaml_any_tag())["services"]
    built = {name: svc["build"] for name, svc in base.items() if "build" in svc}
    assert set(rel) == set(built)
    # the image each Compose service gets is the one CI builds from the same build section (release_manifest.IMAGES)
    source = {"AGENT_IMAGE": "agent_a", "CORE_IMAGE": "core", "KNOWLEDGE_IMAGE": "knowledge", "WEB_IMAGE": "web"}
    for name, svc in rel.items():
        assert svc["build"] == "!reset", name
        m = re.fullmatch(r"\$\{([A-Z_]+):\?[^}]+\}", svc["image"])
        assert m and m.group(1) in source, (name, svc["image"])
        assert built[name] == built[source[m.group(1)]], f"{name} is not built like {source[m.group(1)]}"


def test_manifest_revision_must_match_the_checkout(tmp_path: Path) -> None:
    """What: a manifest built from another commit than the checked-out tag stops the deploy before any pull or change.
    Use when: pytest. Inputs: tmp. Output: assert. Side effects: tmp state. Role: config files (data/, Caddyfile) and images come from one commit."""
    p = real(["up", "--tag", "v1"], tmp_path, STUB_HEAD="b" * 40)
    assert p.returncode != 0 and "were built from" in p.stderr
    log = (tmp_path / "stub.log").read_text()
    assert " pull " not in log and "migrate_core" not in log


def test_invalid_manifest_is_refused_before_any_change(tmp_path: Path) -> None:
    """What: a manifest for another tag, an image named by a mutable tag, or a foreign registry is refused, and the state
    dir is untouched. Use when: pytest. Inputs: tmp. Output: assert. Side effects: tmp state. Role: deploy only pinned, own images."""
    for bad, needle in ((lambda d: d.update(tag="v9"), "not 'v1'"),
                        (lambda d: d["images"]["core"].update(ref=f"{REPO}/core:{REV}"), "tag is mutable"),
                        (lambda d: d.update(repo="docker.io/evil"), "repo")):
        doc = release_doc("v1")
        bad(doc)
        mf = tmp_path / "m.json"
        mf.write_text(json.dumps(doc))
        p = real(["up", "--tag", "v1", "--manifest", str(mf)], tmp_path)
        assert p.returncode != 0 and needle in p.stderr, p.stderr
        assert not (tmp_path / "state" / "pending").exists() and not (tmp_path / "state" / "good").exists()


def test_missing_manifest_names_the_ways_to_get_it(tmp_path: Path) -> None:
    """What: no manifest file and a failing `gh release download` stops the deploy naming the file. Use when: pytest.
    Inputs: tmp. Output: assert. Side effects: tmp state. Role: never fall back to a build."""
    p = real(["up", "--tag", "v7"], tmp_path)
    assert p.returncode != 0 and "release-v7.json" in p.stderr
    log = (tmp_path / "stub.log").read_text()
    assert "gh release download v7" in log and " pull " not in log


def test_running_digests_are_read_back_and_recorded(tmp_path: Path) -> None:
    """What: after a green up the state dir records each app service's image reference and id, read from the containers,
    equal to the manifest. Use when: pytest. Inputs: tmp. Output: assert. Side effects: tmp state.
    Role: "the running digest equals the tested digest, read from both" (live-system standard, test once, promote the artifact)."""
    assert real(["up", "--tag", "v1"], tmp_path).returncode == 0
    rows = state(tmp_path, "running").splitlines()
    assert rows[0] == f"release v1 revision {REV}"
    want = {"core": "core", "knowledge": "knowledge", "agent_a": "agent", "agent_b": "agent", "web": "web"}
    assert {r.split()[0]: r.split()[1] for r in rows[1:]} == {s: ref("v1", i) for s, i in want.items()}
    assert state(tmp_path, "current") == "v1"


def test_wrong_running_digest_fails_the_smoke(tmp_path: Path) -> None:
    """What: a container running another digest, or an older image id under the right name, fails the read-back and the
    tag is not marked good. Use when: pytest. Inputs: tmp. Output: assert. Side effects: tmp state. Role: the read-back can fail."""
    p = real(["up", "--tag", "v1"], tmp_path, STUB_WRONG_IMAGE="core")
    assert p.returncode != 0 and "core runs" in p.stderr and state(tmp_path, "good") == ""
    p = real(["up", "--tag", "v1"], tmp_path, STUB_STALE="agent_b")
    assert p.returncode != 0 and "agent_b" in p.stderr and state(tmp_path, "good") == ""


def test_present_image_is_not_pulled_again(tmp_path: Path) -> None:
    """What: an image already on the host (same digest) is not pulled again. Use when: pytest. Inputs: tmp. Output: assert.
    Side effects: tmp state. Role: a rollback to digests already on the VM needs no registry login."""
    assert real(["up", "--tag", "v1"], tmp_path, STUB_PRESENT="1").returncode == 0
    assert " pull " not in (tmp_path / "stub.log").read_text()


def test_failed_pull_stops_before_any_change(tmp_path: Path) -> None:
    """What: a pull failure (no registry login, unknown digest) stops before postgres, backup or migrations. Use when: pytest.
    Inputs: tmp. Output: assert. Side effects: tmp state. Role: fail loudly, never build instead."""
    p = real(["up", "--tag", "v1"], tmp_path, STUB_FAIL_PULL="1")
    log = (tmp_path / "stub.log").read_text()
    assert p.returncode != 0 and "docker login ghcr.io" in p.stderr
    assert "migrate_core" not in log and "up -d" not in log and not BUILD.search(log)


def test_rollback_redeploys_the_previous_release_digests(tmp_path: Path) -> None:
    """What: up v1, up v2, up v3 failing; rollback pulls and runs v2's digests (the last good release) and records them.
    Use when: pytest. Inputs: tmp. Output: assert. Side effects: tmp state. Role: rollback means the previous release's tested images."""
    assert real(["up", "--tag", "v1"], tmp_path).returncode == 0
    assert real(["up", "--tag", "v2"], tmp_path).returncode == 0
    assert real(["up", "--tag", "v3"], tmp_path, STUB_FAIL_EDGE="1").returncode != 0
    assert state(tmp_path, "current") == "v3"
    open(tmp_path / "stub.log", "w").close()
    open(tmp_path / "stub.log.env", "w").close()
    rb = real(["rollback"], tmp_path)
    assert rb.returncode == 0, rb.stderr
    log = (tmp_path / "stub.log").read_text().splitlines()
    assert [ln.split()[-1] for ln in log if " pull " in ln] == [ref("v2", i) for i in IMAGES]
    restarts = [ln for ln in (tmp_path / "stub.log.env").read_text().splitlines() if "--force-recreate" in ln]
    assert restarts and all(ln.startswith(f"AGENT_IMAGE={ref('v2', 'agent')} CORE_IMAGE={ref('v2', 'core')}") for ln in restarts)
    assert "checkout --detach refs/tags/v2" in "\n".join(log)
    assert state(tmp_path, "good") == "v2" and state(tmp_path, "current") == "v2"
    assert ref("v2", "agent") in state(tmp_path, "running") and ref("v3", "agent") not in state(tmp_path, "running")


def test_rollback_restore_db_runs_with_the_failed_release_images_then_switches(tmp_path: Path) -> None:
    """What: with --restore-db the database restore's compose calls use the release that is running (v3), and the restart
    after it uses v2. Use when: pytest. Inputs: tmp. Output: assert. Side effects: tmp state.
    Role: a failed restore restarts what was running (its message says so), never a half-switched mix."""
    assert real(["up", "--tag", "v2"], tmp_path, STUB_SCHEMAS="3").returncode == 0
    assert real(["up", "--tag", "v3"], tmp_path, STUB_SCHEMAS="3", STUB_FAIL_EDGE="1").returncode != 0
    open(tmp_path / "stub.log.env", "w").close()
    assert real(["rollback", "--restore-db"], tmp_path, STUB_SCHEMAS="3").returncode == 0
    lines = (tmp_path / "stub.log.env").read_text().splitlines()
    stop = next(i for i, ln in enumerate(lines) if " stop agent_a" in ln)
    assert lines[stop].startswith(f"AGENT_IMAGE={ref('v3', 'agent')}")
    first_restart = next(i for i, ln in enumerate(lines) if "--force-recreate" in ln)
    assert first_restart > stop and lines[first_restart].startswith(f"AGENT_IMAGE={ref('v2', 'agent')}")


def test_print_mode_calls_no_docker(tmp_path: Path) -> None:
    """What: --print up, rollback and rollback --restore-db make no docker call at all (the stub on PATH logs every call).
    Use when: pytest. Inputs: tmp. Output: assert. Side effects: tmp state. Role: --print runs nothing, shown by measurement."""
    _restore_state(tmp_path)
    for args in (["up", "--tag", "v1"], ["rollback"], ["rollback", "--restore-db"]):
        p = run(args, good_env(), tmp_path)
        assert p.returncode == 0, (args, p.stderr)
    log = tmp_path / "stub.log"
    assert not log.exists() or not [ln for ln in log.read_text().splitlines() if ln.startswith(("docker ", "git ", "gh "))]


def test_rollback_without_the_previous_manifest_fails(tmp_path: Path) -> None:
    """What: a rollback target with no manifest (a pre-registry release) fails naming the file, and changes nothing.
    Use when: pytest. Inputs: tmp. Output: assert. Side effects: tmp state. Role: never rebuild an old tag to roll back."""
    st = tmp_path / "state"
    (st / "releases").mkdir(parents=True)
    (st / "rollback_to").write_text("v5\n")
    p = real(["rollback"], tmp_path)
    assert p.returncode != 0 and "release-v5.json" in p.stderr
    assert "force-recreate" not in (tmp_path / "stub.log").read_text()


def test_every_compose_call_goes_through_the_one_release_invocation() -> None:
    """What: deploy.sh names `docker compose` only in the two functions that add the release override (compose, dc); no other
    line, executed or printed for the operator, may. Use when: pytest. Inputs: none. Output: assert. Side effects: reads a file.
    Role: fix round 1 I3 (a printed recovery command without compose.release.yaml would build on the VM)."""
    code = [ln for ln in SCRIPT.read_text().splitlines() if not ln.lstrip().startswith("#")]
    hits = [ln.strip() for ln in code if "docker compose" in ln]
    assert len(hits) == 2 and hits[0].startswith("compose() {") and hits[1].startswith("dc() {"), hits
    assert all('"${COMPOSE_FILES[@]}"' in h for h in hits)
    # control: the line fixed in round 1 is caught by the same filter
    assert "docker compose" in "die \"... run: docker compose -f compose.yaml up -d --no-deps core\""


def test_runbook_compose_commands_use_the_release_invocation() -> None:
    """What: every `docker compose` command DEPLOY.md gives the operator for the VM is the script's passthrough instead.
    Use when: pytest. Inputs: none. Output: assert. Side effects: reads a file. Role: I3 (the runbook must not teach a build)."""
    text = (ROOT / "scripts" / "deploy" / "DEPLOY.md").read_text()
    spans = re.findall(r"`([^`]*docker compose[^`]*)`", text)
    assert spans == [], spans
    assert "scripts/deploy/deploy.sh compose ps" in text


def test_compose_passthrough_uses_the_running_release(tmp_path: Path) -> None:
    """What: `deploy.sh compose ARGS` runs Compose with the release override and the running release's digests, and the
    site-down message names that form. Use when: pytest. Inputs: tmp. Output: assert. Side effects: tmp state.
    Role: one way to run Compose on the VM, so no operator command can build."""
    assert real(["up", "--tag", "v1"], tmp_path).returncode == 0
    open(tmp_path / "stub.log.env", "w").close()
    p = real(["compose", "up", "-d", "--no-deps", "core"], tmp_path)
    assert p.returncode == 0, p.stderr
    rows = [ln for ln in (tmp_path / "stub.log.env").read_text().splitlines() if ln.startswith("AGENT_IMAGE=")]
    assert len(rows) == 1 and rows[0].startswith(f"AGENT_IMAGE={ref('v1', 'agent')}")
    assert rows[0].endswith("-f compose.yaml -f scripts/deploy/compose.release.yaml up -d --no-deps core")
    _restore_state(tmp_path)
    (tmp_path / "state" / "current").write_text("v1\n")
    p = real(["rollback", "--restore-db"], tmp_path, STUB_RESTORE_FAIL="1", STUB_FAIL_UP="1")
    assert "scripts/deploy/deploy.sh" in p.stderr and " compose up -d --no-deps" in p.stderr and "docker compose" not in p.stderr


def test_compose_passthrough_needs_a_recorded_release(tmp_path: Path) -> None:
    """What: with no release recorded the passthrough refuses before calling Compose. Use when: pytest. Inputs: tmp.
    Output: assert. Side effects: tmp state. Role: never a Compose call without digests."""
    p = real(["compose", "ps"], tmp_path)
    assert p.returncode != 0 and "no release recorded" in p.stderr
    assert "compose" not in (tmp_path / "stub.log").read_text() if (tmp_path / "stub.log").exists() else True


def test_deploy_compose_files_only_for_backup_and_restore(tmp_path: Path) -> None:
    """What: DEPLOY_COMPOSE_FILES (the postgres-only test stack) is refused for up, rollback, reseed, smoke and compose, and
    still accepted for backup. Use when: pytest. Inputs: tmp. Output: assert. Side effects: tmp state.
    Role: review M1 (the hook let `up` run 20 compose calls without the release override)."""
    for args in (["up", "--tag", "v1"], ["rollback"], ["reseed"], ["smoke"], ["compose", "ps"]):
        p = real(args, tmp_path, DEPLOY_COMPOSE_FILES="compose.yaml")
        assert p.returncode != 0 and "DEPLOY_COMPOSE_FILES" in p.stderr, args
    assert " up " not in ((tmp_path / "stub.log").read_text() if (tmp_path / "stub.log").exists() else "")
    p = real(["backup", "--tag", "v2"], tmp_path, DEPLOY_COMPOSE_FILES="scripts/deploy/compose.restore-test.yaml")
    assert p.returncode == 0, p.stderr


def test_current_names_the_release_from_the_first_recreate(tmp_path: Path) -> None:
    """What: when the rolling restart fails part way (agent_a), `current` already names the new release, because core and
    knowledge were switched. Use when: pytest. Inputs: tmp. Output: assert. Side effects: tmp state. Role: review M2."""
    assert real(["up", "--tag", "v1"], tmp_path).returncode == 0
    p = real(["up", "--tag", "v2"], tmp_path, STUB_FAIL_RECREATE="agent_a")
    assert p.returncode != 0
    assert state(tmp_path, "current") == "v2" and state(tmp_path, "pending") == "v2" and state(tmp_path, "good") == "v1"
    # an aborted switch (in-flight turns) recreates nothing and leaves current alone
    p = real(["up", "--tag", "v3"], tmp_path, STUB_REGCLASS="t", STUB_TURNS="1", INFLIGHT_WAIT_S="0")
    assert p.returncode != 0 and state(tmp_path, "current") == "v2"


def test_rollback_checks_the_checkout_against_the_manifest(tmp_path: Path) -> None:
    """What: a rollback whose checkout is not the previous release's commit stops before any pull or restart.
    Use when: pytest. Inputs: tmp. Output: assert. Side effects: tmp state. Role: review M3 (mutation M12 survived)."""
    st = tmp_path / "state"
    st.mkdir()
    (st / "rollback_to").write_text("v1\n")
    p = real(["rollback"], tmp_path, STUB_HEAD="c" * 40)
    assert p.returncode != 0 and "were built from" in p.stderr
    log = (tmp_path / "stub.log").read_text()
    assert " pull " not in log and "force-recreate" not in log


def _build_args_guard() -> str:
    """What: the run script of the images workflow step that requires the web image's public build arguments.
    Use when: pytest. Inputs: none. Output: the script text. Side effects: reads a file. Role: lets the test execute the real guard."""
    steps = _wf("images.yml")["jobs"]["images"]["steps"]
    return next(st["run"] for st in steps if st.get("name", "").startswith("Require the web image's public build arguments"))


def test_images_fail_without_the_langsmith_build_arguments() -> None:
    """What: the guard step fails when LANGSMITH_PROJECT_URL or LANGSMITH_PROJECT is empty or the URL is not https, and passes
    when both are set; it comes before the bake. Use when: pytest. Inputs: none. Output: assert.
    Side effects: runs the step's bash with a clean environment (no docker, no network). Role: review I2 (an empty drawer link
    would be baked into the release image and could only change with a new commit)."""
    script = _build_args_guard()

    def go(**env: str) -> subprocess.CompletedProcess:
        """What: run the guard with env. Use when: this test. Inputs: env. Output: CompletedProcess. Side effects: none.
        Role: helper."""
        return subprocess.run(["bash", "-c", script], env={"PATH": os.environ["PATH"], **env}, capture_output=True, text=True, timeout=30)

    url = "https://smith.langchain.com/o/0000/projects/p/1111"
    assert go(LANGSMITH_PROJECT="cs-voice-agent", LANGSMITH_PROJECT_URL=url).returncode == 0
    for bad in ({"LANGSMITH_PROJECT_URL": ""}, {"LANGSMITH_PROJECT_URL": "smith.langchain.com/x"}, {"LANGSMITH_PROJECT": ""}):
        p = go(**{"LANGSMITH_PROJECT": "cs-voice-agent", "LANGSMITH_PROJECT_URL": url, **bad})
        assert p.returncode != 0 and "::error" in p.stdout, (bad, p.stdout)
    names = [st.get("name", st.get("uses", "")) for st in _wf("images.yml")["jobs"]["images"]["steps"]]
    guard = next(i for i, n in enumerate(names) if n.startswith("Require the web image"))
    assert guard < next(i for i, n in enumerate(names) if n.startswith("Build agent, core"))


# ---- release_manifest.py (the helper CI, the release workflow and deploy.sh share) ----

def mf(args: list[str]) -> subprocess.CompletedProcess:
    """What: run release_manifest.py. Use when: helper tests. Inputs: args. Output: CompletedProcess. Side effects: per args.
    Role: helper."""
    return subprocess.run(["python3", str(MANIFEST), *args], capture_output=True, text=True, cwd=ROOT, timeout=30)


def bake_meta(name: str) -> dict[str, Any]:
    """What: bake metadata as `docker buildx bake --metadata-file` writes it for the four targets. Use when: helper tests.
    Inputs: the pushed tag name. Output: dict. Side effects: none. Role: fixture (shape read from a real bake run, see the report)."""
    targets = {"agent": "agent_a", "core": "core", "knowledge": "knowledge", "web": "web"}
    return {t: {"containerimage.digest": digest("ci", i), "image.name": f"{REPO}/{i}:{name}",
                "containerimage.descriptor": {"mediaType": "application/vnd.oci.image.index.v1+json"}} for i, t in targets.items()}


def test_manifest_images_then_release_then_env(tmp_path: Path) -> None:
    """What: bake metadata becomes an images manifest, then release-r-01.json, then the deploy's env lines. Use when: pytest.
    Inputs: tmp. Output: assert. Side effects: writes tmp. Role: the whole hand-off CI to release to VM in one format."""
    (tmp_path / "meta.json").write_text(json.dumps(bake_meta(REV)))
    p = mf(["images", "--bake-metadata", str(tmp_path / "meta.json"), "--revision", REV, "--repo", REPO, "--name", REV,
            "--run-url", "https://example.invalid/run/1", "--out", str(tmp_path / "images.json")])
    assert p.returncode == 0, p.stderr
    imgs = json.loads((tmp_path / "images.json").read_text())
    assert imgs["kind"] == "images" and imgs["images"]["core"]["ref"] == f"{REPO}/core@{digest('ci', 'core')}"
    p = mf(["release", "--images", str(tmp_path / "images.json"), "--tag", "r-01", "--revision", REV, "--out", str(tmp_path / "rel.json")])
    assert p.returncode == 0, p.stderr
    rel = json.loads((tmp_path / "rel.json").read_text())
    assert rel["tag"] == "r-01" and rel["kind"] == "release" and rel["images"] == imgs["images"]
    p = mf(["env", "--manifest", str(tmp_path / "rel.json"), "--tag", "r-01"])
    assert p.returncode == 0, p.stderr
    assert p.stdout.splitlines() == [f"AGENT_IMAGE={REPO}/agent@{digest('ci', 'agent')}", f"CORE_IMAGE={REPO}/core@{digest('ci', 'core')}",
                                     f"KNOWLEDGE_IMAGE={REPO}/knowledge@{digest('ci', 'knowledge')}", f"WEB_IMAGE={REPO}/web@{digest('ci', 'web')}",
                                     f"RELEASE_REVISION={REV}"]


def test_manifest_refuses_bad_input(tmp_path: Path) -> None:
    """What: an unpushed target, a missing target, a bad release tag and a tag on another commit are refused. Use when: pytest.
    Inputs: tmp. Output: assert. Side effects: writes tmp. Role: every check can fail."""
    meta = bake_meta(REV)
    del meta["web"]["containerimage.digest"]
    (tmp_path / "m1.json").write_text(json.dumps(meta))
    base = ["--revision", REV, "--repo", REPO, "--name", REV, "--out", str(tmp_path / "x.json")]
    p = mf(["images", "--bake-metadata", str(tmp_path / "m1.json"), *base])
    assert p.returncode == 1 and "web" in p.stderr and not (tmp_path / "x.json").exists()
    meta = bake_meta(REV)
    del meta["knowledge"]
    (tmp_path / "m2.json").write_text(json.dumps(meta))
    assert "no target knowledge" in mf(["images", "--bake-metadata", str(tmp_path / "m2.json"), *base]).stderr
    (tmp_path / "m3.json").write_text(json.dumps(bake_meta(REV)))
    assert mf(["images", "--bake-metadata", str(tmp_path / "m3.json"), *base]).returncode == 0
    for tag, rev, needle in (("v1.0", REV, "r-01"), ("r-01", "b" * 40, "not from the tag's commit")):
        p = mf(["release", "--images", str(tmp_path / "x.json"), "--tag", tag, "--revision", rev, "--out", str(tmp_path / "r.json")])
        assert p.returncode == 1 and needle in p.stderr
    p = mf(["env", "--manifest", str(tmp_path / "x.json"), "--tag", "r-01"])
    assert p.returncode == 1 and "not a release manifest" in p.stderr


# ---- the workflows (static checks; the runs themselves are recorded in the PR) ----

def _wf(name: str) -> dict[str, Any]:
    """What: load a workflow file. Use when: workflow tests. Inputs: file name. Output: dict. Side effects: reads a file. Role: helper."""
    import yaml

    return yaml.safe_load((ROOT / ".github" / "workflows" / name).read_text())


def _on(wf: dict[Any, Any]) -> Any:
    """What: the triggers of a workflow (YAML 1.1 reads `on` as True). Use when: workflow tests. Inputs: workflow.
    Output: triggers. Side effects: none. Role: helper."""
    return wf.get("on", wf.get(True))


def test_images_workflow_runs_only_after_a_green_ci_on_main_with_least_privilege() -> None:
    """What: images.yml runs on ci completing on main (or by hand with a sha it re-checks), builds only for a successful ci run
    of a push, builds the tested commit (head_sha, not main's tip), may write packages and read contents and actions only, and
    ci.yml itself writes no package. Use when: pytest. Inputs: none. Output: assert. Side effects: reads files.
    Role: build once, after the gates (D-072), with ci.yml kept gate-only (D-068)."""
    wf = _wf("images.yml")
    on = _on(wf)
    assert on["workflow_run"] == {"workflows": ["ci"], "types": ["completed"], "branches": ["main"]}
    assert set(on) == {"workflow_run", "workflow_dispatch"} and set(on["workflow_dispatch"]["inputs"]) == {"sha"}
    assert wf["permissions"] == {"contents": "read"}
    job = wf["jobs"]["images"]
    assert job["permissions"] == {"contents": "read", "packages": "write", "actions": "read"}
    assert "workflow_run.conclusion == 'success'" in job["if"] and "workflow_run.event == 'push'" in job["if"]
    assert job["env"]["SHA"].startswith("${{ github.event.workflow_run.head_sha")
    assert job["steps"][0]["with"]["ref"] == "${{ env.SHA }}"
    text = json.dumps(job)
    assert "GITHUB_SHA" not in text  # on workflow_run it is main's tip, not the tested commit
    assert "--branch main --event push" in text and "--status success" in text and "merge-base --is-ancestor" in text
    assert "release_manifest.py images" in text and "upload-artifact" in text and "--push" in text
    assert "org.opencontainers.image.source" in text and "org.opencontainers.image.revision" in text
    assert "images-${{ env.SHA }}" in text
    uses = [st["uses"] for st in job["steps"] if "uses" in st and st["uses"].startswith("docker/")]
    assert uses and all(re.search(r"@[0-9a-f]{40}$", u) for u in uses)  # pinned by commit in the job that can write packages
    ci = _wf("ci.yml")
    assert "images" not in ci["jobs"] and ci["permissions"] == {"contents": "read"}
    assert all("packages" not in job.get("permissions", {}) for job in ci["jobs"].values())


def test_release_workflow_promotes_only_main_digests() -> None:
    """What: release.yml runs on an r-* tag or by hand with tag and sha, can write contents (the release) and read actions
    and packages only, requires the green ci run of that sha on main, and takes the manifest from a successful images run. Use when: pytest. Inputs: none.
    Output: assert. Side effects: reads a file. Role: the release names digests the gates' run pushed, never a new build."""
    wf = _wf("release.yml")
    on = _on(wf)
    assert on["push"] == {"tags": ["r-*"]}
    assert set(on["workflow_dispatch"]["inputs"]) == {"tag", "sha"}
    assert wf["permissions"] == {"contents": "write", "actions": "read", "packages": "read"}
    text = json.dumps(wf)
    assert "--branch main" in text and "--event push" in text and "images-$SHA" in text
    runs = "\n".join(st.get("run", "") for st in wf["jobs"]["release"]["steps"])
    assert "actions/artifacts?name=images-$SHA" in runs and '"images success"' in runs
    assert "release_manifest.py release" in text and "gh release create" in text and "imagetools inspect" in text
    runs = "\n".join(st.get("run", "") for st in wf["jobs"]["release"]["steps"]).replace("buildx imagetools", "imagetools")
    assert runs and not BUILD.search(runs)  # inspects digests, never builds


def test_deploy_workflow_downloads_the_release_and_logs_in_read_only() -> None:
    """What: deploy.yml takes the release asset, copies it to the VM state dir, logs the VM in to GHCR with a read-only
    token for the run, and logs out after. Use when: pytest. Inputs: none. Output: assert. Side effects: reads a file.
    Role: the VM pulls the tested digests; it never needs a build or a long-lived registry credential from the workflow."""
    wf = _wf("deploy.yml")
    assert wf["permissions"] == {"contents": "read", "packages": "read"}
    text = json.dumps(wf)
    assert "gh release download" in text and ".deploy/releases" in text and "docker login ghcr.io" in text
    assert "docker logout ghcr.io" in text
    steps = wf["jobs"]["deploy"]["steps"]
    assert any(s.get("if") == "always()" and "logout" in s.get("run", "") for s in steps)


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
