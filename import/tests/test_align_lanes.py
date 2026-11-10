"""`align_lanes`: the pad-to-a-multiple half of `clampMaxNumElementsStrict`.

`LegalizerInfo.h:1327` defines the strict clamp as `alignNumElementsTo` followed
by `clampMaxNumElements`.  The language keeps both rules, because the evaluator
has to apply both mutations; only the C++ emitter recognises the pair.
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

SOURCE = """\
type T_s32 = LLT::scalar(32)
type T_v2s32 = LLT::fixed_vector(2, 32)
type T_v3s32 = LLT::fixed_vector(3, 32)
type T_v4s32 = LLT::fixed_vector(4, 32)
type T_v6s32 = LLT::fixed_vector(6, 32)

ruleset T_G_ADD: G_ADD
    legal in(slot(0), [T_s32, T_v4s32])
    align_lanes(0, T_s32, 4)
    clamp_max(0, T_s32, 4)
"""

GATED = """\
feature T_big = Subtarget.hasBigVecs()
type T_s32 = LLT::scalar(32)
type T_v8s32 = LLT::fixed_vector(8, 32)

ruleset T_G_ADD: G_ADD
    align_lanes(0, T_s32, 4) when T_big
    clamp_max(0, T_s32, 4) when T_big
"""


def run(*args):
    return subprocess.run([sys.executable, "-m", "gisel", *args],
                          cwd=ROOT, capture_output=True, text=True)


class Semantics(unittest.TestCase):
    """A vector whose lanes are not a multiple is padded, then clamped."""

    def answers(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            src = d / "t.gisel"
            src.write_text(SOURCE)
            doc, cases, out = d / "doc.json", d / "q.jsonl", d / "a.jsonl"
            r = run("gen", str(src), "--emit=core", "--out", str(doc))
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            queries = [("s32", None), ("v4s32", None),
                       ("v2s32", "<4 x s32>"), ("v3s32", "<4 x s32>"),
                       ("v6s32", "<8 x s32>")]
            cases.write_text("\n".join(json.dumps(
                {"config": "baseline", "name": "G_ADD", "types": [t],
                 "mem": None, "imms": []}) for t, _ in queries) + "\n")
            r = run("eval", "--doc", str(doc), "--features", str(FEATURES),
                    "--cases", str(cases), "--answers", str(out))
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            got = [json.loads(line) for line in out.read_text().splitlines()]
        return {row["types"][0]: row for row in got}, queries

    def test_unaligned_vector_moves_to_the_next_multiple(self):
        got, queries = self.answers()
        for name, new_type in queries:
            row = got[name]
            if new_type is None:
                self.assertEqual(row["action"], "Legal", name)
            else:
                self.assertEqual(
                    (row["action"], row["new_type"]), ("MoreElements", new_type),
                    name)

    def test_inc_calls_align_to(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            src, inc = d / "t.gisel", d / "t.inc"
            src.write_text(SOURCE)
            r = run("gen", str(src), "--emit=inc", "--out", str(inc))
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            text = inc.read_text()
        self.assertIn("llvm::alignTo(", text)
        self.assertIn("LLT::fixed_vector(", text)


class GatedClamp(unittest.TestCase):
    """`clamp_* ... when P` is how a constructor `if` reaches a clamp."""

    def test_the_gate_survives_into_the_core_rule(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            src, doc = d / "t.gisel", d / "doc.json"
            src.write_text(GATED)
            r = run("gen", str(src), "--emit=core", "--out", str(doc))
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            rules = json.loads(doc.read_text())["rulesets"][0]["rules"]
        self.assertEqual(len(rules), 2)
        for rule in rules:
            self.assertEqual(rule["action"] in ("MoreElements", "FewerElements"),
                             True)
            self.assertIn("Subtarget.hasBigVecs()",
                          json.dumps(rule["predicate"]))

    def test_it_lints(self):
        with tempfile.TemporaryDirectory() as d:
            src = Path(d) / "t.gisel"
            src.write_text(GATED)
            r = run("lint", str(src))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)


class Importer(unittest.TestCase):
    SRC = ROOT / "import" / "tests" / "fixtures" / "legalizer-ctor-strict-clamp.cpp"

    def test_strict_clamp_becomes_two_statements(self):
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "T.gisel"
            r = subprocess.run(
                [sys.executable, str(TOOL), "RISCV", str(self.SRC),
                 "--out", str(out)], cwd=ROOT, capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            text = out.read_text()
        self.assertIn("1 rulesets, 4 rules, 0 refused", r.stdout)
        self.assertIn("align_lanes(0, R_S16, 2)", text)
        self.assertIn("clamp_max(0, R_S16, 2)", text)
