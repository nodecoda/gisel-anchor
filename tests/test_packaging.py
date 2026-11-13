"""The installed product: two console scripts and a pin that travels with them.

`gisel-anchor` is a producer, not a library: what a user installs must be
able to run `gisel-import` and `gisel-oracle` without a checkout.  The pin is
the one file that makes `gisel-import --git-repo ...` self-sufficient, so it
ships twice -- canonical under `pin/`, a checked copy under `importer/data/`.
"""

import json
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SYNC = ROOT / "tools" / "sync-package-data.py"
TOOL = ROOT / "importer" / "import-cxx.py"
PIN = ROOT / "pin" / "PIN.json"
BUNDLED = ROOT / "importer" / "data" / "PIN.json"


class PinTravelsWithTheCode(unittest.TestCase):
    def test_the_copy_is_in_sync(self):
        done = subprocess.run([sys.executable, str(SYNC), "--check"],
                              cwd=ROOT, capture_output=True, text=True)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("in sync", done.stdout)

    def test_the_wheel_declares_the_pin(self):
        text = (ROOT / "pyproject.toml").read_text()
        self.assertIn('"data/PIN.json"', text)

    def test_both_copies_name_the_same_commit(self):
        canonical = json.loads(PIN.read_text())["commit"]
        bundled = json.loads(BUNDLED.read_text())["commit"]
        self.assertEqual(canonical, bundled)
        self.assertRegex(canonical, r"^[0-9a-f]{40}$")


class TheGitRepoFormNeedsNoPinFlag(unittest.TestCase):
    def test_git_repo_alone_uses_the_bundled_pin(self):
        # /tmp is not a git repository, so the run must fail *after* the pin
        # was read: the error names the pinned commit.  Before the default,
        # the same command died in the argument checks instead.
        with __import__("tempfile").TemporaryDirectory() as tmp:
            done = subprocess.run(
                [sys.executable, str(TOOL), "X86", "--git-repo", tmp,
                 "--out", str(Path(tmp) / "x.gisel")],
                cwd=ROOT, capture_output=True, text=True)
        commit = json.loads(PIN.read_text())["commit"]
        self.assertNotEqual(done.returncode, 0)
        self.assertIn(commit, done.stdout + done.stderr)

    def test_llvm_src_alone_does_not_smuggle_in_the_pin(self):
        # --llvm-src must keep working without --git-repo; a default pin that
        # applies unconditionally would turn this into a "needs --git-repo".
        with __import__("tempfile").TemporaryDirectory() as tmp:
            done = subprocess.run(
                [sys.executable, str(TOOL), "X86", "--llvm-src", tmp,
                 "--out", str(Path(tmp) / "x.gisel")],
                cwd=ROOT, capture_output=True, text=True)
        self.assertNotIn("--pin needs --git-repo", done.stdout + done.stderr)
        self.assertNotIn("is not in", done.stdout + done.stderr)


class ConsoleScriptsExist(unittest.TestCase):
    def test_both_entry_points_are_declared(self):
        text = (ROOT / "pyproject.toml").read_text()
        self.assertIn('gisel-import = "importer.cli:main"', text)
        self.assertIn('gisel-oracle = "oracle.__main__:main"', text)

    def test_the_shim_runs_the_single_implementation(self):
        program = "\n".join([
            "import sys",
            "sys.path.insert(0, %r)" % str(ROOT),
            "from importer import cli",
            "raise SystemExit(cli.main(['--help']) or 0)",
        ])
        done = subprocess.run([sys.executable, "-c", program], cwd=ROOT,
                              capture_output=True, text=True)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("import-cxx.py", done.stdout)


if __name__ == "__main__":
    unittest.main()
