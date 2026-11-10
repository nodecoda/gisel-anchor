"""Mutation bodies the constructor passes to `widenScalarIf`/`fewerElementsIf`.

`scalarize`, `changeElementSizeTo` and `widenScalarOrEltToNextPow2` are
mutations, not builders: the predicate is the whole guard, so the evaluator
must not add an implicit `sizeNotPow2` on top.  The last one is spelled as a
`replace(elt_size_to(...))` for exactly that reason.
"""

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TOOL = ROOT / "import" / "import-cxx.py"
FEATURES = ROOT / "import" / "tests" / "features.json"

WIDEN = ("widen(slot(0), replace(elt_size_to(slot(0), "
         "pow2_at_least(bits_of(scalar_of(slot(0))), 16)))) when "
         "urem(size_of(slot(0)), 16) != 0")
SCALARIZE = ("fewer(slot(0), replace(elem_of(slot(0)))) when "
             "sort_of(slot(0)) == vector")
SIZE_TO = ("widen(slot(0), replace(elt_size_to(slot(0), "
           "size_of(ty(T_s32))))) when bits_of(slot(0)) < 32")
SIZE_TO_IMPORTED = ("widen(slot(0), replace(elt_size_to(slot(0), "
                    "size_of(ty(Amdgpu_S32))))) when size_of(slot(0)) < 32")
WIDEN_IMPORTED = ("widen(slot(1), replace(elt_size_to(slot(1), "
                  "pow2_at_least(bits_of(scalar_of(slot(1))), 16)))) when "
                  "urem(size_of(slot(1)), 16) != 0")

SOURCE = """\
type T_s32 = LLT::scalar(32)
type T_v3s8 = LLT::fixed_vector(3, 8)
type T_v3s16 = LLT::fixed_vector(3, 16)

ruleset T_G_ADD: G_ADD
    %s

ruleset T_G_SUB: G_SUB
    %s

ruleset T_G_MUL: G_MUL
    %s
""" % (WIDEN, SCALARIZE, SIZE_TO)


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
                ("G_ADD", ["s8"]), ("G_ADD", ["s24"]), ("G_ADD", ["v3s8"]),
                ("G_SUB", ["v3s16"]), ("G_SUB", ["s16"]),
                ("G_MUL", ["s16"]), ("G_MUL", ["v3s16"]),
            ]
            cases.write_text("\n".join(json.dumps(
                {"config": "baseline", "name": n, "types": t,
                 "mem": None, "imms": []}) for n, t in queries) + "\n")
            r = run("eval", "--doc", str(doc), "--features", str(FEATURES),
                    "--cases", str(cases), "--answers", str(out))
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            got = [json.loads(line) for line in out.read_text().splitlines()]
        return {(row["name"], tuple(row["types"])): row for row in got}

    def test_next_pow2_at_least_min_has_no_implicit_guard(self):
        got = self.answers()
        # 8 is a power of two but still widens because Min is 16.
        self.assertEqual(got[("G_ADD", ("s8",))]["new_type"], "s16")
        self.assertEqual(got[("G_ADD", ("s24",))]["new_type"], "s32")
        self.assertEqual(got[("G_ADD", ("v3s8",))]["new_type"], "<3 x s16>")

    def test_scalarize_uses_the_element_type(self):
        got = self.answers()
        row = got[("G_SUB", ("v3s16",))]
        self.assertEqual((row["action"], row["new_type"]),
                         ("FewerElements", "s16"))

    def test_change_element_size_to_a_named_type(self):
        got = self.answers()
        self.assertEqual(got[("G_MUL", ("s16",))]["new_type"], "s32")
        self.assertEqual(got[("G_MUL", ("v3s16",))]["new_type"], "<3 x s32>")


LANE_SOURCE = """\
type T_v2s32 = LLT::fixed_vector(2, 32)
type T_v4s32 = LLT::fixed_vector(4, 32)
type T_v8s32 = LLT::fixed_vector(8, 32)

ruleset T_G_AND: G_AND
    fewer(slot(0), 4) when num_elems(slot(0)) > 4

ruleset T_G_OR: G_OR
    more(slot(0), 8) when num_elems(slot(0)) < 8
"""


class LaneCount(unittest.TestCase):
    """`fewer`/`more` to an exact lane count has no implicit guard.

    `changeElementCountTo(i, EC)` runs whenever the rule predicate holds; the
    predicate already spells `num_elems` when the C++ does.
    """

    def answers(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            src = d / "t.gisel"
            src.write_text(LANE_SOURCE)
            doc, cases, out = d / "doc.json", d / "q.jsonl", d / "a.jsonl"
            r = run("gen", str(src), "--emit=core", "--out", str(doc))
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            queries = [("G_AND", ["v8s32"]), ("G_OR", ["v2s32"])]
            cases.write_text("\n".join(json.dumps(
                {"config": "baseline", "name": n, "types": t,
                 "mem": None, "imms": []}) for n, t in queries) + "\n")
            r = run("eval", "--doc", str(doc), "--features", str(FEATURES),
                    "--cases", str(cases), "--answers", str(out))
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            got = [json.loads(line) for line in out.read_text().splitlines()]
        return {row["name"]: row for row in got}

    def test_fewer_to_an_exact_count(self):
        row = self.answers()["G_AND"]
        self.assertEqual((row["action"], row["new_type"]),
                         ("FewerElements", "<4 x s32>"))

    def test_more_to_an_exact_count(self):
        row = self.answers()["G_OR"]
        self.assertEqual((row["action"], row["new_type"]),
                         ("MoreElements", "<8 x s32>"))


class Emitter(unittest.TestCase):
    def test_inc_uses_the_whole_type_shim(self):
        with tempfile.TemporaryDirectory() as d:
            src, inc = Path(d) / "t.gisel", Path(d) / "t.inc"
            src.write_text(SOURCE)
            r = run("gen", str(src), "--emit=inc", "--out", str(inc))
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            text = inc.read_text()
        self.assertIn(".coreWidenTo(", text)
        self.assertIn("getScalarType()", text)
        self.assertIn("std::max(llvm::PowerOf2Ceil(", text)


class Importer(unittest.TestCase):
    SRC = ROOT / "import" / "tests" / "fixtures" / "legalizer-ctor-mutation-forms.cpp"

    def test_variable_indices_and_mutation_bodies(self):
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "A.gisel"
            r = subprocess.run(
                [sys.executable, str(TOOL), "--probe", "AMDGPU:Amdgpu",
                 "--src", str(self.SRC), "--out", str(out)],
                cwd=ROOT, capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            text = out.read_text()
        self.assertIn("0 refused", r.stdout)
        self.assertIn(WIDEN_IMPORTED, text)
        self.assertIn(SCALARIZE, text)
        self.assertIn(SIZE_TO_IMPORTED, text)


if __name__ == "__main__":
    unittest.main()
