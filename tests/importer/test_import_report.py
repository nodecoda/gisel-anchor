"""`transcribe()` must hand back the same facts the report is written from."""

from __future__ import annotations

import hashlib
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TOOL = ROOT / "scripts" / "import-cxx.py"
FIXTURE = ROOT / "tests" / "importer" / "fixtures" / "legalizer-ctor.cpp"


class ReportShape(unittest.TestCase):
    @staticmethod
    def _module():
        from gisel_anchor.importer import import_cxx as module
        return module

    def test_report_hashes_the_text_it_returns(self):
        module = self._module()
        spec = module.Target("RISCV", "R", "", 0, 0, pending=True)
        code, text, report = module.transcribe(spec, FIXTURE, None)
        self.assertEqual(report["target"], "RISCV")
        self.assertEqual(report["corpus_sha256"],
                         hashlib.sha256(text.encode()).hexdigest())
        self.assertEqual(report["corpus_name"], "RISCVLegalizerInfo.gisel")
        self.assertEqual(report["counts"]["rules"], report["rules"])
        self.assertEqual(report["counts"]["refused"], len(report["refused"]))
        self.assertNotIn("pinned", report)
        self.assertNotIn("drift", report)

    def test_a_pinned_target_reports_drift_instead_of_hiding_it(self):
        module = self._module()
        spec = module.Target("RISCV", "R", "", 99, 999)
        _, _, report = module.transcribe(spec, FIXTURE, None)
        self.assertEqual(report["pinned"], {"rulesets": 99, "rules": 999})
        self.assertTrue(report["drift"])


if __name__ == "__main__":
    unittest.main()
