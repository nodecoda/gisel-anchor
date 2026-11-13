"""`numElementsNotPow2` keeps upstream's `isFixedVector()` half.

LLVM's predicate is `isFixedVector() && !isPowerOf2(getNumElements())`.  The
translation used to drop the first half, so it also fired on scalable vectors,
whose known-min count (e.g. 3) is not a power of two.  U-full found the
difference: 400 SPIR-V queries answered `FewerElements` where upstream said
`Custom`.
"""

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TOOL = ROOT / "scripts" / "import-cxx.py"
FIXTURE = (ROOT / "tests" / "importer" / "fixtures"
           / "legalizer-ctor-count-predicates.cpp")


def run(*args):
    return subprocess.run([sys.executable, *args],
                          cwd=ROOT, capture_output=True, text=True)


class CountPredicates(unittest.TestCase):
    def test_scalarize_requires_a_fixed_vector(self):
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "X.gisel"
            r = run(str(TOOL), "--probe", "X86:X", "--src", str(FIXTURE),
                    "--out", str(out))
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            text = out.read_text()
        self.assertIn(
            "scalarize(slot(0)) when (is_fixed_vector(slot(0)) and "
            "not is_pow2(num_elems(slot(0))))", text)
        # The sibling predicate already had the guard; both must agree.
        self.assertIn("is_fixed_vector(slot(0)) and num_elems(slot(0)) > 4",
                      text)


if __name__ == "__main__":
    unittest.main()
