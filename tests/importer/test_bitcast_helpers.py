"""`bitcast` targets computed from the query.

Three shapes: a named helper that inlines a `static LLT` function with an
early return, a helper that builds a vector of 32-bit elements, and an inline
lambda whose element width is selected by the query.  The evaluator applies
the same type arithmetic, so the answers can be compared with C++.
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

REGISTER = ("bitcast(slot(0), replace(select((size_of(slot(0)) <= 32), "
            "int_of_size(size_of(slot(0))), vec_of(udiv(size_of(slot(0)), "
            "32), int_of_size(32)))))")
VECTOR32 = ("bitcast(slot(0), replace(scalar_or_vec_of(udiv(size_of(slot(0)), "
            "32), int_of_size(32))))")
INLINE = ("bitcast(slot(1), replace(vec_of(udiv(size_of(slot(1)), "
          "select((urem(size_of(slot(0)), 64) == 0), 64, 32)), "
          "int_of_size(select((urem(size_of(slot(0)), 64) == 0), 64, 32)))))")

SOURCE = """\
type T_v4s8 = LLT::fixed_vector(4, 8)
type T_v8s8 = LLT::fixed_vector(8, 8)
type T_v2s16 = LLT::fixed_vector(2, 16)

ruleset T_G_ADD: G_ADD
    %s

ruleset T_G_SUB: G_SUB
    %s

ruleset T_G_MUL: G_MUL
    %s when size_of(slot(0)) > 64
""" % (REGISTER, VECTOR32, INLINE)


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
            queries = [
                ("G_ADD", ["v4s8"]), ("G_ADD", ["v8s8"]),
                ("G_SUB", ["v8s8"]), ("G_SUB", ["v2s16"]),
                ("G_MUL", ["s128", "v4s128"]), ("G_MUL", ["s96", "v4s96"]),
            ]
            cases.write_text("\n".join(json.dumps(
                {"config": "baseline", "name": n, "types": t,
                 "mem": None, "imms": []}) for n, t in queries) + "\n")
            r = run("eval", "--doc", str(doc), "--features", str(FEATURES),
                    "--cases", str(cases), "--answers", str(out))
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            got = [json.loads(line) for line in out.read_text().splitlines()]
        return {(row["name"], tuple(row["types"])): row for row in got}

    def test_register_type_splits_at_32_bits(self):
        got = self.answers()
        # <4 x i8> -> i32, <8 x i8> -> <2 x i32>
        self.assertEqual(got[("G_ADD", ("v4s8",))]["new_type"], "i32")
        self.assertEqual(got[("G_ADD", ("v8s8",))]["new_type"], "<2 x i32>")

    def test_vector_of_32bit_elements(self):
        got = self.answers()
        self.assertEqual(got[("G_SUB", ("v8s8",))]["new_type"], "<2 x i32>")
        self.assertEqual(got[("G_SUB", ("v2s16",))]["new_type"], "i32")

    def test_inline_lambda_selects_the_element_width(self):
        got = self.answers()
        # 128-bit elements -> 64-bit elements; 96-bit -> 32-bit elements.
        self.assertEqual(got[("G_MUL", ("s128", "v4s128"))]["new_type"],
                         "<8 x i64>")
        self.assertEqual(got[("G_MUL", ("s96", "v4s96"))]["new_type"],
                         "<12 x i32>")


class Emitter(unittest.TestCase):
    def test_inc_has_the_type_arithmetic(self):
        with tempfile.TemporaryDirectory() as d:
            src, inc = Path(d) / "t.gisel", Path(d) / "t.inc"
            src.write_text(SOURCE)
            r = run("gen", str(src), "--emit=inc", "--out", str(inc))
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            text = inc.read_text()
        self.assertIn(".coreBitcastToType(", text)
        self.assertIn("LLT::integer(", text)
        self.assertIn("LLT::scalarOrVector(ElementCount::getFixed(", text)


class Importer(unittest.TestCase):
    SRC = ROOT / "tests" / "importer" / "fixtures" / "legalizer-ctor-bitcast-helpers.cpp"

    def test_named_and_inline_helpers_are_read(self):
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "A.gisel"
            r = subprocess.run(
                [sys.executable, str(TOOL), "--probe", "AMDGPU:Amdgpu",
                 "--src", str(self.SRC), "--out", str(out)],
                cwd=ROOT, capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            text = out.read_text()
        self.assertIn("0 refused", r.stdout)
        self.assertIn(REGISTER, text)
        self.assertIn(VECTOR32, text)
        self.assertIn(INLINE, text)


if __name__ == "__main__":
    unittest.main()
