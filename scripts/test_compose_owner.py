"""What: tests for scripts/lib/compose_owner.sh (a run may tear down only a Compose project it created) and for every
teardown site that uses it (scripts/pg_tests.sh, scripts/deploy/restore_pg_test.sh; live_smoke.sh is covered in
scripts/test_check_smoke_status.py).
Use when: CI job `python (scripts)`, or after editing a teardown.
Inputs: a fake `docker` FIRST on PATH that records every call and runs nothing; FAKE_EXISTING names projects the fake
reports as having containers. Project names are fake (`qs-probe-*`); no real stack name is ever used.
Output: pass or fail. Side effects: only inside pytest tmp dirs; the real docker is never invoked (asserted).
Role: proves that a refused or unowned teardown never reaches `docker compose down` (the cs-demo incident, PR #74 N1)."""
import os
import shutil
import subprocess
from pathlib import Path

HERE = Path(__file__).resolve().parent
LIB = HERE / "lib" / "compose_owner.sh"
FAKE_DOCKER = """#!/usr/bin/env bash
# What: fake docker. Use when: these tests. Inputs: argv. Output: fake ids. Side effects: appends to $DOCKER_LOG. Role: test double.
printf '%s\\n' "$*" >> "$DOCKER_LOG"
[ -n "${FAKE_DOCKER_FAIL:-}" ] && exit 1
case "$*" in
  *"label=com.docker.compose.project="*)
    for p in ${FAKE_EXISTING:-}; do case "$*" in *"label=com.docker.compose.project=$p") echo "deadbeef0001" ;; esac; done ;;
esac
exit 0
"""


def fake_env(tmp: Path, **extra: str) -> dict[str, str]:
    """What: an env whose PATH starts with a recording fake docker (and fake uv that reports 0.8.22).
    Use when: any test that could reach a teardown. Inputs: tmp dir, extra env. Output: env dict.
    Side effects: writes the fakes under tmp. Role: makes the destructive act impossible in tests.
    The scripts cd to the repo root, so a relative fake dir would stop resolving and the real docker would be found
    (this happened in a fix-round-2 probe): every path here is absolute."""
    tmp = tmp.resolve()
    bin_ = tmp / "fakebin"
    bin_.mkdir(exist_ok=True)
    (bin_ / "docker").write_text(FAKE_DOCKER)
    (bin_ / "uv").write_text('#!/usr/bin/env bash\nprintf "uv %s\\n" "$*" >> "$DOCKER_LOG"\n[ "$1" = --version ] && echo "uv 0.8.22"\nexit 0\n')
    for f in ("docker", "uv"):
        (bin_ / f).chmod(0o755)
    env = {**os.environ, "PATH": f"{bin_}{os.pathsep}{os.environ['PATH']}", "DOCKER_LOG": str(tmp / "docker.log"),
           "COMPOSE_OWNER_DIR": str(tmp / "owners"), **extra}
    for k in ("COMPOSE_PROJECT_NAME", "COMPOSE_OWNER_RUN_ID", "FAKE_EXISTING", "FAKE_DOCKER_FAIL"):
        if k not in extra:
            env.pop(k, None)
    # The guard against the guard: the docker this env resolves must be the fake, from any working directory.
    assert bin_.is_absolute() and Path(env["DOCKER_LOG"]).is_absolute() and Path(env["COMPOSE_OWNER_DIR"]).is_absolute()
    assert shutil.which("docker", path=env["PATH"]) == str(bin_ / "docker")
    return env


def docker_calls(tmp: Path) -> list[str]:
    """What: the recorded docker calls. Use when: asserting. Inputs: tmp dir. Output: lines. Side effects: none. Role: helper."""
    log = tmp.resolve() / "docker.log"
    return log.read_text().splitlines() if log.exists() else []


def sh(script: str, env: dict[str, str]) -> subprocess.CompletedProcess:
    """What: run bash with the library sourced. Use when: library tests. Inputs: script body, env. Output: result.
    Side effects: those of the script (fakes only). Role: helper."""
    return subprocess.run(["bash", "-c", f'set -euo pipefail; source "{LIB}"\n{script}'], env=env, capture_output=True, text=True, timeout=60)


def test_claim_then_teardown_runs_the_command_once_and_clears_the_marker(tmp_path: Path) -> None:
    """What: a fresh project is claimed; its teardown runs the command with the project name; the marker goes.
    Use when: CI. Inputs: tmp. Output: assertions. Side effects: fakes only. Role: the allowed path."""
    env = fake_env(tmp_path)
    r = sh('compose_claim qs-probe-own\ncompose_teardown qs-probe-own bash -c \'echo "down for $COMPOSE_PROJECT_NAME" >> "$DOCKER_LOG"\'', env)
    assert r.returncode == 0, r.stderr
    assert "down for qs-probe-own" in docker_calls(tmp_path)
    assert not (tmp_path / "owners" / "qs-probe-own").exists()


def test_existing_project_is_never_claimed_or_torn_down(tmp_path: Path) -> None:
    """What: a project that already has resources (someone else's stack) is refused, and a teardown without a claim
    does not run its command. Use when: CI. Inputs: tmp. Output: assertions. Side effects: fakes only. Role: the incident."""
    env = fake_env(tmp_path, FAKE_EXISTING="qs-probe-theirs")
    r = sh("compose_claim qs-probe-theirs", env)
    assert r.returncode == 3 and "already has" in r.stderr
    r = sh("compose_teardown qs-probe-theirs docker compose down -v", env)
    assert r.returncode == 3 and "not created by this run" in r.stderr
    assert not [c for c in docker_calls(tmp_path) if "down" in c]


def test_teardown_with_another_runs_id_is_refused(tmp_path: Path) -> None:
    """What: a marker written by run A does not let run B (different run id) tear the project down.
    Use when: CI. Inputs: tmp. Output: assertions. Side effects: fakes only. Role: ownership is per run, not per name."""
    env = fake_env(tmp_path)
    r = sh("compose_claim qs-probe-a; echo \"$COMPOSE_OWNER_RUN_ID\"; sleep 0", env)
    assert r.returncode == 0, r.stderr
    r = sh("COMPOSE_OWNER_RUN_ID=someone-else compose_teardown qs-probe-a docker compose down -v", env)
    assert r.returncode == 3 and not [c for c in docker_calls(tmp_path) if "down" in c]


def test_a_live_claim_blocks_a_second_claim_and_a_dead_empty_one_is_reclaimed(tmp_path: Path) -> None:
    """What: while the claiming process lives, a second claim is refused; after it dies with no resources left the
    claim is reclaimed. Use when: CI. Inputs: tmp. Output: assertions. Side effects: fakes only. Role: no shared teardown."""
    env = fake_env(tmp_path)
    holder = subprocess.Popen(["bash", "-c", f'source "{LIB}"; compose_claim qs-probe-x; echo ok; sleep 30'], env=env, stdout=subprocess.PIPE, text=True)
    assert holder.stdout is not None and holder.stdout.readline().strip() == "ok"
    r = sh("compose_claim qs-probe-x", env)
    assert r.returncode == 3 and "claimed by live pid" in r.stderr
    holder.kill()
    holder.wait()
    r = sh("compose_claim qs-probe-x", env)
    assert r.returncode == 0, r.stderr


def test_docker_failure_fails_closed(tmp_path: Path) -> None:
    """What: when docker cannot list resources, nothing is claimed. Use when: CI. Inputs: tmp. Output: assertions.
    Side effects: fakes only. Role: an unknown state is never treated as empty."""
    env = fake_env(tmp_path, FAKE_DOCKER_FAIL="1")
    r = sh("compose_claim qs-probe-y", env)
    assert r.returncode == 3 and not (tmp_path / "owners" / "qs-probe-y").exists()


def test_bad_project_names_are_refused(tmp_path: Path) -> None:
    """What: empty or odd names are refused before docker. Use when: CI. Inputs: tmp. Output: assertions.
    Side effects: none. Role: input validation."""
    env = fake_env(tmp_path)
    for name in ("", "Qs Probe", "../x"):
        assert sh(f"compose_claim '{name}'", env).returncode == 2
    assert docker_calls(tmp_path) == []


def test_pg_tests_refuses_an_existing_project_with_no_up_and_no_down(tmp_path: Path) -> None:
    """What: scripts/pg_tests.sh on a project that already exists exits 2 and never runs `up` or `down`.
    Use when: CI. Inputs: tmp. Output: assertions. Side effects: fakes only. Role: teardown site 1."""
    env = fake_env(tmp_path, COMPOSE_PROJECT_NAME="qs-probe-pg", FAKE_EXISTING="qs-probe-pg")
    r = subprocess.run(["bash", str(HERE / "pg_tests.sh")], env=env, capture_output=True, text=True, timeout=60)
    assert r.returncode == 2, r.stdout + r.stderr
    calls = docker_calls(tmp_path)
    assert not [c for c in calls if " up " in f" {c} " or " down" in c], calls


def test_restore_pg_test_refuses_an_existing_project_with_no_up_and_no_down(tmp_path: Path) -> None:
    """What: scripts/deploy/restore_pg_test.sh when its project already exists exits 2 and never runs `up` or `down`.
    Use when: CI. Inputs: tmp. Output: assertions. Side effects: fakes only. Role: teardown site 2."""
    env = fake_env(tmp_path, FAKE_EXISTING="cs-restore-test")
    r = subprocess.run(["bash", str(HERE / "deploy" / "restore_pg_test.sh")], env=env, capture_output=True, text=True, timeout=60)
    assert r.returncode == 2, r.stdout + r.stderr
    calls = docker_calls(tmp_path)
    assert not [c for c in calls if " up " in f" {c} " or " down" in c], calls


def test_every_compose_down_in_the_repo_goes_through_compose_teardown() -> None:
    """What: no script runs `compose ... down` except through compose_teardown. Use when: CI. Inputs: tracked files.
    Output: assertions. Side effects: runs git ls-files. Role: a new teardown site cannot bypass the guard."""
    root = HERE.parent
    files = subprocess.run(["git", "ls-files", "*.sh"], cwd=root, check=True, capture_output=True, text=True).stdout.split()
    offenders = []
    for rel in files:
        if rel == "scripts/lib/compose_owner.sh":
            continue
        for n, line in enumerate((root / rel).read_text().splitlines(), 1):
            code = line.split("#", 1)[0]
            if " down" in code and ("compose" in code or "dc " in code) and "compose_teardown" not in code \
                    and not code.strip().startswith(("echo", "die")):
                offenders.append(f"{rel}:{n}: {line.strip()}")
    assert offenders == []


def test_fake_env_resolves_relative_dirs_so_a_cd_cannot_reach_the_real_docker(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """What: fake_env given a relative dir still yields absolute fakes, and a script that cd's elsewhere still runs the
    fake docker. Use when: CI. Inputs: tmp. Output: assertions. Side effects: fakes only. Role: the probe failure mode."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "rel").mkdir()
    env = fake_env(Path("rel"))
    r = subprocess.run(["bash", "-c", "cd / && docker ps -a"], env=env, capture_output=True, text=True)
    assert r.returncode == 0 and docker_calls(Path("rel")) == ["ps -a"]
