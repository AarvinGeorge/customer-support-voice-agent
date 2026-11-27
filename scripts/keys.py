"""What: local API-key helper. Sets keys into .env with hidden input, shows which are set (masked),
and verifies each with one read-only API call.
Use when: adding or rotating a key on a dev machine. On the VM, the same names are rendered into the
server env from GitHub Actions secrets at deploy (D-008).
Inputs: subcommand (status | set [NAME] | verify | gen); key values typed at a hidden prompt,
never as arguments (they would land in shell history).
Output: masked status lines; verification results as HTTP status meanings. Never prints a key.
Side effects: writes development/.env (mode 0600); verify sends GET requests to provider APIs.
Role: the single entry point for secrets on a dev machine (12-factor config in the environment)."""
from __future__ import annotations

import getpass
import os
import secrets
import sys
import tempfile
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

ENV_PATH = Path(__file__).resolve().parent.parent / ".env"


@dataclass(frozen=True)
class KeySpec:
    """What: description of one secret the app needs.
    Use when: adding a new key to the registry. Inputs: fields below. Output: none.
    Side effects: none. Role: one row of the key registry (names follow contract doc 07 section 1.4)."""

    name: str
    purpose: str
    prefixes: tuple[str, ...]  # accepted prefixes; empty means no prefix check
    source: str  # "provider" (a person pastes it) or "generated" (random, made by gen)
    verify_url: str | None = None


REGISTRY: tuple[KeySpec, ...] = (
    KeySpec("ANTHROPIC_API_KEY", "Agent model and Gate 2 verifier (Claude)", ("sk-ant-",), "provider",
            "https://api.anthropic.com/v1/models?limit=1"),
    KeySpec("OPENAI_API_KEY", "Voice (Realtime), embeddings, fallback model, eval judge", ("sk-",), "provider",
            "https://api.openai.com/v1/models"),
    KeySpec("LANGSMITH_API_KEY", "LangSmith tracing and drawer reads (service key)",
            ("lsv2_sk_", "lsv2_pt_"), "provider", "https://api.smith.langchain.com/api/v1/sessions?limit=1"),
    KeySpec("NOTION_TOKEN", "Read-only Notion internal connection API token for the help-center source (D-034)",
            ("ntn_", "secret_"), "provider", "https://api.notion.com/v1/users/me"),
    KeySpec("CORE_TOKEN_AGENT", "Agent tool wrappers to core (no commit scope)", (), "generated"),
    KeySpec("CORE_TOKEN_COMMIT", "Agent ingress confirmation code to core (commit scope)", (), "generated"),
    KeySpec("CORE_TOKEN_OPS", "Agent ops router to core (approvals)", (), "generated"),
    KeySpec("KNOWLEDGE_TOKEN_AGENT", "Agent and local evals to knowledge", (), "generated"),
    KeySpec("SESSION_SIGNING_KEY", "Signs the visitor access cookie", (), "generated"),
    KeySpec("PIN_PEPPER", "Secret pepper for PIN hashes in core", (), "generated"),
    KeySpec("OPERATOR_TOKEN_KEY", "Signs core-issued operator tokens", (), "generated"),
    KeySpec("DEMO_ACCESS_CODE", "Gate on the demo URL (shared with reviewers)", (), "generated"),
    KeySpec("OPERATOR_ACCESS_CODE", "Gate on the operator approvals page", (), "generated"),
    KeySpec("POSTGRES_PASSWORD", "Local and VM Postgres password (Compose)", (), "generated"),
)
SPECS = {s.name: s for s in REGISTRY}


def read_env(path: Path) -> dict[str, str]:
    """What: parse KEY=VALUE lines from an env file, skipping comments and blanks.
    Use when: reading current state. Inputs: path. Output: dict of name to value.
    Side effects: reads the file. Role: env file reader."""
    out: dict[str, str] = {}
    if not path.exists():
        return out
    for line in path.read_text().splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        name, _, value = stripped.partition("=")
        out[name.strip()] = value.strip()
    return out


def upsert_env(path: Path, name: str, value: str) -> None:
    """What: set NAME=value in the env file, replacing an existing line or appending one.
    Use when: storing a key. Inputs: path, name, value (no newlines). Output: none.
    Side effects: atomic rewrite of the file with mode 0600. Role: the only writer of .env."""
    if "\n" in value or "\r" in value:
        raise ValueError("value must be a single line")
    lines = path.read_text().splitlines(keepends=True) if path.exists() else []
    new_line = f"{name}={value}\n"
    replaced = False
    for i, line in enumerate(lines):
        if line.strip().startswith(f"{name}="):
            lines[i] = new_line
            replaced = True
            break
    if not replaced:
        if lines and not lines[-1].endswith("\n"):
            lines[-1] += "\n"
        lines.append(new_line)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".env.tmp")
    try:
        with os.fdopen(fd, "w") as f:
            f.writelines(lines)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def format_problem(name: str, value: str) -> str | None:
    """What: check a value's shape for its slot (prefix, length, no whitespace).
    Use when: before saving a key. Inputs: key name, value. Output: problem text, or None if fine.
    Side effects: none. Role: catches a key pasted into the wrong slot."""
    if any(c.isspace() for c in value):
        return "contains whitespace"
    if len(value) < 20 and name not in ("DEMO_ACCESS_CODE", "OPERATOR_ACCESS_CODE"):
        return "too short to be a real key"
    spec = SPECS.get(name)
    if spec and spec.prefixes and not value.startswith(spec.prefixes):
        return f"expected it to start with {' or '.join(spec.prefixes)}"
    if name == "OPENAI_API_KEY" and value.startswith("sk-ant-"):
        return "this looks like an Anthropic key"
    return None


def mask(value: str) -> str:
    """What: safe display of a secret. Use when: printing status. Inputs: value.
    Output: 'MISSING' or 'set (ends ...abcd)'. Side effects: none. Role: never leak a key."""
    return "MISSING" if not value else f"set (ends ...{value[-4:]})"


def verify(spec: KeySpec, value: str) -> str:
    """What: one read-only GET to the provider to confirm the key works.
    Use when: after setting a key. Inputs: spec, value. Output: a plain-words result.
    Side effects: one outbound GET request. Role: proves a key, never mutates anything."""
    if not spec.verify_url:
        return "no online check for this key"
    headers = {"User-Agent": "cs-voice-agent-keys/1"}
    if spec.name == "ANTHROPIC_API_KEY":
        headers |= {"x-api-key": value, "anthropic-version": "2023-06-01"}
    elif spec.name == "NOTION_TOKEN":
        headers |= {"Authorization": f"Bearer {value}", "Notion-Version": "2026-03-11"}
    elif spec.name == "LANGSMITH_API_KEY":
        headers |= {"x-api-key": value}
    else:
        headers |= {"Authorization": f"Bearer {value}"}
    req = urllib.request.Request(spec.verify_url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return f"OK (HTTP {resp.status})"
    except urllib.error.HTTPError as e:
        meaning = {401: "key rejected", 403: "key lacks permission"}.get(e.code, "unexpected response")
        return f"FAIL (HTTP {e.code}, {meaning})"
    except urllib.error.URLError as e:
        return f"FAIL (network: {e.reason})"


def cmd_status() -> int:
    """What: print each registry key as MISSING or masked. Use when: checking setup.
    Inputs: none. Output: exit 1 if any key is missing. Side effects: reads .env. Role: subcommand."""
    env = read_env(ENV_PATH)
    missing_now = 0
    for spec in REGISTRY:
        state = mask(env.get(spec.name, ""))
        if state == "MISSING":
            missing_now += 1
        print(f"{spec.name:26} {state:22} [{spec.source}] {spec.purpose}")
    return 1 if missing_now else 0


def cmd_set(names: list[str]) -> int:
    """What: prompt (hidden) for each named key, or every provider key, and save it.
    Use when: adding or rotating keys. Inputs: key names. Output: exit 0 on success.
    Side effects: writes .env. Role: subcommand."""
    targets = names or [s.name for s in REGISTRY if s.source == "provider"]
    for name in targets:
        if name not in SPECS:
            print(f"Unknown key {name}. Known: {', '.join(SPECS)}")
            return 2
        value = getpass.getpass(f"{name} ({SPECS[name].purpose}), Enter to skip: ").strip()
        if not value:
            print(f"  skipped {name}")
            continue
        problem = format_problem(name, value)
        if problem:
            print(f"  not saved: {problem}")
            continue
        upsert_env(ENV_PATH, name, value)
        print(f"  saved {name}: {mask(value)}")
    return 0


def cmd_verify() -> int:
    """What: run the read-only check for every key that is set. Use when: after cmd_set.
    Inputs: none. Output: exit 1 if any check fails. Side effects: GET requests. Role: subcommand."""
    env = read_env(ENV_PATH)
    failures = 0
    checked = 0
    for spec in REGISTRY:
        value = env.get(spec.name, "")
        if not value or not spec.verify_url:
            continue
        checked += 1
        result = verify(spec, value)
        failures += result.startswith("FAIL")
        print(f"{spec.name:26} {result}")
    if checked == 0:
        print("No keys with an online check are set. Nothing was verified.")
        return 1
    return 1 if failures else 0


def generate_missing(path: Path) -> list[str]:
    """What: create a random value for every generated secret that is not set yet.
    Use when: first setup, or after adding a new generated secret. Inputs: env file path.
    Output: names that were created (empty if nothing was missing); never overwrites a set value.
    Side effects: writes the env file. Role: generator for internal secrets."""
    env = read_env(path)
    made = []
    for spec in REGISTRY:
        if spec.source != "generated" or env.get(spec.name):
            continue
        short = spec.name in ("DEMO_ACCESS_CODE", "OPERATOR_ACCESS_CODE")
        upsert_env(path, spec.name, secrets.token_urlsafe(9 if short else 32))
        made.append(spec.name)
    return made


def cmd_gen() -> int:
    """What: generate missing internal secrets and say which. Use when: first setup.
    Inputs: none. Output: exit 0. Side effects: writes .env. Role: subcommand."""
    made = generate_missing(ENV_PATH)
    print("Generated: " + (", ".join(made) if made else "nothing (all already set)"))
    return 0


def main(argv: list[str]) -> int:
    """What: CLI dispatcher. Use when: running the script. Inputs: argv.
    Output: exit code. Side effects: per subcommand. Role: entry point."""
    if not argv or argv[0] in {"-h", "--help"}:
        print(__doc__)
        print("Usage: python3 scripts/keys.py status | set [NAME ...] | verify | gen")
        return 0
    cmd, rest = argv[0], argv[1:]
    if cmd == "status":
        return cmd_status()
    if cmd == "set":
        return cmd_set(rest)
    if cmd == "verify":
        return cmd_verify()
    if cmd == "gen":
        return cmd_gen()
    print(f"Unknown command {cmd}")
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
