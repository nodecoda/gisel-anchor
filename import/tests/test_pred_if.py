"""`minScalarIf(P, i, T)`, and a predicate reading a file-scope integer.

`minScalarIf` widens the slot to `T` when `P` holds and the slot is narrower;
it is the guarded form of `minScalar`.  A file-scope `constexpr unsigned` in a
predicate is its value (`MaxRegisterSize` -> 1024), not an opaque name.
"""

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TOOL = ROOT / "import" / "import-cxx.py"
FIXTURE = ROOT / "import" / "tests" / "fixtures" / "legalizer-ctor-pred-if.cpp"


def run(*args):
    return subprocess.run([sys.executable, *args],
                          cwd=ROOT, capture_output=True, text=True)


class Importer(unittest.TestCase):
    def test_guarded_min_scalar_and_file_constant(self):
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "A.gisel"
            r = run(str(TOOL), "--probe", "AMDGPU:Amdgpu",
                    "--src", str(FIXTURE), "--out", str(out))
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            self.assertIn("0 refused", r.stdout)
            text = out.read_text()
        self.assertIn("widen(slot(0), ty(Amdgpu_I16)) when slot(1) == Amdgpu_F16",
                      text)
        self.assertIn("legal size_of(slot(0)) <= 1024", text)


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
            queries = [
                ("G_FPTOSI_SAT", ["i8", "f16"]),
                ("G_FPTOSI_SAT", ["i32", "f32"]),
                ("G_ADD", ["s32"]),
                ("G_ADD", ["s2048"]),
            ]
            cases.write_text("\n".join(json.dumps(
                {"config": "baseline", "name": n, "types": t,
                 "mem": None, "imms": []}) for n, t in queries) + "\n")
            r = run("-m", "gisel", "eval", "--doc", str(doc),
                    "--features", str(features), "--cases", str(cases),
                    "--answers", str(out))
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            got = [json.loads(line) for line in out.read_text().splitlines()]
        return {(row["name"], tuple(row["types"])): row for row in got}

    def test_min_scalar_if_widens_only_under_the_guard(self):
        got = self.answers()
        row = got[("G_FPTOSI_SAT", ("i8", "f16"))]
        self.assertEqual((row["action"], row["new_type"]),
                         ("WidenScalar", "i16"))
        self.assertEqual(got[("G_FPTOSI_SAT", ("i32", "f32"))]["action"],
                         "Legal")

    def test_the_file_constant_is_its_value(self):
        got = self.answers()
        self.assertEqual(got[("G_ADD", ("s32",))]["action"], "Legal")
        self.assertEqual(got[("G_ADD", ("s2048",))]["action"], "Lower")


class Emitter(unittest.TestCase):
    def test_inc_keeps_the_guard_and_the_literal(self):
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
        self.assertIn(".coreMinScalar(", text)
        self.assertIn("getSizeInBits().getKnownMinValue() <= 1024", text)


if __name__ == "__main__":
    unittest.main()
