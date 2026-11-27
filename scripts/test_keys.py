"""What: unit tests for scripts/keys.py (env upsert, format checks, masking, file permissions).
Use when: run in CI and before changing keys.py. Inputs: none. Output: unittest results.
Side effects: writes temp files only. Role: guards the secret-handling helper."""
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import keys  # noqa: E402


class UpsertTests(unittest.TestCase):
    """What: tests for writing keys into an env file.
    Use when: changing upsert_env. Inputs: none. Output: assertions.
    Side effects: temp files. Role: protects existing lines and secrecy of the file."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = Path(self.dir.name) / ".env"

    def tearDown(self):
        self.dir.cleanup()

    def test_replaces_existing_key_and_keeps_other_lines(self):
        self.path.write_text("# comment\nLANGSMITH_API_KEY=\nLANGSMITH_PROJECT=cs-voice-agent\n")
        keys.upsert_env(self.path, "LANGSMITH_API_KEY", "lsv2_sk_abc123")
        text = self.path.read_text()
        self.assertIn("LANGSMITH_API_KEY=lsv2_sk_abc123\n", text)
        self.assertIn("# comment\n", text)
        self.assertIn("LANGSMITH_PROJECT=cs-voice-agent\n", text)
        self.assertEqual(text.count("LANGSMITH_API_KEY="), 1)

    def test_appends_missing_key(self):
        self.path.write_text("A=1\n")
        keys.upsert_env(self.path, "OPENAI_API_KEY", "sk-proj-xyz")
        self.assertEqual(self.path.read_text(), "A=1\nOPENAI_API_KEY=sk-proj-xyz\n")

    def test_creates_file_owner_only(self):
        keys.upsert_env(self.path, "ANTHROPIC_API_KEY", "sk-ant-xyz")
        mode = stat.S_IMODE(os.stat(self.path).st_mode)
        self.assertEqual(mode, 0o600)

    def test_rejects_newline_in_value(self):
        with self.assertRaises(ValueError):
            keys.upsert_env(self.path, "OPENAI_API_KEY", "sk-a\nEVIL=1")

    def test_generate_fills_missing_and_never_overwrites(self):
        self.path.write_text("CORE_TOKEN_AGENT=keepme-already-set-1234567890\n")
        made = keys.generate_missing(self.path)
        env = keys.read_env(self.path)
        self.assertEqual(env["CORE_TOKEN_AGENT"], "keepme-already-set-1234567890")
        self.assertNotIn("CORE_TOKEN_AGENT", made)
        self.assertIn("PIN_PEPPER", made)
        self.assertGreaterEqual(len(env["PIN_PEPPER"]), 32)
        self.assertNotEqual(env["CORE_TOKEN_COMMIT"], env["CORE_TOKEN_OPS"])
        self.assertEqual(keys.generate_missing(self.path), [])

    def test_reads_values(self):
        self.path.write_text("A=1\n# B=2\nC=\n")
        self.assertEqual(keys.read_env(self.path), {"A": "1", "C": ""})


class FormatTests(unittest.TestCase):
    """What: tests for per-key format checks and masking.
    Use when: changing the key registry. Inputs: none. Output: assertions.
    Side effects: none. Role: catches pasting the wrong key into the wrong slot."""

    def test_prefix_checks(self):
        self.assertIsNone(keys.format_problem("LANGSMITH_API_KEY", "lsv2_sk_" + "a" * 30))
        self.assertIsNotNone(keys.format_problem("LANGSMITH_API_KEY", "sk-ant-" + "a" * 30))
        self.assertIsNone(keys.format_problem("ANTHROPIC_API_KEY", "sk-ant-" + "a" * 30))
        self.assertIsNotNone(keys.format_problem("OPENAI_API_KEY", "sk-ant-" + "a" * 30))
        self.assertIsNone(keys.format_problem("OPENAI_API_KEY", "sk-proj-" + "a" * 30))

    def test_rejects_whitespace_and_short(self):
        self.assertIsNotNone(keys.format_problem("OPENAI_API_KEY", "sk- abc"))
        self.assertIsNotNone(keys.format_problem("OPENAI_API_KEY", "sk-abc"))

    def test_registry_matches_contract_and_drops_old_vendors(self):
        names = set(keys.SPECS)
        for n in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "LANGSMITH_API_KEY", "CORE_TOKEN_AGENT",
                  "CORE_TOKEN_COMMIT", "CORE_TOKEN_OPS", "KNOWLEDGE_TOKEN_AGENT", "SESSION_SIGNING_KEY",
                  "PIN_PEPPER", "OPERATOR_TOKEN_KEY", "DEMO_ACCESS_CODE", "OPERATOR_ACCESS_CODE",
                  "POSTGRES_PASSWORD"):
            self.assertIn(n, names)
        for old in ("UPSTASH_REDIS_REST_URL", "UPSTASH_REDIS_REST_TOKEN", "DATABASE_URL"):
            self.assertNotIn(old, names)

    def test_only_provider_keys_are_typed_by_a_person(self):
        typed = {s.name for s in keys.REGISTRY if s.source == "provider"}
        self.assertEqual(typed, {"ANTHROPIC_API_KEY", "OPENAI_API_KEY", "LANGSMITH_API_KEY"})

    def test_mask_never_shows_more_than_last_four(self):
        self.assertEqual(keys.mask("lsv2_sk_abcdefghijkl"), "set (ends ...ijkl)")
        self.assertEqual(keys.mask(""), "MISSING")


if __name__ == "__main__":
    unittest.main()
