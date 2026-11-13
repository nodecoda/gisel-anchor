"""`scripts/import-cxx.py`: the C++ constructor -> `.gisel` importer."""

import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
TOOL = ROOT / "scripts" / "import-cxx.py"
CORPUS = Path(os.environ.get(
    "GISEL_CORPUS", ROOT.parent / "gisel" / "tests" / "corpus"))
FIXTURE = ROOT / "tests" / "importer" / "fixtures" / "legalizer-ctor.cpp"
PIN = ROOT / "pin" / "PIN.json"


def _env_path(name):
    """A source tree is opt-in: never fall back to a developer's home."""
    value = os.environ.get(name)
    return Path(value) if value else None


LLVM_GIT = _env_path("GS_LLVM_GIT")
RISCV_SRC = _env_path("GS_RISCV_SRC")
X86_SRC = _env_path("GS_X86_SRC")
AARCH64_SRC = _env_path("GS_AARCH64_SRC")
M68K_SRC = _env_path("GS_M68K_SRC")
PPC_SRC = _env_path("GS_POWERPC_SRC")
WASM_SRC = _env_path("GS_WEBASSEMBLY_SRC")
ARM_SRC = _env_path("GS_ARM_SRC")
MIPS_SRC = _env_path("GS_MIPS_SRC")
SPIRV_SRC = _env_path("GS_SPIRV_SRC")


def run(*args, env=None):
    return subprocess.run([sys.executable, str(TOOL), *args],
                          cwd=ROOT, env=env, capture_output=True, text=True)


def body(text):
    """Drop the generated header, which names the source path."""
    return text.split("//===------", 1)[1]


class Fixture(unittest.TestCase):
    def test_if_else_becomes_two_gated_tables(self):
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "T.gisel"
            r = run("RISCV", str(FIXTURE), "--out", str(out))
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            self.assertIn("0 refused", r.stdout)
            text = out.read_text()
            # `if (ST.hasStdExtZbb()) { legalFor } else { maxScalar }`
            self.assertIn(
                'legal (R_hasStdExtZbb and '
                "in_pairs(slot(0), slot(1), [[R_s32, R_s32]]))", text)
            self.assertIn(
                'narrow(slot(0), ty(R_s32)) when not '
                'R_hasStdExtZbb', text)
            # `minScalar(pred, 0, s32)` is a gated widen, not an unconditional one.
            self.assertIn(
                'widen(slot(0), ty(R_s32)) when '
                'R_hasStdExtZbb', text)
            self.assertIn("lower true", text)

    def test_predicate_first_mutations_are_gated(self):
        # `minScalar(ST.hasStdExtZbb(), 0, sXLen)` in G_ABS: the leading
        # predicate is a gate, not a no-op.  Dropping it would widen in the
        # non-Zbb case too.
        text = (CORPUS / "RISCVLegalizerInfo.gisel").read_text()
        self.assertIn('widen(slot(0), ty(R_sXLen)) when '
                      'R_hasStdExtZbb', text)

    def test_every_statement_is_either_emitted_or_refused(self):
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "T.gisel"
            r = run("RISCV", str(FIXTURE), "--out", str(out), "--stats")
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            self.assertNotIn("refused line", r.stdout)
            # 4 builder calls: legalFor, clampScalar (2 rules), maxScalar, minScalar, lower
            self.assertIn("TOTAL", r.stdout)
            total = [l for l in r.stdout.splitlines() if l.startswith("TOTAL")][0]
            self.assertEqual(total.split()[-2:], ["5", "6"])


class ImporterBugsAreRefusals(unittest.TestCase):
    """A parse bug names the line; it never aborts the transcription."""

    CRASH = ROOT / "tests" / "importer" / "fixtures" / "legalizer-ctor-crash.cpp"

    def test_untranslatable_statement_is_refused_with_its_line(self):
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "T.gisel"
            r = run("RISCV", str(self.CRASH), "--out", str(out))
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("1 rulesets, 1 rules, 1 refused", r.stdout)
        self.assertIn("refused line 6: R_G_ADD.someUnknownBuilder", r.stdout)
        self.assertNotIn("Traceback", r.stderr)

    def test_block_reports_the_line_of_a_failing_statement(self):
        from gisel_anchor.importer import import_cxx as module

        original = module.Importer.statement

        def statement(self, text, line):
            if text.startswith("boom"):
                raise RecursionError("too deep")
            return original(self, text, line)

        module.Importer.statement = statement
        try:
            importer = module.Importer("RISCV", "")
            importer.block("const LLT s32 = LLT::scalar(32);\nboom;\n"
                           "const LLT s64 = LLT::scalar(64);", 10)
        finally:
            module.Importer.statement = original
        self.assertEqual([line for line, _ in importer.p.refused], [11])
        self.assertIn("RecursionError", importer.p.refused[0][1])


class ParseGuard(unittest.TestCase):
    """Generated text is a corpus: it must parse, or it is not written."""

    @staticmethod
    def _module():
        from gisel_anchor.importer import import_cxx as module
        return module

    def test_good_text_parses(self):
        module = self._module()
        text = ("include \"llvm/Target/Target.td\"\n\n"
                "ruleset R_G_ADD: G_ADD\n    legal true\n")
        self.assertIsNone(module.parse_problem(text))

    def test_gated_clamp_parses(self):
        # A clamp inside a constructor `if` used to be a refusal because the
        # grammar had no `when` for it.  It has one now; the gate is a normal
        # conjunct of the desugared rule's predicate.
        module = self._module()
        text = ("type s32 = LLT::scalar(32)\n\n"
                "ruleset R_G_ADD: G_ADD\n"
                "    clamp_max(0, s32, 2) when true\n")
        self.assertIsNone(module.parse_problem(text))


class ConstructorContracts(unittest.TestCase):
    """`assert` is plumbing; `sameSize` is a real predicate."""

    SRC = ROOT / "tests" / "importer" / "fixtures" / "legalizer-ctor-contracts.cpp"

    def test_assert_is_plumbing_and_same_size_transcribes(self):
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "T.gisel"
            r = run("RISCV", str(self.SRC), "--out", str(out))
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            self.assertIn("1 rulesets, 1 rules, 0 refused", r.stdout)
            self.assertIn("ignored line 3: assert(ST.hasStdExtZbb())", r.stdout)
            text = out.read_text()
        self.assertIn("legal size_of(slot(0)) == size_of(slot(1))", text)


class ProbeMode(unittest.TestCase):
    """`--probe` measures a target that has no pinned corpus yet."""

    def test_probe_reports_counts_and_writes_nothing(self):
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "P.gisel"
            r = run("--probe", "Example:Ex", "--src", str(FIXTURE))
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            self.assertIn("rulesets", r.stdout)
            self.assertNotIn("DRIFT", r.stdout)
            self.assertFalse(out.exists())

    def test_probe_needs_a_source(self):
        r = run("--probe", "Example:Ex")
        self.assertEqual(r.returncode, 2)
        self.assertIn("--probe needs --src", r.stderr)


class LambdaPredicates(unittest.TestCase):
    """A predicate lambda is read by its body, whatever the return type."""

    RETURN = ROOT / "tests" / "importer" / "fixtures" / "legalizer-ctor-lambda-return.cpp"
    SHAPE = ROOT / "tests" / "importer" / "fixtures" / "legalizer-ctor-lambda-shape.cpp"

    def test_lambda_with_an_explicit_return_type_is_read(self):
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "T.gisel"
            r = run("RISCV", str(self.RETURN), "--out", str(out))
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            self.assertIn("1 rulesets, 1 rules, 0 refused", r.stdout)
            text = out.read_text()
        self.assertIn("legal sort_of(slot(0)) == scalar", text)

    def test_an_unreadable_lambda_is_refused_not_recursed(self):
        # `pred()` and `pred_lambda()` used to hand the same text back and
        # forth, so a lambda with a shape neither knew raised RecursionError
        # instead of naming the statement.
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "T.gisel"
            r = run("RISCV", str(self.SHAPE), "--out", str(out))
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("lambda shape not understood", r.stdout)
        self.assertNotIn("RecursionError", r.stdout + r.stderr)


class PredicateValueEscape(unittest.TestCase):
    """A helper that returns a `LegalityPredicate` is copied, not inlined.

    `isRegisterType(ST, 0)` is a `std::function<bool(const LegalityQuery&)>`:
    it only answers when called with the rule's query.  Following its body
    recurses through the `bool` overload of the same name, so the importer
    must copy the call instead.
    """

    SRC = ROOT / "tests" / "importer" / "fixtures" / "legalizer-ctor-pred-value.cpp"

    def test_predicate_value_is_copied_with_the_query(self):
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "T.gisel"
            r = run("RISCV", str(self.SRC), "--out", str(out))
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            text = out.read_text()
        self.assertIn('legal opaque("isRegisterType(ST, 0)(Query)")', text)
        self.assertNotIn("RecursionError", r.stdout + r.stderr)

    def test_the_inc_calls_the_helper_with_the_query(self):
        with tempfile.TemporaryDirectory() as d:
            src, inc = Path(d) / "T.gisel", Path(d) / "T.inc"
            run("RISCV", str(self.SRC), "--out", str(src))
            r = subprocess.run(
                [sys.executable, "-m", "gisel", "gen", str(src),
                 "--emit=inc", "--out", str(inc)],
                cwd=ROOT, capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            text = inc.read_text()
        self.assertIn("return (isRegisterType(ST, 0)(Query));", text)


class ReimportedCorpora(unittest.TestCase):
    """Every pinned constructor reproduces its checked-in `.gisel`."""

    def test_pinned_targets_reimport(self):
        for target, src, rulesets, rules in (
                ("X86", X86_SRC, 63, 328),
                ("AArch64", AARCH64_SRC, 117, 744),
                ("M68k", M68K_SRC, 6, 13),
                ("WebAssembly", WASM_SRC, 29, 109),
                ("PowerPC", PPC_SRC, 15, 25),
                ("ARM", ARM_SRC, 52, 96),
                ("Mips", MIPS_SRC, 48, 95),
                ("SPIRV", SPIRV_SRC, 83, 353)):
            if not _readable(src):
                continue
            with self.subTest(target=target), \
                    tempfile.TemporaryDirectory() as d:
                out = Path(d) / "T.gisel"
                r = run(target, str(src), "--out", str(out))
                self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
                self.assertIn("%d rulesets, %d rules, 0 refused"
                              % (rulesets, rules), r.stdout)
                self.assertEqual(
                    body(out.read_text()),
                    body((CORPUS / ("%sLegalizerInfo.gisel" % target)).read_text()))


def _readable(path):
    """`Path.exists()` raises when a parent is unreadable (CI runners)."""
    if path is None:
        return False
    try:
        return path.exists()
    except OSError:
        return False


class SourceDiscovery(unittest.TestCase):
    def test_unreadable_parent_is_skipped(self):
        with mock.patch.object(Path, "exists", side_effect=PermissionError):
            self.assertFalse(_readable(Path("/home/ubuntu/llvm/llvm-project")))

    def test_missing_environment_variable_is_not_a_path(self):
        self.assertFalse(_readable(None))


class PinnedRevision(unittest.TestCase):
    """The corpus provenance is a commit, not a working tree."""

    @unittest.skipUnless(_readable(PIN) and _readable(LLVM_GIT),
                         "pin file or LLVM checkout not present")
    def test_pin_reproduces_every_corpus(self):
        r = run("--all", "--pin", str(PIN), "--git-repo", str(LLVM_GIT),
                "--check")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(r.stdout.count(": matches"), 10, r.stdout)
        self.assertIn("import-report.json: matches", r.stdout)
        self.assertEqual(r.stdout.count(": read "), 9, r.stdout)

    def test_pin_records_one_commit_for_every_target(self):
        pin = json.loads(PIN.read_text())
        self.assertEqual(len(pin["commit"]), 40, pin["commit"])
        self.assertEqual(sorted(pin["sources"]),
                         ["AArch64", "ARM", "M68k", "Mips", "PowerPC",
                          "RISCV", "SPIRV", "WebAssembly", "X86"])
        for name, entry in pin["sources"].items():
            self.assertEqual(len(entry["sha256"]), 64, name)
            self.assertIn("llvm/lib/Target/", entry["path"])


class ReviewedNames(unittest.TestCase):
    """No `selN`/`condN` leftovers, and the reviewed names are present."""

    def test_no_generated_names_remain(self):
        for target in ("X86", "AArch64", "RISCV"):
            text = (CORPUS / ("%sLegalizerInfo.gisel" % target)).read_text()
            with self.subTest(target=target):
                self.assertNotRegex(text, r"\b[A-Z]_sel\d+\b")
                self.assertNotRegex(text, r"\b[A-Z]_cond\d+\b")

    def test_x86_named_selects(self):
        text = (CORPUS / "X86LegalizerInfo.gisel").read_text()
        for name in ("X_sMaxScalar", "X_s8MaxVector", "X_s16MaxVector",
                     "X_s32MaxVector", "X_s64MaxVector",
                     "X_sse2MaxScalar", "X_x87MinScalar", "X_cmovMinScalar"):
            self.assertIn("select %s =" % name, text)
        self.assertIn("pred X_useX87NoSSE1 =", text)
        self.assertIn(
            "select X_sMaxScalar = if X_Is64Bit then X_s64 else X_s32", text)

    def test_riscv_named_select(self):
        text = (CORPUS / "RISCVLegalizerInfo.gisel").read_text()
        self.assertIn("select R_maxScalar = if R_has64BitScalar", text)
        self.assertIn("pred R_has64BitScalar =", text)
        self.assertIn("feature R_hasStdExtD = ST.hasStdExtD()", text)
        self.assertIn("feature R_subtargetInfoHasZbb = STI.hasStdExtZbb()", text)
        # `getELen()` asserts without V, so the hoisted name keeps the guard
        # that dominates it in the C++ predicate.
        self.assertIn("feature R_XLen_le_ELen = "
                      "(ST.hasVInstructions() && XLen <= ST.getELen())", text)
        self.assertIn("feature R_getELen_64 = "
                      "(ST.hasVInstructions() && ST.getELen() == 64)", text)


class PinnedCounts(unittest.TestCase):
    def test_drift_fails_loudly(self):
        """A file with the pinned name but different rules must not pass."""
        with tempfile.TemporaryDirectory() as d:
            src = Path(d) / "AArch64LegalizerInfo.cpp"
            src.write_text(FIXTURE.read_text())
            out = Path(d) / "T.gisel"
            r = run("AArch64", str(src), "--out", str(out))
            self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
            self.assertIn("DRIFT", r.stdout)

    def test_all_is_pinned_or_explicit_source_only(self):
        r = run("X86", "--all")
        self.assertEqual(r.returncode, 2)
        self.assertIn("exactly one of TARGET, --all or --probe", r.stderr)


class RisCvCorpus(unittest.TestCase):
    def test_corpus_lints(self):
        r = subprocess.run(
            [sys.executable, "-m", "gisel.cli", "lint",
             str(CORPUS / "RISCVLegalizerInfo.gisel")],
            cwd=ROOT, capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("0 error(s)", r.stdout)
        self.assertIn("402 rule(s)", r.stdout)

    @unittest.skipUnless(_readable(RISCV_SRC), "RISCVLegalizerInfo.cpp not present")
    def test_reimport_reproduces_the_checked_in_corpus(self):
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "R.gisel"
            r = run("RISCV", str(RISCV_SRC), "--out", str(out))
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            self.assertIn("0 refused", r.stdout)
            self.assertIn("402 rules", r.stdout)
            self.assertEqual(body(out.read_text()),
                             body((CORPUS / "RISCVLegalizerInfo.gisel").read_text()))


if __name__ == "__main__":
    unittest.main()
