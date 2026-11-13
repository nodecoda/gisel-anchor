"""The AMDGPU lane-count mutation helpers, read by body shape.

`oneMoreElement`, `moreEltsToNext32Bit` and `fewerEltsToSize64Vector` are
`static LegalizeMutation F(unsigned i)` functions whose lambdas are a few
local declarations plus `return pair(i, <LLT expr>)`.  The importer expands
the expression; the evaluator then applies the same lane arithmetic.
"""

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TOOL = ROOT / "scripts" / "import-cxx.py"
FEATURES = ROOT / "tests" / "importer" / "features.json"

ONE_MORE = ("more(slot(0), replace(vec_of(add(num_elems(slot(0)), 1), "
            "elem_of(slot(0)))))")
NEXT_32 = ("more(slot(0), replace(vec_of(udiv(sub(add(mul(32, "
           "udiv(add(size_of(slot(0)), 31), 32)), "
           "size_of(elem_of(slot(0)))), 1), "
           "size_of(elem_of(slot(0)))), elem_of(slot(0)))))")
SPLIT_64 = ("fewer(slot(0), replace(scalar_or_vec_of(udiv("
            "add(num_elems(slot(0)), 1), udiv(add(size_of(slot(0)), 63), "
            "64)), elem_of(slot(0)))))")

SOURCE = """\
type T_v2s16 = LLT::fixed_vector(2, 16)
type T_v3s16 = LLT::fixed_vector(3, 16)
type T_v8s16 = LLT::fixed_vector(8, 16)
type T_v3s8 = LLT::fixed_vector(3, 8)

ruleset T_G_ADD: G_ADD
    legal in(slot(0), [T_v2s16])
    more(slot(0), replace(vec_of(add(num_elems(slot(0)), 1), elem_of(slot(0)))))

ruleset T_G_SUB: G_SUB
    more(slot(0), replace(vec_of(udiv(sub(add(mul(32, udiv(add(size_of(slot(0)), 31), 32)), size_of(elem_of(slot(0)))), 1), size_of(elem_of(slot(0)))), elem_of(slot(0)))))

ruleset T_G_MUL: G_MUL
    fewer(slot(0), replace(scalar_or_vec_of(udiv(add(num_elems(slot(0)), 1), udiv(add(size_of(slot(0)), 63), 64)), elem_of(slot(0))))) when sort_of(slot(0)) == vector and size_of(slot(0)) > 64
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
            queries = [("G_ADD", "v3s16"), ("G_ADD", "v8s16"),
                       ("G_SUB", "v3s8"), ("G_SUB", "v3s16"),
                       ("G_MUL", "v8s16"), ("G_MUL", "v3s16")]
            cases.write_text("\n".join(json.dumps(
                {"config": "baseline", "name": n, "types": [t],
                 "mem": None, "imms": []}) for n, t in queries) + "\n")
            r = run("eval", "--doc", str(doc), "--features", str(FEATURES),
                    "--cases", str(cases), "--answers", str(out))
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            got = [json.loads(line) for line in out.read_text().splitlines()]
        return {(row["name"], row["types"][0]): row for row in got}

    def test_one_more_element(self):
        got = self.answers()
        self.assertEqual((got[("G_ADD", "v3s16")]["action"],
                          got[("G_ADD", "v3s16")]["new_type"]),
                         ("MoreElements", "<4 x s16>"))

    def test_next_32bit_multiple(self):
        got = self.answers()
        self.assertEqual(got[("G_SUB", "v3s8")]["new_type"], "<4 x s8>")
        # 3 x 16 = 48 -> ceil(48/32)*32 = 64 -> 4 lanes.
        self.assertEqual(got[("G_SUB", "v3s16")]["new_type"], "<4 x s16>")

    def test_split_to_size_64(self):
        got = self.answers()
        # 8 x 16 = 128 bits -> two 64-bit pieces -> 4 lanes.
        self.assertEqual(got[("G_MUL", "v8s16")]["new_type"], "<4 x s16>")

    def test_the_guard_keeps_small_vectors_out(self):
        got = self.answers()
        self.assertEqual(got[("G_MUL", "v3s16")]["action"], "Unsupported")


class Emitter(unittest.TestCase):
    def test_inc_keeps_the_lane_arithmetic(self):
        with tempfile.TemporaryDirectory() as d:
            src, inc = Path(d) / "t.gisel", Path(d) / "t.inc"
            src.write_text(SOURCE)
            r = run("gen", str(src), "--emit=inc", "--out", str(inc))
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            text = inc.read_text()
        self.assertIn(".coreMoreTo(", text)
        self.assertIn("getKnownMinValue() : 0) + 1", text)
        self.assertIn("LLT::scalarOrVector(ElementCount::getFixed(", text)


class Importer(unittest.TestCase):
    SRC = ROOT / "tests" / "importer" / "fixtures" / "legalizer-ctor-lane-helpers.cpp"

    def test_helper_bodies_become_lane_expressions(self):
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "A.gisel"
            r = subprocess.run(
                [sys.executable, str(TOOL), "--probe", "AMDGPU:Amdgpu",
                 "--src", str(self.SRC), "--out", str(out)],
                cwd=ROOT, capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            text = out.read_text()
        self.assertIn("0 refused", r.stdout)
        self.assertIn(ONE_MORE, text)
        self.assertIn(NEXT_32, text)
        self.assertIn(SPLIT_64, text)


if __name__ == "__main__":
    unittest.main()
