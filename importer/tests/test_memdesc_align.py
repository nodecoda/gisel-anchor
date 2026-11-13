"""memdesc align: a literal, a configuration-chosen integer, an expression.

`GlobalAlign8 = ST.hasUnalignedBufferAccessEnabled() ? 0 : 8` is two tables
(one per configuration), not one integer.  The importer splits the table on
the gate, and folds arithmetic such as `2 * 8` to 16.  The evaluator then
picks the table the configuration names.
"""

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TOOL = ROOT / "importer" / "import-cxx.py"
FIXTURE = ROOT / "importer" / "tests" / "fixtures" / "legalizer-ctor-memdesc-align.cpp"

FEATURES = {
    "unaligned": {"ST.hasUnalignedBufferAccessEnabled()": True},
    "aligned": {"ST.hasUnalignedBufferAccessEnabled()": False},
}


def run(*args):
    return subprocess.run([sys.executable, *args],
                          cwd=ROOT, capture_output=True, text=True)


class Importer(unittest.TestCase):
    def imported(self):
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "A.gisel"
            r = run(str(TOOL), "--probe", "AMDGPU:Amdgpu",
                    "--src", str(FIXTURE), "--out", str(out))
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            self.assertIn("0 refused", r.stdout)
            return out.read_text()

    def test_the_table_is_split_on_the_gate(self):
        text = self.imported()
        self.assertIn(
            "feature Amdgpu_ST_hasUnalignedBufferAccessEnabled = "
            "ST.hasUnalignedBufferAccessEnabled()", text)
        self.assertIn(
            "legal (Amdgpu_ST_hasUnalignedBufferAccessEnabled and "
            "in_mem_desc(", text)
        self.assertIn(
            "legal (not Amdgpu_ST_hasUnalignedBufferAccessEnabled and "
            "in_mem_desc(", text)
        self.assertIn(
            "mem_desc(Amdgpu_S32, Amdgpu_GlobalPtr, Amdgpu_S8, 0)", text)
        self.assertIn(
            "mem_desc(Amdgpu_S32, Amdgpu_GlobalPtr, Amdgpu_S8, 8)", text)

    def test_an_integer_expression_is_folded(self):
        text = self.imported()
        # `2 * 8` is the same row under both configurations.
        self.assertEqual(text.count(
            "mem_desc(Amdgpu_S32, Amdgpu_GlobalPtr, Amdgpu_S16, 16)"), 2)
        self.assertNotIn("2 * 8", text)


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
            features.write_text(json.dumps(FEATURES))
            queries = [
                ("unaligned", ["s8", 4]),
                ("aligned", ["s8", 4]),
                ("aligned", ["s8", 8]),
                ("aligned", ["s16", 16]),
            ]
            cases.write_text("\n".join(json.dumps(
                {"config": c, "name": "G_LOAD",
                 "types": ["s32", "p1"], "mem": [t, a, "NotAtomic",
                                                 "NotAtomic"],
                 "imms": []}) for c, (t, a) in queries) + "\n")
            r = run("-m", "gisel", "eval", "--doc", str(doc),
                    "--features", str(features), "--cases", str(cases),
                    "--answers", str(out))
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            got = [json.loads(line) for line in out.read_text().splitlines()]
        return {(row["config"], row["mem"][0], row["mem"][1]): row["action"]
                for row in got}

    def test_the_configuration_picks_the_align(self):
        got = self.answers()
        # Unaligned access: align 0 accepts an align-4 query.  Otherwise the
        # table requires 8, and nothing else matches.
        self.assertEqual(got[("unaligned", "s8", 4)], "Legal")
        self.assertEqual(got[("aligned", "s8", 4)], "Unsupported")
        self.assertEqual(got[("aligned", "s8", 8)], "Legal")

    def test_the_folded_row_is_the_same_in_both_configurations(self):
        got = self.answers()
        self.assertEqual(got[("aligned", "s16", 16)], "Legal")


class Emitter(unittest.TestCase):
    def test_inc_keeps_the_memdesc_table_and_the_guard(self):
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
        self.assertIn(
            "bool Amdgpu_ST_hasUnalignedBufferAccessEnabled = "
            "ST.hasUnalignedBufferAccessEnabled();", text)
        self.assertEqual(text.count(
            "LegalityPredicates::typePairAndMemDescInSet("), 2)
        self.assertIn("{Amdgpu_S32, Amdgpu_GlobalPtr, Amdgpu_S8, 0}", text)
        self.assertIn("{Amdgpu_S32, Amdgpu_GlobalPtr, Amdgpu_S8, 8}", text)
        self.assertIn("{Amdgpu_S32, Amdgpu_GlobalPtr, Amdgpu_S16, 16}", text)


if __name__ == "__main__":
    unittest.main()
