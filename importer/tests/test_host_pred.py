"""Host expressions inside a rule predicate, copied through `opaque(...)`.

Three shapes are covered: a host helper inside a conjunction, a bool lambda
declared *inside* the constructor (the generated `.inc` replaces that body),
and a `LegalityPredicate` helper whose slot index is a loop variable.  Each
one must leave a self-contained C++ string behind; a leftover constructor
local would not compile after the constructor swap.
"""

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TOOL = ROOT / "importer" / "import-cxx.py"
FIXTURE = ROOT / "importer" / "tests" / "fixtures" / "legalizer-ctor-host-pred.cpp"


def run(*args):
    return subprocess.run([sys.executable, *args],
                          cwd=ROOT, capture_output=True, text=True)


class Importer(unittest.TestCase):
    def corpus(self, d):
        out = Path(d) / "A.gisel"
        r = run(str(TOOL), "--probe", "AMDGPU:Amdgpu",
                "--src", str(FIXTURE), "--out", str(out))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("0 refused", r.stdout)
        return out.read_text()

    def setUp(self):
        self._d = tempfile.TemporaryDirectory()
        self.text = self.corpus(self._d.name)

    def tearDown(self):
        self._d.cleanup()

    def test_slot_index_is_folded(self):
        self.assertIn(
            'opaque("SIRegisterInfo::getSGPRClassForBitWidth'
            '(Query.Types[1].getSizeInBits())")', self.text)
        self.assertIn(
            'opaque("SIRegisterInfo::getSGPRClassForBitWidth'
            '(Query.Types[0].getSizeInBits())")', self.text)

    def test_predicate_helper_index_is_folded(self):
        self.assertIn('opaque("isIllegalRegisterType(ST, 1)(Query)")',
                      self.text)
        self.assertIn('opaque("isIllegalRegisterType(ST, 0)(Query)")',
                      self.text)

    def test_constructor_local_is_inlined(self):
        self.assertIn('opaque("([&]() -> bool { return true && '
                      'Query.MMODescrs[0].MemoryTy.getSizeInBits() > 64; '
                      '}())")', self.text)
        self.assertNotIn("needToSplit(", self.text)
        self.assertNotIn("IsLoad", self.text)


class Semantics(unittest.TestCase):
    def test_a_host_predicate_is_a_hole_not_false(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            src, doc = d / "A.gisel", d / "doc.json"
            cases, out = d / "q.jsonl", d / "a.jsonl"
            r = run(str(TOOL), "--probe", "AMDGPU:Amdgpu",
                    "--src", str(FIXTURE), "--out", str(src))
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            r = run("-m", "gisel", "gen", str(src), "--emit=core",
                    "--out", str(doc))
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            features = d / "features.json"
            features.write_text(json.dumps({"baseline": {}}))
            cases.write_text("\n".join([
                json.dumps({"config": "baseline", "name": "G_LOAD",
                            "types": ["s32", "p0:64"],
                            "mem": ["s32", 32], "imms": []}),
                json.dumps({"config": "baseline",
                            "name": "G_EXTRACT_VECTOR_ELT",
                            "types": ["s32", "v2s32", "s32"],
                            "mem": None, "imms": []}),
            ]) + "\n")
            r = run("-m", "gisel", "eval", "--doc", str(doc),
                    "--features", str(features), "--cases", str(cases),
                    "--answers", str(out))
            got = [json.loads(line) for line in out.read_text().splitlines()]
        self.assertEqual([row["status"] for row in got],
                         ["unanswerable", "unanswerable"])
        self.assertTrue(all("opaque" in row["reason"] for row in got))


if __name__ == "__main__":
    unittest.main()
