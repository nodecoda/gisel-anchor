"""`gisel-anchor <verb>` is the one entry point; the old scripts stay."""

import os
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def run(*args):
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(
        p for p in (str(ROOT / "src"), env.get("PYTHONPATH", "")) if p)
    return subprocess.run([sys.executable, "-m", "gisel_anchor.cli", *args],
                          cwd=ROOT, env=env, capture_output=True, text=True)


class EntryPoint(unittest.TestCase):
    def test_version(self):
        done = run("--version")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertRegex(done.stdout, r"^gisel-anchor \d+\.\d+\.\d+\n$")

    def test_help_lists_both_verbs(self):
        done = run("--help")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("import", done.stdout)
        self.assertIn("oracle", done.stdout)

    def test_no_arguments_is_a_usage_error(self):
        done = run()
        self.assertEqual(done.returncode, 2)
        self.assertIn("usage: gisel-anchor", done.stdout)

    def test_unknown_verb_is_a_usage_error(self):
        done = run("nope")
        self.assertEqual(done.returncode, 2)
        self.assertIn("unknown verb", done.stderr)


if __name__ == "__main__":
    unittest.main()
