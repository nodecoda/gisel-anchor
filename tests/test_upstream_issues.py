"""The upstream issue list is a claim, not a scratchpad.

Every entry must point at a real issue URL, name a target this corpus knows,
and say whether a front end can reach the input.  A fixed issue must name its
fix, so the list cannot silently keep stale blockers.
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PATH = ROOT / "docs" / "UPSTREAM-ISSUES.json"
TARGETS = {"x86", "aarch64", "riscv", "arm", "mips", "ppc", "m68k", "wasm",
           "spirv"}
STATUS = {"open", "fixed", "wontfix"}
REACHABLE = {"ir", "mir", "none"}


class UpstreamIssues(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.data = json.loads(PATH.read_text())
        cls.issues = cls.data["issues"]

    def test_schema(self):
        self.assertEqual(self.data["schema"], 1)
        self.assertTrue(self.data["note"])

    def test_every_url_names_its_issue(self):
        seen = set()
        for issue in self.issues:
            self.assertRegex(issue["url"],
                             r"^https://github\.com/llvm/llvm-project/issues/\d+$")
            self.assertTrue(issue["url"].endswith("/%d" % issue["id"]))
            self.assertNotIn(issue["id"], seen)
            seen.add(issue["id"])

    def test_enums_and_targets(self):
        for issue in self.issues:
            self.assertIn(issue["target"], TARGETS, issue["id"])
            self.assertIn(issue["status"], STATUS, issue["id"])
            self.assertIn(issue["reachable"], REACHABLE, issue["id"])
            self.assertTrue(issue["title"], issue["id"])
            self.assertTrue(issue["repro"], issue["id"])
            self.assertRegex(issue["filed"], r"^\d{4}-\d{2}-\d{2}$")

    def test_a_fixed_issue_names_its_fix(self):
        for issue in self.issues:
            if issue["status"] == "fixed":
                self.assertTrue(issue.get("fixed_by"), issue["id"])

    def test_an_ir_reachable_issue_is_a_real_front_end_input(self):
        for issue in self.issues:
            if issue["reachable"] == "ir":
                self.assertNotIn("MIR", issue["repro"].upper(), issue["id"])


if __name__ == "__main__":
    unittest.main()
