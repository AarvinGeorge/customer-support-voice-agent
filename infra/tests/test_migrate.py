"""What: unit tests for the Python hook step of infra/migrate.py (no database needed).
Use when: changing migrate.py. Inputs: none. Output: unittest results.
Side effects: temp files only. Role: proves hooks run in order, get the owner URL, support async, and fail loudly."""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import migrate  # noqa: E402


class HookTests(unittest.TestCase):
    """What: tests for run_hooks. Use when: changing hook discovery. Inputs: none. Output: assertions.
    Side effects: temp files. Role: guards the per-service non-transactional hook contract."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = Path(self.dir.name)
        self.log = self.path / "log.txt"

    def tearDown(self):
        self.dir.cleanup()

    def hook(self, name: str, body: str) -> None:
        (self.path / name).write_text(body)

    def test_runs_in_filename_order_with_owner_url(self):
        for n in ("0020_b.py", "0010_a.py"):
            self.hook(n, f"def run(url):\n    open({str(self.log)!r}, 'a').write('{n}:' + url + '\\n')\n")
        ran = migrate.run_hooks("postgresql://owner", self.path)
        self.assertEqual(ran, ["0010_a.py", "0020_b.py"])
        self.assertEqual(self.log.read_text().splitlines(),
                         ["0010_a.py:postgresql://owner", "0020_b.py:postgresql://owner"])

    def test_async_hook_is_awaited(self):
        self.hook("0001_async.py", f"async def run(url):\n    open({str(self.log)!r}, 'w').write('done')\n")
        migrate.run_hooks("u", self.path)
        self.assertEqual(self.log.read_text(), "done")

    def test_no_hooks_is_fine(self):
        self.assertEqual(migrate.run_hooks("u", self.path), [])

    def test_hook_without_run_fails_loudly(self):
        self.hook("0001_bad.py", "x = 1\n")
        with self.assertRaises(RuntimeError):
            migrate.run_hooks("u", self.path)

    def test_hook_exception_propagates(self):
        self.hook("0001_boom.py", "def run(url):\n    raise ValueError('boom')\n")
        with self.assertRaises(ValueError):
            migrate.run_hooks("u", self.path)


if __name__ == "__main__":
    unittest.main()
