"""`widen(slot(i), multiple_of(N))`: round up to a multiple, not the next pow2.

`LegalizerInfo.h:989` defines `widenScalarToNextMultipleOf` as
`changeElementSize(alignTo(ssz, N))`.  The language already had the
`multiple_of` value primitive; this locks its widening form and the importer.
"""

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TOOL = ROOT / "importer" / "import-cxx.py"
FEATURES = ROOT / "importer" / "tests" / "features.json"

SOURCE = """\
type T_s16 = LLT::scalar(16)
type T_s24 = LLT::scalar(24)
type T_s32 = LLT::scalar(32)

ruleset T_G_ADD: G_ADD
    legal in(slot(0), [T_s32])
    widen(slot(0), multiple_of(32))
"""


def run(*args):
    return subprocess.run([sys.executable, "-m", "gisel", *args],
                          cwd=ROOT, capture_output=True, text=True)


class Semantics(unittest.TestCase):
    def answers(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            src = d / "t.gisel"
            src.write_text(SOURCE)
            doc, cases, out = d / "doc.json", d / "q.jsonl", d / "a.jsonl"
            r = run("gen", str(src), "--emit=core", "--out", str(doc))
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            names = ["s16", "s24", "s32"]
            cases.write_text("\n".join(json.dumps(
                {"config": "baseline", "name": "G_ADD", "types": [t],
                 "mem": None, "imms": []}) for t in names) + "\n")
            r = run("eval", "--doc", str(doc), "--features", str(FEATURES),
                    "--cases", str(cases), "--answers", str(out))
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            got = [json.loads(line) for line in out.read_text().splitlines()]
        return {row["types"][0]: row for row in got}

    def test_rounds_up_to_the_multiple(self):
        got = self.answers()
        # 16 -> 32 and 24 -> 32: alignTo, not pow2ceil (24 -> 32 here, but a
        # width such as 8 would also go to 32 rather than staying at 8).
        for name in ("s16", "s24"):
            self.assertEqual(
                (got[name]["action"], got[name]["new_type"]),
                ("WidenScalar", "s32"), name)

    def test_an_already_aligned_scalar_is_not_mutated(self):
        got = self.answers()
        self.assertEqual(got["s32"]["action"], "Legal")


class Emitter(unittest.TestCase):
    def test_inc_calls_the_core_shift(self):
        with tempfile.TemporaryDirectory() as d:
            src, inc = Path(d) / "t.gisel", Path(d) / "t.inc"
            src.write_text(SOURCE)
            r = run("gen", str(src), "--emit=inc", "--out", str(inc))
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            text = inc.read_text()
        self.assertIn("coreWidenScalarToNextMultipleOf(", text)
        self.assertIn(", 0, 32)", text)


class Importer(unittest.TestCase):
    SRC = ROOT / "importer" / "tests" / "fixtures" / "legalizer-ctor-widen-multiple.cpp"

    def test_mutation_is_read_as_multiple_of(self):
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "A.gisel"
            r = subprocess.run(
                [sys.executable, str(TOOL), "--probe", "AMDGPU:Amdgpu",
                 "--src", str(self.SRC), "--out", str(out)],
                cwd=ROOT, capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            text = out.read_text()
        self.assertIn("widen(slot(0), multiple_of(32))", text)
        self.assertIn("0 refused", r.stdout)


if __name__ == "__main__":
    unittest.main()
