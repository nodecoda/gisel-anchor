"""A mutation body with a local reassigned inside nested `if`s.

The value language has no statements, so the reassignment becomes a `select`
on the value of the variable; declarations made inside the block do not
escape it.  The final expression is the same integer C++ computes.
"""

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TOOL = ROOT / "import" / "import-cxx.py"
FIXTURE = ROOT / "import" / "tests" / "fixtures" / "legalizer-ctor-mutation-if.cpp"

RULE = ("widen(slot(0), replace(int_of_size(select(("
        "pow2_ceil(add(size_of(slot(0)), 1)) >= 256), select(("
        "align_to(add(size_of(slot(0)), 1), 64) < "
        "pow2_ceil(add(size_of(slot(0)), 1))), "
        "align_to(add(size_of(slot(0)), 1), 64), "
        "pow2_ceil(add(size_of(slot(0)), 1))), "
        "pow2_ceil(add(size_of(slot(0)), 1)))))) when "
        "urem(size_of(slot(0)), 16) != 0")


def run(*args):
    return subprocess.run([sys.executable, *args],
                          cwd=ROOT, capture_output=True, text=True)


class Importer(unittest.TestCase):
    def test_the_if_becomes_a_select(self):
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "A.gisel"
            r = run(str(TOOL), "--probe", "AMDGPU:Amdgpu",
                    "--src", str(FIXTURE), "--out", str(out))
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            self.assertIn("0 refused", r.stdout)
            text = out.read_text()
        self.assertIn(RULE, text)


class Semantics(unittest.TestCase):
    def answers(self):
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
            queries = ["s40", "s127", "s128", "s200"]
            cases.write_text("\n".join(json.dumps(
                {"config": "baseline", "name": "G_MERGE_VALUES",
                 "types": [t, "s32"], "mem": None, "imms": []})
                for t in queries) + "\n")
            r = run("-m", "gisel", "eval", "--doc", str(doc),
                    "--features", str(features), "--cases", str(cases),
                    "--answers", str(out))
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            got = [json.loads(line) for line in out.read_text().splitlines()]
        return {row["types"][0]: row for row in got}

    def test_next_power_of_two(self):
        got = self.answers()
        self.assertEqual((got["s40"]["action"], got["s40"]["new_type"]),
                         ("WidenScalar", "i64"))
        self.assertEqual((got["s127"]["action"], got["s127"]["new_type"]),
                         ("WidenScalar", "i128"))

    def test_the_256_rule_rounds_to_64(self):
        got = self.answers()
        # 129 -> next multiple of 64 is 192, smaller than 256.
        self.assertEqual(got["s200"]["new_type"], "i256")
        # 128 is already a multiple of 16: the guard does not fire.
        self.assertEqual(got["s128"]["action"], "Lower")


class Emitter(unittest.TestCase):
    def test_inc_keeps_the_integer_arithmetic(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            src, inc = d / "A.gisel", d / "A.inc"
            r = run(str(TOOL), "--probe", "AMDGPU:Amdgpu",
                    "--src", str(FIXTURE), "--out", str(src))
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            r = run("-m", "gisel", "gen", str(src), "--emit=inc",
                    "--out", str(inc))
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            text = inc.read_text()
        self.assertIn("PowerOf2Ceil", text)
        self.assertIn("llvm::alignTo((", text)


if __name__ == "__main__":
    unittest.main()
