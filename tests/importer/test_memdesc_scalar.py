"""`getScalarTypeFromMemDesc(i)`: a mutation whose target width is the mem type.

The helper body reads `Query.MMODescrs[0].MemoryTy`, not `Query.Types[i]`.
The mutation is `replace(int_of_size(size_of(mem_ty(mem_slot(0)))))`; the
predicate lambda reads the same descriptor through `mem_ty(mem_slot(0))`.
"""

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TOOL = ROOT / "scripts" / "import-cxx.py"
FIXTURE = ROOT / "tests" / "importer" / "fixtures" / "legalizer-ctor-memdesc-scalar.cpp"

MUTATION = ("narrow(slot(0), "
            "replace(int_of_size(size_of(mem_ty(mem_slot(0)))))) when "
            "(sort_of(mem_ty(mem_slot(0))) == scalar and "
            "size_of(mem_ty(mem_slot(0))) > 32 and "
            "size_of(slot(0)) > size_of(mem_ty(mem_slot(0))))")


def run(*args):
    return subprocess.run([sys.executable, *args],
                          cwd=ROOT, capture_output=True, text=True)


class Importer(unittest.TestCase):
    def test_the_helper_reads_the_memory_type(self):
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "A.gisel"
            r = run(str(TOOL), "--probe", "AMDGPU:Amdgpu",
                    "--src", str(FIXTURE), "--out", str(out))
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            self.assertIn("0 refused", r.stdout)
            text = out.read_text()
        self.assertIn(MUTATION, text)


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
            queries = [("s96", "s48"), ("s32", "s64")]
            cases.write_text("\n".join(json.dumps(
                {"config": "baseline", "name": "G_SEXTLOAD",
                 "types": [t, "p0"], "mem": [m, 8, "NotAtomic", "NotAtomic"],
                 "imms": []}) for t, m in queries) + "\n")
            r = run("-m", "gisel", "eval", "--doc", str(doc),
                    "--features", str(features), "--cases", str(cases),
                    "--answers", str(out))
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            got = [json.loads(line) for line in out.read_text().splitlines()]
        return {row["mem"][0]: row for row in got}

    def test_narrows_to_the_memory_width(self):
        got = self.answers()
        self.assertEqual(got["s48"]["action"], "NarrowScalar")
        self.assertEqual(got["s48"]["new_type"], "i48")

    def test_the_guard_needs_a_wide_result(self):
        got = self.answers()
        # 32 is not wider than the 64-bit memory type: the narrow rule is
        # skipped and the terminal `lower` answers instead.
        self.assertEqual(got["s64"]["action"], "Lower")


class Emitter(unittest.TestCase):
    def test_inc_narrows_to_the_memory_size(self):
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
        self.assertIn(".coreNarrowTo(", text)
        self.assertIn("LLT::integer(((Query.MMODescrs[0]).MemoryTy)", text)
        self.assertIn("MemoryTy.isScalar()", text)


if __name__ == "__main__":
    unittest.main()
