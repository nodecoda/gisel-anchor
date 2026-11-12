"""make-base must apply the deviation patches, not just the PR patches.

The fixture is a three-commit repository: pin, a PR, and a local deviation.
A run that stops after `patches` produces "patched", so the final content is
the assertion; the manifest's file hash is checked by --verify as well.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "pin" / "make-base.sh"


def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args],
                          capture_output=True, text=True, check=True).stdout


class MakeBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.repo = self.root / "llvm"
        self.repo.mkdir()
        git(self.repo, "init", "-q")
        git(self.repo, "config", "user.name", "fixture")
        git(self.repo, "config", "user.email", "fixture@localhost")

        (self.repo / "a.txt").write_text("base\n")
        git(self.repo, "add", "a.txt")
        git(self.repo, "commit", "-q", "-m", "pin")
        self.pin = git(self.repo, "rev-parse", "HEAD").strip()

        (self.repo / "a.txt").write_text("patched\n")
        git(self.repo, "commit", "-q", "-am", "pr")
        self.patch = git(self.repo, "rev-parse", "HEAD").strip()

        (self.repo / "a.txt").write_text("deviation\n")
        git(self.repo, "commit", "-q", "-am", "deviation")
        self.final = git(self.repo, "rev-parse", "HEAD").strip()

        self.pin_dir = self.root / "pin"
        (self.pin_dir / "deviations").mkdir(parents=True)
        diff = subprocess.run(
            ["git", "-C", str(self.repo), "diff", self.patch, self.final,
             "--", "a.txt"], capture_output=True, text=True, check=True).stdout
        patch = self.pin_dir / "deviations" / "a.patch"
        patch.write_text(diff)
        self.manifest = {
            "commit": self.pin,
            "patches": [{"commit": self.patch, "pr": 1, "title": "fixture"}],
            "official_base": {
                "deviations": [{
                    "path": "a.txt",
                    "patch": "deviations/a.patch",
                    "patch_sha256": hashlib.sha256(
                        patch.read_bytes()).hexdigest(),
                }],
                "files": {"a.txt": hashlib.sha256(
                    b"deviation\n").hexdigest()},
            },
        }
        self.pin_json = self.pin_dir / "PIN.json"
        self.pin_json.write_text(json.dumps(self.manifest, indent=2) + "\n")

    def tearDown(self):
        self.tmp.cleanup()

    def run_make(self, dest):
        env = dict(os.environ, PIN=str(self.pin_json),
                   PIN_DIR=str(self.pin_dir))
        return subprocess.run(
            ["bash", str(SCRIPT), str(self.repo), str(dest), "--verify"],
            capture_output=True, text=True, env=env)

    def test_deviations_are_applied_after_the_pr_patches(self):
        dest = self.root / "base"
        proc = self.run_make(dest)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual((dest / "a.txt").read_text(), "deviation\n")
        self.assertIn("pin: deviation a.patch", proc.stderr)
        self.assertIn("verified 1 file(s)", proc.stdout)

    def test_a_tampered_patch_stops_the_build(self):
        doc = json.loads(self.pin_json.read_text())
        doc["official_base"]["deviations"][0]["patch_sha256"] = "0" * 64
        self.pin_json.write_text(json.dumps(doc, indent=2) + "\n")
        proc = self.run_make(self.root / "base-bad")
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("sha256", proc.stderr)


if __name__ == "__main__":
    unittest.main()
