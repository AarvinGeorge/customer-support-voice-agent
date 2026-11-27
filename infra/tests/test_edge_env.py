"""What: tests that an unset SITE_ADDRESS cannot silently serve plain http on the VM (review PR #11 I1).
Use when: editing Caddyfile, compose.yaml or compose.override.yaml, and in CI (needs `docker`; the Caddy test pulls caddy:2-alpine).
Inputs: none. Run: python3 -m unittest infra/tests/test_edge_env.py from the repo root.
Output: unittest result. Side effects: runs `docker compose config` and one throwaway `docker run caddy validate`.
Role: proves the VM form (`-f compose.yaml`) hands Caddy an empty address, that Caddy refuses to start on it, and that local dev still defaults to :80."""
import json
import os
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _caddy_env(*compose_args: str, **extra: str) -> str | None:
    """What: render the stack and return the SITE_ADDRESS the caddy service would get.
    Use when: comparing the VM form and the local form. Inputs: extra compose args, env overrides.
    Output: the value (empty string when unset). Side effects: one docker compose subprocess. Role: test helper."""
    env = {k: v for k, v in os.environ.items() if k != "SITE_ADDRESS"} | extra
    out = subprocess.run(
        ["docker", "compose", *compose_args, "--env-file", "/dev/null", "config", "--format", "json"],
        cwd=ROOT, env=env, capture_output=True, text=True, check=True,
    ).stdout
    return json.loads(out)["services"]["caddy"]["environment"]["SITE_ADDRESS"]


class SiteAddressTests(unittest.TestCase):
    """What: the SITE_ADDRESS contract. Use when: unittest discovery. Inputs: none. Output: assertions.
    Side effects: docker subprocesses. Role: fail loudly instead of serving http on the VM."""

    def test_vm_form_has_no_silent_default(self):
        self.assertEqual(_caddy_env("-f", "compose.yaml"), "")

    def test_local_form_defaults_to_plain_http(self):
        self.assertEqual(_caddy_env(), ":80")

    def test_explicit_value_passes_through(self):
        self.assertEqual(_caddy_env("-f", "compose.yaml", SITE_ADDRESS="demo.example.com"), "demo.example.com")

    @staticmethod
    def _validate(*env_args: str) -> subprocess.CompletedProcess:
        """What: run `caddy validate` on the repo Caddyfile in a throwaway container. Use when: testing address handling.
        Inputs: docker `-e` args. Output: the completed process. Side effects: one docker run. Role: test helper."""
        return subprocess.run(
            ["docker", "run", "--rm", *env_args, "-v", f"{ROOT}/Caddyfile:/etc/caddy/Caddyfile:ro",
             "caddy:2.11-alpine", "caddy", "validate", "--config", "/etc/caddy/Caddyfile"],
            capture_output=True, text=True,
        )

    def test_caddy_accepts_a_set_address(self):
        """Positive control: proves the refusal test below can tell the difference."""
        res = self._validate("-e", "SITE_ADDRESS=:80")
        self.assertEqual(res.returncode, 0, res.stderr[-300:])

    def test_caddy_refuses_an_unset_or_empty_address(self):
        for args in ((), ("-e", "SITE_ADDRESS=")):
            res = self._validate(*args)
            self.assertNotEqual(res.returncode, 0, args)
            self.assertIn("without any key", res.stderr + res.stdout)


if __name__ == "__main__":
    unittest.main()
