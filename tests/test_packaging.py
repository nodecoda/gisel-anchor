"""The installed product: two console scripts and a pin that travels with them.

`gisel-anchor` is a producer, not a library: what a user installs must be able
to run `gisel-import` and `gisel-oracle` without a checkout.  The pin is the
one file that makes `gisel-import --git-repo ...` self-sufficient, so it ships
twice -- canonical under `pin/`, a checked copy under
`src/gisel_anchor/importer/data/`.
"""

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SYNC = ROOT / "scripts" / "sync-package-data.py"
TOOL = ROOT / "scripts" / "import-cxx.py"
PIN = ROOT / "pin" / "PIN.json"
BUNDLED = ROOT / "src" / "gisel_anchor" / "importer" / "data" / "PIN.json"


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
        with tempfile.TemporaryDirectory() as tmp:
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
        with tempfile.TemporaryDirectory() as tmp:
            done = subprocess.run(
                [sys.executable, str(TOOL), "X86", "--llvm-src", tmp,
                 "--out", str(Path(tmp) / "x.gisel")],
                cwd=ROOT, capture_output=True, text=True)
        self.assertNotIn("--pin needs --git-repo", done.stdout + done.stderr)
        self.assertNotIn("is not in", done.stdout + done.stderr)


class ConsoleScriptsExist(unittest.TestCase):
    def test_both_entry_points_are_declared(self):
        text = (ROOT / "pyproject.toml").read_text()
        self.assertIn(
            'gisel-import = "gisel_anchor.importer.import_cxx:main"', text)
        self.assertIn(
            'gisel-oracle = "gisel_anchor.oracle.__main__:main"', text)

    def test_the_checkout_script_runs_the_single_implementation(self):
        done = subprocess.run([sys.executable, str(TOOL), "--help"], cwd=ROOT,
                              capture_output=True, text=True)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("--probe", done.stdout)
        self.assertIn("One-shot transcription", done.stdout)


class OneTreeShape(unittest.TestCase):
    """The layout is a contract: one package root, one test root, one bin dir."""

    def test_the_package_lives_under_src(self):
        self.assertTrue((ROOT / "src" / "gisel_anchor" / "__init__.py").is_file())
        for retired in ("importer", "oracle", "tools"):
            self.assertFalse((ROOT / retired).exists(), retired)

    def test_tests_are_not_scattered_across_packages(self):
        for pkg in (ROOT / "src").rglob("tests"):
            self.fail("tests under the package tree: %s" % pkg)

    def test_sdist_can_run_the_tests(self):
        text = (ROOT / "MANIFEST.in").read_text()
        self.assertIn("recursive-include tests", text)
        self.assertIn("recursive-include scripts", text)
        self.assertIn("pin", text)


if __name__ == "__main__":
    unittest.main()
