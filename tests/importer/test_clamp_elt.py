"""`clampNumElements(i, V16S32, V32S32)` reads the element type of a vector.

The vector constants are named in upper case (`Amdgpu_V16S32`); the element
name must come out as the declared scalar (`Amdgpu_S32`), not a synthesized
`Amdgpu_s32`.
"""

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TOOL = ROOT / "scripts" / "import-cxx.py"
FIXTURE = ROOT / "tests" / "importer" / "fixtures" / "legalizer-ctor-clamp-elt.cpp"


def run(*args):
    return subprocess.run([sys.executable, *args],
                          cwd=ROOT, capture_output=True, text=True)


class Importer(unittest.TestCase):
    def test_element_names_are_the_declared_scalars(self):
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "A.gisel"
            r = run(str(TOOL), "--probe", "AMDGPU:Amdgpu",
                    "--src", str(FIXTURE), "--out", str(out))
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            self.assertIn("0 refused", r.stdout)
            text = out.read_text()
        self.assertIn(
            "clamp_min(0, Amdgpu_S32, num_elems(ty(Amdgpu_V16S32)))", text)
        self.assertIn(
            "clamp_max(0, Amdgpu_S32, num_elems(ty(Amdgpu_V32S32)))", text)
        self.assertIn(
            "clamp_min(0, Amdgpu_S64, num_elems(ty(Amdgpu_V2S64)))", text)
        self.assertNotIn("Amdgpu_s32", text)


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
            queries = ["v8s32", "v64s32"]
            cases.write_text("\n".join(json.dumps(
                {"config": "baseline", "name": "G_IMPLICIT_DEF",
                 "types": [t], "mem": None, "imms": []})
                for t in queries) + "\n")
            r = run("-m", "gisel", "eval", "--doc", str(doc),
                    "--features", str(features), "--cases", str(cases),
                    "--answers", str(out))
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            got = [json.loads(line) for line in out.read_text().splitlines()]
        return {row["types"][0]: row for row in got}

    def test_too_few_lanes_grow_to_16(self):
        row = self.answers()["v8s32"]
        self.assertEqual((row["action"], row["new_type"]),
                         ("MoreElements", "<16 x s32>"))

    def test_too_many_lanes_drop_to_32(self):
        row = self.answers()["v64s32"]
        self.assertEqual((row["action"], row["new_type"]),
                         ("FewerElements", "<32 x s32>"))


class Emitter(unittest.TestCase):
    def test_inc_keeps_the_vector_constant(self):
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
        self.assertIn(".clampMinNumElements(0, Amdgpu_S32, "
                      "(Amdgpu_V16S32).getNumElements())", text)
        self.assertIn(".clampMaxNumElements(0, Amdgpu_S32, "
                      "(Amdgpu_V32S32).getNumElements())", text)


if __name__ == "__main__":
    unittest.main()
