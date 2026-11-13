"""`{{S32}, {S64}}`: nested braces are a type set for slot 0, not a pair.

C++ overload resolution picks `legalFor(initializer_list<LLT>)` here, which is
`typeInSet(typeIdx(0), {S32, S64})`.  A two- or three-element inner brace is
the pair/tuple overload and stays `in_pairs`/`in_triples`.
"""

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TOOL = ROOT / "scripts" / "import-cxx.py"
FIXTURE = ROOT / "tests" / "importer" / "fixtures" / "legalizer-ctor-nested-set.cpp"


def run(*args):
    return subprocess.run([sys.executable, *args],
                          cwd=ROOT, capture_output=True, text=True)


def import_fixture(out):
    r = run(str(TOOL), "--probe", "AMDGPU:Amdgpu",
            "--src", str(FIXTURE), "--out", str(out))
    return r


class Importer(unittest.TestCase):
    def test_single_element_rows_flatten_to_a_slot_0_set(self):
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "A.gisel"
            r = import_fixture(out)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            self.assertIn("0 refused", r.stdout)
            text = out.read_text()
        self.assertIn("legal in(slot(0), [Amdgpu_S32, Amdgpu_S64])", text)
        self.assertIn("lower in(slot(0), [Amdgpu_V2S16])", text)
        self.assertIn(
            "lower in(slot(0), [Amdgpu_S32, Amdgpu_S64, Amdgpu_S16])", text)
        self.assertNotIn("in_pairs", text)


class Semantics(unittest.TestCase):
    def answers(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            src, doc = d / "A.gisel", d / "doc.json"
            cases, out = d / "q.jsonl", d / "a.jsonl"
            r = import_fixture(src)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            r = run("-m", "gisel", "gen", str(src), "--emit=core",
                    "--out", str(doc))
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            features = d / "features.json"
            features.write_text(json.dumps({"baseline": {}}))
            queries = [("G_SEXT_INREG", "s32"), ("G_SEXT_INREG", "s64"),
                       ("G_SEXT_INREG", "s16"), ("G_ADD", "s16"),
                       ("G_ADD", "s64")]
            cases.write_text("\n".join(json.dumps(
                {"config": "baseline", "name": n, "types": [t],
                 "mem": None, "imms": []}) for n, t in queries) + "\n")
            r = run("-m", "gisel", "eval", "--doc", str(doc),
                    "--features", str(features), "--cases", str(cases),
                    "--answers", str(out))
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            got = [json.loads(line) for line in out.read_text().splitlines()]
        return {(row["name"], row["types"][0]): row["action"] for row in got}

    def test_the_set_covers_slot_0(self):
        got = self.answers()
        self.assertEqual(got[("G_SEXT_INREG", "s32")], "Legal")
        self.assertEqual(got[("G_SEXT_INREG", "s64")], "Legal")
        self.assertEqual(got[("G_SEXT_INREG", "s16")], "Unsupported")

    def test_three_singletons_are_one_lower_set(self):
        got = self.answers()
        self.assertEqual(got[("G_ADD", "s16")], "Lower")
        self.assertEqual(got[("G_ADD", "s64")], "Lower")


if __name__ == "__main__":
    unittest.main()
