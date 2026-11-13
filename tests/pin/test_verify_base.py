"""The pin base verifier is the only place that hashes a base tree."""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

VERIFIER = (Path(__file__).resolve().parents[2] / "scripts" / "pin"
            / "verify-base.py")


def sha(data):
    return hashlib.sha256(data).hexdigest()


class VerifyBaseTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.tree = root / "tree"
        (self.tree / "llvm").mkdir(parents=True)
        (self.tree / "llvm/a.cpp").write_bytes(b"alpha")
        (self.tree / "llvm/b.cpp").write_bytes(b"beta")
        self.pin = root / "PIN.json"
        self.pin.write_text(json.dumps({
            "commit": "0" * 40,
            "official_base": {
                "head": "0" * 12,
                "files": {
                    "llvm/a.cpp": sha(b"alpha"),
                    "llvm/b.cpp": sha(b"beta"),
                },
            },
        }))

    def tearDown(self):
        self.tmp.cleanup()

    def run_verifier(self, *args):
        return subprocess.run(
            [sys.executable, str(VERIFIER), *args],
            capture_output=True, text=True)

    def test_verify_accepts_a_matching_tree(self):
        proc = self.run_verifier(str(self.tree), str(self.pin), "--verify")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("verified 2 file(s)", proc.stdout)

    def test_verify_reports_expected_and_actual(self):
        (self.tree / "llvm/a.cpp").write_bytes(b"tampered")
        proc = self.run_verifier(str(self.tree), str(self.pin), "--verify")
        self.assertEqual(proc.returncode, 1)
        self.assertIn("expected " + sha(b"alpha"), proc.stderr)
        self.assertIn("got " + sha(b"tampered"), proc.stderr)

    def test_verify_reports_a_missing_file(self):
        (self.tree / "llvm/b.cpp").unlink()
        proc = self.run_verifier(str(self.tree), str(self.pin), "--verify")
        self.assertEqual(proc.returncode, 1)
        self.assertIn("llvm/b.cpp", proc.stderr)
        self.assertIn("missing", proc.stderr)

    def test_verify_without_a_map_is_a_usage_error(self):
        self.pin.write_text(json.dumps({"commit": "0" * 40, "sources": {}}))
        proc = self.run_verifier(str(self.tree), str(self.pin), "--verify")
        self.assertEqual(proc.returncode, 2)
        self.assertIn("no official_base.files", proc.stderr)

    def test_unsafe_path_is_rejected(self):
        self.pin.write_text(json.dumps({
            "commit": "0" * 40,
            "official_base": {"files": {"/etc/passwd": sha(b"x")}},
        }))
        proc = self.run_verifier(str(self.tree), str(self.pin), "--verify")
        self.assertEqual(proc.returncode, 2)
        self.assertIn("unsafe path", proc.stderr)

    def test_emit_json_is_the_actual_map(self):
        proc = self.run_verifier(str(self.tree), str(self.pin), "--emit-json")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(json.loads(proc.stdout), {
            "llvm/a.cpp": sha(b"alpha"),
            "llvm/b.cpp": sha(b"beta"),
        })

    def test_default_mode_prints_and_succeeds(self):
        proc = self.run_verifier(str(self.tree), str(self.pin))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("llvm/a.cpp", proc.stdout)


if __name__ == "__main__":
    unittest.main()
