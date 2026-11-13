#!/usr/bin/env python3
"""One-shot transcriber: three upstream legalizer constructors -> `.gisel`.

This is deliberately **not** a reusable C++ importer.  It knows exactly the
three constructors this repository ships a corpus for, hardcodes their quirks
and pins the rule counts it produced last time.  A fourth target, or a
different upstream revision, is a new tool -- not a flag on this one.

    importer/import-cxx.py X86 .../X86LegalizerInfo.cpp --out ...
    importer/import-cxx.py --all --llvm-src /path/to/llvm --out-dir <gisel>/tests/corpus
    importer/import-cxx.py --all --llvm-src /path/to/llvm --check

The input is not C++ in general: it is the subset these three constructors are
written in -- `getActionDefinitionsBuilder(...)` chains, `auto &X = ...` with
later extension, `if`/`else`, `for` over opcode lists, `const LLT` constants
and a handful of `LegalityPredicate` lambdas.

Anything outside that subset is **refused with a line number**, never dropped:
a transcription that silently loses a rule is worse than no transcription.

Exit codes: 0 imported, 1 refused or drifted from the pinned counts,
2 usage/parse error.
"""

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GISEL_SRC = Path(os.environ.get("GISEL_SRC", ROOT.parent / "gisel"))
# The corpus is gisel's input, so it stays in the gisel checkout unless
# GISEL_CORPUS says otherwise.
CORPUS_DIR = Path(os.environ.get(
    "GISEL_CORPUS", GISEL_SRC / "tests" / "corpus"))

PREFIX = None  # set from the target name: RISCV -> "R_"
CURRENT_TARGET = None
USED_RENAMES = {"select": set(), "feature": set()}


def _norm(text):
    """Whitespace-insensitive key for the reviewed rename tables."""
    return " ".join(text.split())


# ---------------------------------------------------------------------------
# Reviewed names for the generated declarations
#
# The transcriber names anonymous ternaries `selN`/`condN`, which is accurate
# but unreadable.  The tables below are the reviewed replacement: the key is
# the C++ expression with the target prefix removed, the value is
# (select name, predicate name).  A `None` predicate means the condition is a
# bare feature name and is used directly in the `select`, so no alias record
# is emitted.  Every key must still match, or the import fails: an upstream
# edit must not silently reintroduce generated names.
# ---------------------------------------------------------------------------

SELECT_NAMES = {
    "X86": {
        "Subtarget.is64Bit() ? s64 : s32": ("sMaxScalar", None),
        "HasAVX512 ? v64s8 : HasAVX ? v32s8 : v16s8": ("s8MaxVector", None),
        "HasAVX ? v32s8 : v16s8": ("s8MaxVectorNoAVX512", None),
        "HasAVX512 ? v32s16 : HasAVX ? v16s16 : v8s16": ("s16MaxVector", None),
        "HasAVX ? v16s16 : v8s16": ("s16MaxVectorNoAVX512", None),
        "HasAVX512 ? v16s32 : HasAVX ? v8s32 : v4s32": ("s32MaxVector", None),
        "HasAVX ? v8s32 : v4s32": ("s32MaxVectorNoAVX512", None),
        "HasAVX512 ? v8s64 : HasAVX ? v4s64 : v2s64": ("s64MaxVector", None),
        "HasAVX ? v4s64 : v2s64": ("s64MaxVectorNoAVX512", None),
        "HasSSE2 ? s64 : s32": ("sse2MaxScalar", None),
        "(UseX87 && !HasSSE1) ? s16 : s32": ("x87MinScalar", "useX87NoSSE1"),
        "HasCMOV ? s16 : s8": ("cmovMinScalar", None),
    },
    "AArch64": {},
    "RISCV": {
        "(XLen == 64 || ST.hasStdExtD()) ? s64 : s32":
            ("maxScalar", "has64BitScalar"),
    },
}

# C++ feature text -> name suffix.  Without these the receiver is part of the
# slug (`ST_hasNEON`), which reads like a different namespace.
FEATURE_NAMES = {
    "X86": {},
    "AArch64": {
        "ST.hasBF16()": "hasBF16",
        "ST.hasFPARMv8()": "hasFPARMv8",
        "ST.hasMOPS()": "hasMOPS",
        "ST.hasNEON()": "hasNEON",
    },
    "WebAssembly": {
        "ST.hasAddr64()": "HasAddr64",
        "ST.hasSignExt()": "HasSignExt",
    },
    "ARM": {
        "ST.hasNEON()": "hasNEON",
        "ST.hasV5TOps()": "hasV5TOps",
        "ST.hasVFP2Base()": "hasVFP2Base",
        "ST.hasVFP4Base()": "hasVFP4Base",
        "ST.isTargetAEABI()": "isTargetAEABI",
        "ST.isTargetGNUAEABI()": "isTargetGNUAEABI",
        "ST.isTargetMuslAEABI()": "isTargetMuslAEABI",
        "ST.isThumb1Only()": "isThumb1Only",
        "ST.useSoftFloat()": "useSoftFloat",
    },
    "SPIRV": {
        "ST.canUseExtInstSet(SPIRV::InstructionSet::OpenCL_std)": "hasOpenCLStd",
        "ST.canUseExtension(SPIRV::Extension::SPV_ALTERA_arbitrary_precision_integers)": "hasArbitraryPrecisionInts",
        "ST.canUseExtension(SPIRV::Extension::SPV_EXT_long_vector)": "hasLongVector",
        "ST.getPointerSize() == 64": "has64BitPointers",
        "ST.isShader()": "isShader",
    },
    "Mips": {
        "ST.hasMSA()": "hasMSA",
        "ST.hasMips32r2()": "hasMips32r2",
        "ST.systemSupportsUnalignedAccess()": "systemSupportsUnalignedAccess",
    },
    "RISCV": {
        "ST.enableUnalignedScalarMem()": "enableUnalignedScalarMem",
        "ST.getELen() == 64": "getELen_64",
        "ST.hasStdExtA()": "hasStdExtA",
        "ST.hasStdExtD()": "hasStdExtD",
        "ST.hasStdExtF()": "hasStdExtF",
        "ST.hasStdExtM()": "hasStdExtM",
        "ST.hasStdExtP()": "hasStdExtP",
        "ST.hasStdExtZbb()": "hasStdExtZbb",
        "ST.hasStdExtZbc()": "hasStdExtZbc",
        "ST.hasStdExtZbkb()": "hasStdExtZbkb",
        "ST.hasStdExtZbkc()": "hasStdExtZbkc",
        "ST.hasStdExtZfa()": "hasStdExtZfa",
        "ST.hasStdExtZfh()": "hasStdExtZfh",
        "ST.hasStdExtZfhmin()": "hasStdExtZfhmin",
        "ST.hasStdExtZilsd()": "hasStdExtZilsd",
        "ST.hasStdExtZmmul()": "hasStdExtZmmul",
        "ST.hasVInstructions()": "hasVInstructions",
        "ST.hasVInstructionsF64()": "hasVInstructionsF64",
        "ST.hasVInstructionsI64()": "hasVInstructionsI64",
        "ST.hasVendorXTHeadBb()": "hasVendorXTHeadBb",
        "ST.is64Bit()": "is64Bit",
        "STI.hasStdExtZbb()": "subtargetInfoHasZbb",
        "XLen <= ST.getELen()": "XLen_le_ELen",
    },
}


# ---------------------------------------------------------------------------
# Lexing helpers
# ---------------------------------------------------------------------------

def strip_comments(text):
    text = re.sub(r"/\*.*?\*/", " ", text, flags=re.S)
    return [re.sub(r"//.*$", "", line) for line in text.splitlines()]


def split_ternary(text):
    """Split a top-level `C ? A : B`; None when the `?` is nested."""
    depth = 0
    for i, c in enumerate(text):
        if c in "([{":
            depth += 1
        elif c in ")]}":
            depth -= 1
        elif c == "?" and depth == 0:
            depth = 0
            for j in range(i + 1, len(text)):
                d = text[j]
                if d in "([{":
                    depth += 1
                elif d in ")]}":
                    depth -= 1
                elif d == ":" and depth == 0:
                    return text[:i], text[i + 1:j], text[j + 1:]
            return None
    return None


def split_top(text, sep=","):
    """Split at top level, honouring (), [], {} and string literals."""
    parts, depth, cur, instr = [], 0, [], False
    for c in text:
        if instr:
            cur.append(c)
            if c == '"':
                instr = False
        elif c == '"':
            instr = True
            cur.append(c)
        elif c in "([{":
            depth += 1
            cur.append(c)
        elif c in ")]}":
            depth -= 1
            cur.append(c)
        elif c == sep and depth == 0:
            parts.append("".join(cur).strip())
            cur = []
        else:
            cur.append(c)
    tail = "".join(cur).strip()
    if tail:
        parts.append(tail)
    return parts


def take_call(text, i):
    """`name(...)` starting at i; returns (name, args, end) or None."""
    m = re.match(r"([A-Za-z_]\w*(?:::[A-Za-z_]\w*)*)\s*\(", text[i:])
    if not m:
        return None
    name = m.group(1)
    j = i + m.end()
    depth, start = 1, j
    while j < len(text) and depth:
        if text[j] == "(":
            depth += 1
        elif text[j] == ")":
            depth -= 1
        j += 1
    return name, text[start:j - 1], j


# ---------------------------------------------------------------------------
# LLT expressions  ->  `type` declarations
# ---------------------------------------------------------------------------

BUILTIN_LLT = {
    "LLT::scalar": "s", "LLT::integer": "i",
    "LLT::bfloat16()": "bf16", "LLT::float16()": "f16",
    "LLT::float32()": "f32", "LLT::float64()": "f64",
}


def _llt_float_ieee(text):
    """`LLT::floatIEEE(N)` -> the named shortcut the DSL grammar knows.

    `floatIEEE` is the general constructor; the DSL spells the sized IEEE
    types as `float16()/float32()/float64()/float128()`.  Same LLT, so the
    rewrite is a spelling change, not a translation.  A size with no named
    shortcut is left alone and then refused with its source line.
    """
    return re.sub(r"LLT::floatIEEE\(\s*(16|32|64|128)\s*\)",
                  lambda m: "LLT::float%s()" % m.group(1), text)


# SPIRVAS address-space names in declaration order; the C++ enum is the
# authority (`llvm/Support/SPIRVAddrSpace.h`) and the corpus spells the numbers.
SPIRV_ADDRESS_SPACES = {
    name: str(index) for index, name in enumerate((
        "Function", "CrossWorkgroup", "UniformConstant", "Workgroup",
        "Generic", "DeviceOnlyINTEL", "HostOnlyINTEL", "Input", "Output",
        "CodeSectionINTEL", "Private", "StorageBuffer", "Uniform",
        "PushConstant"))
}

# llvm/Support/AMDGPUAddrSpace.h spells an address space as an enumerator; the
# corpus only carries the number, same as the SPIR-V table above.
AMDGPU_ADDRESS_SPACES = {
    "FLAT_ADDRESS": "0", "GLOBAL_ADDRESS": "1", "REGION_ADDRESS": "2",
    "LOCAL_ADDRESS": "3", "CONSTANT_ADDRESS": "4", "PRIVATE_ADDRESS": "5",
    "CONSTANT_ADDRESS_32BIT": "6", "BUFFER_FAT_POINTER": "7",
    "BUFFER_RESOURCE": "8", "BUFFER_STRIDED_POINTER": "9",
}


class Types:
    """C++ LLT constants found in the constructor."""

    def __init__(self):
        self.decl = []          # [(cpp name, emitted name, rhs text)]
        self.sets = {}          # cpp name -> [cpp type names]
        self.set_alias = {}     # cpp name -> a set expression (ternary)
        self.by_cpp = {}        # cpp name -> emitted name
        self.int_consts = {}    # file-scope `unsigned N = 1024;`
        self.refused = []

    def name(self, cpp):
        cpp = _llt_float_ieee(cpp)
        if cpp in self.by_cpp:
            return self.by_cpp[cpp]
        canon = "?" + _norm(cpp).replace(PREFIX, "")
        if canon in self.by_cpp:
            return self.by_cpp[canon]
        # The canonical spelling is lower case (`s32`, `v2s32`), but a target
        # may name the same type in upper case (`Amdgpu_S32`, `Amdgpu_V2S32`).
        # Both are the same LLT, so both spellings are looked up.
        def spelled(*keys):
            for key in keys:
                if key in self.by_cpp:
                    return self.by_cpp[key]
            return None

        m = re.fullmatch(r"LLT::scalar\(\s*(\d+)\s*\)", cpp.strip())
        if m and spelled("s%s" % m.group(1), "S%s" % m.group(1)):
            return spelled("s%s" % m.group(1), "S%s" % m.group(1))
        m = re.fullmatch(r"LLT::integer\(\s*(\d+)\s*\)", cpp.strip())
        if m and spelled("i%s" % m.group(1), "I%s" % m.group(1)):
            return spelled("i%s" % m.group(1), "I%s" % m.group(1))
        m = re.fullmatch(r"LLT::fixed_vector\(\s*(\d+)\s*,\s*LLT::scalar"
                         r"\(\s*(\d+)\s*\)\s*\)", cpp.strip())
        if m:
            got = spelled("v%ss%s" % (m.group(1), m.group(2)),
                          "V%ss%s" % (m.group(1), m.group(2)),
                          "V%sS%s" % (m.group(1), m.group(2)))
            if got:
                return got
        m = re.fullmatch(r"LLT::fixed_vector\(\s*(\d+)\s*,\s*(.+?)\s*\)",
                         cpp.strip(), re.S)
        if m:
            # A vector whose element type the file names (`LLT::fixed_vector(2,
            # LocalPtr)`) has no declared type of its own.  Give it one, the
            # way the source would have: reuse an identical declaration when
            # the file already has it.
            lanes, elem = m.group(1), self.name(m.group(2).strip())
            rhs = "LLT::fixed_vector(%s, %s)" % (lanes, elem)
            for _, name, text in self.decl:
                if text == rhs:
                    self.by_cpp[cpp.strip()] = name
                    return name
            stem = elem.split("_", 1)[1] if elem.startswith(PREFIX) else elem
            name = PREFIX + "V%s%s" % (lanes, stem)
            self.decl.append((None, name, rhs))
            self.by_cpp[cpp.strip()] = name
            return name
        m = split_ternary(cpp.strip())
        if m:
            # `(cond) ? T : F` -> a named `select` declaration (the backends
            # resolve type values through names, not inline expressions).
            self.anon = getattr(self, "anon", [])
            self.preds = getattr(self, "preds", [])
            key = _norm(cpp).replace(PREFIX, "")
            named = SELECT_NAMES.get(CURRENT_TARGET, {}).get(key)
            cond = self.cond_fn(m[0].strip())
            cond = re.sub(r'feature\("((?:[^"\\]|\\.)*)"\)',
                          lambda k: self.feat_name(k.group(1)), cond)
            if named:
                USED_RENAMES["select"].add(key)
                sel_suffix, pred_suffix = named
                name = PREFIX + sel_suffix
            else:
                self.unnamed = getattr(self, "unnamed", 0) + 1
                sel_suffix = pred_suffix = None
                name = PREFIX + "sel%d" % getattr(self, "sel_n", 0)
                self.sel_n = getattr(self, "sel_n", 0) + 1
            lo = self.name(m[1].strip())
            hi = self.name(m[2].strip())
            # A bare condition is already a feature/pred name; the `select`
            # grammar accepts it directly, so no `pred` alias is needed.
            if re.fullmatch(r"[A-Za-z_]\w*", cond):
                self.anon.append((name, cond, lo, hi))
            else:
                pname = PREFIX + pred_suffix if pred_suffix \
                    else PREFIX + "cond%d" % len(self.preds)
                self.preds.append((pname, cond))
                self.anon.append((name, pname, lo, hi))
            self.by_cpp[cpp.strip()] = name
            self.by_cpp[canon] = name
            return name
        return cpp

    def elem_of(self, cpp):
        """Element type of a vector LLT constant -> its `.gisel` type name."""
        name = self.name(cpp)
        stem = re.escape(PREFIX) + r"(?:nxv|nv|v|V)(\d+)"
        for pat, elem in ((stem + r"([sifSIF])(\d+)", None),
                          (stem + r"p0", "p0"),
                          (stem + r"bf16", "bf16")):
            m = re.fullmatch(pat, name)
            if m:
                if elem is None:
                    keys = [m.group(2).upper() + m.group(3),
                            m.group(2).lower() + m.group(3)]
                else:
                    keys = [elem]
                for key in keys:
                    got = self.name(key)
                    if got != key:
                        return got
                return PREFIX + keys[-1]
        raise Refuse("element type of %r" % cpp)

    def translate_refs(self, expr):
        """Rewrite C++ local type names to their `.gisel` names."""
        def sub(m):
            word = m.group(0)
            return self.by_cpp.get(word, word)
        return re.sub(r"\b[A-Za-z_]\w*\b", sub, expr)

    def add(self, cpp_name, rhs, line):
        emitted = PREFIX + cpp_name
        text = _llt_float_ieee(self.translate_refs(rhs))
        if split_ternary(text.strip()):
            # `cond ? A : B` -> a named `select` (same as a reference site).
            self.by_cpp[cpp_name] = self.name(text)
            return
        m = re.fullmatch(r"LLT::pointer\(\s*(.+?)\s*,\s*(.+?)\s*\)", text, re.S)
        if m:
            asx = re.sub(r"SPIRVAS::(\w+)",
                         lambda k: SPIRV_ADDRESS_SPACES.get(k.group(1),
                                                            k.group(1)),
                         m.group(1).strip())
            asx = re.sub(r"AMDGPUAS::(\w+)",
                         lambda k: AMDGPU_ADDRESS_SPACES.get(k.group(1),
                                                             k.group(1)),
                         asx)
            width = m.group(2).strip()
            tern = getattr(self, "int_ternary_text", {}).get(width)
            if tern:
                # `strace PSize` is `ST.getPointerSize()`, two values wide; the
                # type declaration is where the DSL takes the raw condition.
                width = "%s ? %d : %d" % tern
            text = "LLT::pointer(%s, %s)" % (asx, width)
        # LLT::scalar(XLen) / 2*XLen: a feature-dependent width.  The named
        # form is a select over the two legal values.
        m = re.fullmatch(r"LLT::(?:scalar|integer)\(\s*(.+?)\s*\)", text, re.S)
        if m:
            inner = m.group(1).strip()
            inner = str(self.int_consts.get(inner, inner))
            if re.fullmatch(r"\d+", inner):
                kind = "scalar" if text.startswith("LLT::scalar") else "integer"
                text = "LLT::%s(%s)" % (kind, inner)
            else:
                pick = self._width_select(inner)
                if pick is None:
                    self.refused.append((line, "type %s: cannot normalize %r"
                                         % (cpp_name, rhs)))
                    return
                cond, then_bits, else_bits = pick
                kind = "scalar" if text.startswith("LLT::scalar") else "integer"
                self._declare_pair(cpp_name, kind, then_bits, else_bits, cond)
                return
        self.by_cpp[cpp_name] = emitted
        self.decl.append((cpp_name, emitted, text))

    def _width_select(self, expr):
        """A feature-dependent width -> (condition, then bits, else bits).

        Two spellings reach here: RISC-V's `XLen` shorthand and a general
        `cond ? N : M` ternary (WebAssembly's `ST.hasAddr64() ? 64 : 32`).
        Both become a named `select` over two literal widths; the condition is
        kept as C++ text and rendered through `Translator.feature`, so it can
        be any predicate the feature table can answer.
        """
        m = re.fullmatch(r"(.+?)\s*\?\s*(\d+)\s*:\s*(\d+)", expr.strip(),
                         re.S)
        if m:
            return m.group(1).strip(), int(m.group(2)), int(m.group(3))
        e = expr.replace(" ", "")
        if e == "XLen":
            return "XLen == 64", 64, 32
        if e in ("2*XLen", "XLen*2"):
            return "XLen == 64", 128, 64
        return None

    def _existing_name(self, kind, bits):
        """A declared type with this LLT value, if the file already has one.

        Two names for one LLT are the same `const LLT` in C++, so the `.inc`
        emitter (which resolves rule types by value, not by the name the rule
        spelled) would fold them into whichever name was declared last.  Reuse
        the existing name instead: the generated constructor then reads like
        the source, and no `Wasm_p0i_lo` stands in for `Wasm_i32`.
        """
        rhs = "LLT::%s(%d)" % (kind, bits)
        for _, name, text in self.decl:
            if text == rhs:
                return name
        return None

    def _declare_pair(self, cpp_name, kind, then_bits, else_bits, cond):
        emitted = PREFIX + cpp_name
        lo, hi = sorted((then_bits, else_bits))
        names = {}
        for bits, suffix in ((lo, "_lo"), (hi, "_hi")):
            name = self._existing_name(kind, bits)
            if name is None:
                name = "%s%s" % (emitted, suffix)
                self.decl.append((None, name, "LLT::%s(%d)" % (kind, bits)))
            names[bits] = name
        lo_name, hi_name = names[lo], names[hi]
        self.by_cpp["__lo_" + cpp_name] = lo_name
        self.by_cpp["__hi_" + cpp_name] = hi_name
        self.by_cpp[cpp_name] = emitted
        self.selects = getattr(self, "selects", [])
        then_name, else_name = ((hi_name, lo_name) if then_bits >= else_bits
                                else (lo_name, hi_name))
        self.selects.append((emitted, then_name, else_name, cond))


# ---------------------------------------------------------------------------
# Predicates and value expressions
# ---------------------------------------------------------------------------

ORDERINGS = {"Unordered": "ordering_unordered",
             "Monotonic": "ordering_monotonic",
             "Acquire": "ordering_acquire",
             "Release": "ordering_release",
             "AcquireRelease": "ordering_acquire_release",
             "SequentiallyConsistent": "ordering_sequentially_consistent"}


def _fold_pow2ceil(text):
    """`1u << Log2_32_Ceil(x)` -> `pow2_ceil(x)` (balanced args)."""
    out, i = [], 0
    while True:
        j = text.find("Log2_32_Ceil(", i)
        if j < 0:
            out.append(text[i:])
            return "".join(out)
        k = j + len("Log2_32_Ceil")
        end = match_paren(text, k)
        prefix, arg = text[i:j], text[k + 1:end - 1]
        m = re.search(r"1u?\s*<<\s*$", prefix)
        if not m:
            out.append(text[i:end])
        else:
            out.append(prefix[:m.start()])
            out.append("pow2_ceil(%s)" % arg)
        i = end


def _whole_paren(text):
    """`(expr)` -> `expr`; None when the parentheses are not the whole text."""
    t = text.strip()
    if not (t.startswith("(") and t.endswith(")")):
        return None
    depth = 0
    for i, c in enumerate(t):
        if c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth == 0 and i != len(t) - 1:
                return None
    return t[1:-1].strip() if depth == 0 else None


def _strip_outer_parens(text):
    """Drop grouping parentheses around the whole expression."""
    t = text.strip()
    while True:
        inner = _whole_paren(t)
        if inner is None:
            return t
        t = inner


def _binop_at(text, ops):
    """Index of the rightmost top-level operator in ``ops`` (left-assoc)."""
    depth = 0
    best, bestc = -1, None
    for i, c in enumerate(text):
        if c in "([":
            depth += 1
        elif c in ")]":
            depth -= 1
        elif depth == 0 and c in ops:
            if c in "+-":
                j = i - 1
                while j >= 0 and text[j].isspace():
                    j -= 1
                if j < 0 or text[j] in "+-*/%(<,=!&|?:":
                    continue          # unary sign, not a binary operator
            best, bestc = i, c
    return best, bestc


def _match_brace(text, i):
    """Index just past the `}` that closes the `{` at ``i``."""
    depth = 0
    for j in range(i, len(text)):
        if text[j] == "{":
            depth += 1
        elif text[j] == "}":
            depth -= 1
            if depth == 0:
                return j + 1
    raise Refuse("unbalanced braces")


def _is_assert(st):
    """True for `assert(...)` / `static_assert(...)`: a build-time contract."""
    return re.match(r"(?:static_)?assert\s*\(", st.strip()) is not None


def _split_top_op(text, op):
    """Split on a top-level operator (`&&`, `||`)."""
    depth, parts, start, i = 0, [], 0, 0
    while i < len(text):
        c = text[i]
        if c in "([{":
            depth += 1
        elif c in ")]}":
            depth -= 1
        elif depth == 0 and text.startswith(op, i):
            parts.append(text[start:i])
            i += len(op)
            start = i
            continue
        i += 1
    parts.append(text[start:])
    return parts


# `X::y(` that names a type or an enum, not a host function call.
TYPE_NAMESPACES = {"LLT", "ElementCount", "AtomicOrdering", "AMDGPUAS",
                   "AMDGPUSubtarget", "CodeModel", "TypeSize", "KnownBits"}


def _host_call(text):
    """True when `text` calls a host function, not a type/enum constructor."""
    for m in re.finditer(r"\b([A-Za-z_]\w*)::[A-Za-z_]\w*\s*\(", text):
        if m.group(1) not in TYPE_NAMESPACES:
            return True
    return False


def _looks_type(value):
    """True when a `.gisel` value expression already denotes an LLT."""
    return value.startswith(("elem_of(", "ty(", "vec_of(", "scalar_or_vec_of(",
                             "int_of_size(", "scalar_of("))


def _arith(text):
    """C++ integer arithmetic the DSL spells as `mul`/`add`/`udiv`/....

    The rewrite follows C++ precedence (``* / %`` bind tighter than ``+ -``)
    and is left-associative, so ``a + b * c`` becomes
    ``add(a, mul(b, c))``.  Parentheses are only grouping, never a value.
    """
    text = re.sub(r"\b(\d+)[uUlL]+\b", r"\1", text)   # `1u`, `8LL`, ...
    text = _fold_pow2ceil(text)
    t = text.strip()
    # Only arithmetic grouping is dropped.  A parenthesised *predicate*
    # (`(sort_of(x) == vector and ...)`) must keep its parentheses: `_arith`
    # is also reached from the comparison branch.
    while True:
        inner = _whole_paren(t)
        if inner is None or not (re.fullmatch(r"-?\d+", inner) or
                                 _binop_at(inner, "+-*/%")[0] >= 0):
            break
        t = inner
    if re.fullmatch(r"-?\d+", t):
        return t
    for ops, names in (("+-", {"+": "add", "-": "sub"}),
                       ("*/%", {"*": "mul", "/": "udiv", "%": "urem"})):
        i, c = _binop_at(t, ops)
        if i >= 0:
            lhs, rhs = t[:i].strip(), t[i + 1:].strip()
            return "%s(%s, %s)" % (names[c], _arith(lhs), _arith(rhs))
    return t


def _ordering(name):
    return ORDERINGS.get(name, "ordering_" + re.sub(r"(?<!^)(?=[A-Z])", "_", name).lower())


class Translator:
    """C++ predicate/value text -> `.gisel` text.  Raises Refuse."""

    def __init__(self, types):
        self.t = types
        self.features = {}      # cpp text -> feature name
        self.feature_aliases = {}   # cpp bool variable -> feature name
        self.opcode = None
        self.helpers = {}       # static LegalityPredicate helpers
        self.bool_helpers = {}  # static bool helpers used as `if` gates
        self.aligns = {}        # align lambdas: name -> (cond, then, else)
        self.named_preds = {}   # LegalityPredicate variables
        self.param_preds = {}   # local `auto F = [](Query, unsigned i)`
        self.int_ternary = {}   # cpp int name -> (gate, value if true, else)
        self.refused = []
        self.opaque = []        # host predicates copied as `opaque("...")`

    # -- features: any `ST.x()` / `XLen == N` becomes a named feature --------
    def feature(self, cpp_text):
        # A `feature` declaration is one `.gisel` line, and the declaration
        # text is also the evaluator key, so a C++ expression wrapped over
        # several lines is joined before either is written down.
        cpp_text = " ".join(cpp_text.split())
        # `RISCVSubtarget::getELen()` asserts unless the V extension is on, and
        # upstream only ever evaluates it under `hasVInstructions()`.  Hoisting
        # the sub-expression on its own loses that guard, and the generated
        # constructor then aborts on every non-V subtarget, so the guard stays
        # on the hoisted name.  The declaration text is the evaluator key, so
        # the feature table uses the guarded spelling too.
        name = cpp_text
        key_text = cpp_text
        if "getELen()" in cpp_text and "hasVInstructions()" not in cpp_text:
            name = "(ST.hasVInstructions() && %s)" % cpp_text
        if name not in self.features:
            key = _norm(key_text).replace("( ", "(")
            suffix = FEATURE_NAMES.get(CURRENT_TARGET, {}).get(key)
            if suffix:
                USED_RENAMES["feature"].add(key)
            else:
                suffix = re.sub(r"[^0-9A-Za-z]+", "_", key_text).strip("_")
            self.features[name] = PREFIX + suffix
        return self.features[name]

    def feature_named(self, name, cpp_text):
        self.features[cpp_text] = PREFIX + name
        return PREFIX + name

    def opaque_expr(self, cpp):
        """A host C++ expression copied through `opaque(...)`."""
        cpp = " ".join(cpp.split())
        self.opaque.append(cpp)
        return 'opaque("%s")' % (cpp.replace("\\", "\\\\")
                                      .replace('"', '\\"'))

    def host_pred(self, text):
        """`SIRegisterInfo::f(Query.Types[VecTypeIdx])` -> opaque atom.

        The string is emitted verbatim inside the generated predicate lambda,
        so every index spelled by a constructor variable is folded to the
        number the C++ would see.
        """
        def idx(m):
            return "Query.%s[%s]" % (m.group(1), self.slot_index(m.group(2)))
        return self.opaque_expr(re.sub(r"Query\.(Types|MMODescrs)\[([^\]]+)\]",
                                       idx, text))

    def cpp_bool(self, text):
        """The constructor's `Op == G_LOAD` spelling -> `true`/`false`."""
        text = " ".join(text.split())
        if text in ("true", "false"):
            return text
        m = re.fullmatch(r"(\w+)\s*==\s*(G_\w+)", text)
        if m:
            v = self.env.get(m.group(1))
            if isinstance(v, str):
                return "true" if v == m.group(2) else "false"
        raise Refuse("local lambda argument %r" % text)

    def pred(self, text):
        text = text.strip()
        if re.fullmatch(r"Query\.(?:Types|MMODescrs)\[[^\]]+\]\.[A-Za-z_]+\("
                        r"\)", text):
            # One observation spelled on a query slot; `query_body` owns the
            # `Query.Types[i].foo()` vocabulary.
            return self.query_body(text)
        if text.startswith("LegalityPredicate(") and text.endswith(")"):
            return self.pred_lambda(text)
        if re.match(r"\[[^\]]*\]\s*\(", text):
            return self.pred_lambda(text)
        text = " ".join(text.split())
        text = re.sub(r"\bQ\.", "Query.", text)
        # `size_of` in the DSL is the known-minimum element-count product, the
        # same number a release-build LLT reports; the explicit
        # `getKnownMinValue()` spelling is that same observation.
        text = text.replace(".getSizeInBits().getKnownMinValue()", ".getSizeInBits()")
        # The AArch64 constructor spells the element-count observation through
        # a local helper that also answers for scalars and scalable vectors,
        # where LLT::getNumElements() is not defined.  Same observation.
        text = re.sub(r"\bnumElements\(", "num_elems(", text)
        # A mem operand read directly inside a lambda body: `getSizeInBits()`
        # is the memory type's total width, `AlignInBits` its alignment.
        text = re.sub(r"Query\.MMODescrs\[(\d+)\]\.MemoryTy"
                      r"\.getSizeInBits\(\)",
                      r"size_of(mem_ty(mem_slot(\1)))", text)
        text = re.sub(r"Query\.MMODescrs\[(\d+)\]\.AlignInBits",
                      r"align_of(mem_slot(\1))", text)
        # Normalise the power-of-two test before anything decides a call is
        # already a `.gisel` observation: `isPowerOf2_64(x)` is `is_pow2(x)`.
        text = re.sub(r"isPowerOf2_\d+\(|isPowerOf2\(", "is_pow2(", text)
        text = re.sub(r"llvm::has_single_bit<[^>]*>\(", "is_pow2(", text)
        m = re.fullmatch(r"sameSize\(\s*(\d+)\s*,\s*(\d+)\s*\)", text)
        if m:
            # `sameSize(i, j)` is `Types[i].getSizeInBits() ==
            # Types[j].getSizeInBits()` (LegalityPredicates.cpp:215).
            return ("size_of(slot(%s)) == size_of(slot(%s))"
                    % (m.group(1), m.group(2)))
        helper = self.query_helper(text)
        if helper is None:
            helper = self.legality_helper(text)
        if helper is not None:
            return helper
        if text in self.feature_aliases:
            return self.feature_aliases[text]
        if isinstance(getattr(self, "env", {}).get(text), bool):
            return "true" if self.env[text] else "not true"
        if self._single_call(text) and re.fullmatch(r"(slot|elem_of|scalar_of|sort_of|num_elems|bits_of|size_of|"
                        r"size_in_bytes_of|addr_space|align_of|mem_ty|mem_slot|"
                        r"mem_ordering|mem_failure_ordering|is_pow2|is_float16|"
                        r"is_bfloat16|is_byte_sized|imm|ty|int_of_size|urem|udiv|mul|"
                        r"select|replace|scalar_or_vec_of|vec_of|elt_type_to|"
                        r"elt_size_to|div_lanes|pow2_ceil|same_as|pow2|pow2_min|"
                        r"multiple_of|clamp|clamp_min|clamp_max|min_vector_size)\s*\(.*\)",
                        text) and "Query." not in text:
            return text
        if text in ("always", "true"):
            return "true"
        if text in ("never", "false"):
            return "not true"
        if text in self.features:
            return self.features[text]
        if text in self.named_preds:
            return "(%s)" % self.named_preds[text]
        m = re.fullmatch(r"(\w+)\s*\(\s*(?:Query|Q)\s*,\s*(.+)\)",
                         text, re.S)
        if m and m.group(1) in self.param_preds:
            param, body = self.param_preds[m.group(1)]
            idx = self.slot_index(m.group(2))
            body = re.sub(r"\b%s\b" % re.escape(param), idx, body)
            return self.body_pred(body)
        if text.startswith("(") and text.endswith(")") and self._balanced(text[1:-1]):
            return self.pred(text[1:-1])
        for op, kw in (("||", "or"), ("&&", "and")):
            parts = self._split_op(text, op)
            if len(parts) > 1:
                return "(" + (" %s " % kw).join(self.pred(p) for p in parts) + ")"
        if text.startswith("!"):
            return "not " + self.pred(text[1:])
        m = re.fullmatch(r"(.+?)\s*(==|!=)\s*AtomicOrdering::NotAtomic", text)
        if m:
            # NotAtomic is the bottom of the ordering lattice, and the DSL has
            # no tag for it: `!= NotAtomic` is `>= Unordered`.
            lhs = self.pred(m.group(1))
            return "%s %s ordering_unordered" % (
                lhs, ">=" if m.group(2) == "!=" else "<")
        text = re.sub(r"AtomicOrdering::(\w+)",
                      lambda m: _ordering(m.group(1)), text)
        m = re.fullmatch(r"(?:::)?(all|any)\s*\((.*)\)", text, re.S)
        if m:
            parts = split_top(m.group(2))
            kw = "and" if m.group(1) == "all" else "or"
            return "(" + (" %s " % kw).join(self.pred(p) for p in parts) + ")"
        m = re.fullmatch(r"LegalityPredicates::(all|any)\s*\((.*)\)", text, re.S)
        if m:
            parts = split_top(m.group(2))
            kw = "and" if m.group(1) == "all" else "or"
            return "(" + (" %s " % kw).join(self.pred(p) for p in parts) + ")"
        # `typeIs(0, s32)(Query)`: the query is implicit in a `.gisel` rule.
        m = re.fullmatch(r"(.+\S)\s*\((?:Query|Q)\)", text, re.S)
        if m:
            return self.pred(m.group(1))
        # `x.isVector() != y.isVector()` is an xor over two predicates.
        m = re.fullmatch(r"(.+?)\.isVector\(\)\s*!=\s*(.+?)\.isVector\(\)",
                         text, re.S)
        if m:
            a = self.pred("%s.isVector()" % m.group(1))
            b = self.pred("%s.isVector()" % m.group(2))
            return "((%s and not %s) or (not %s and %s))" % (a, b, a, b)
        text = self._contained_braces(text)
        m = re.fullmatch(r"llvm::is_contained\(\s*(\w+)\s*,\s*(.+)\)", text, re.S)
        if m:
            return "in(%s, %s)" % (self.expr_text(m.group(2)), PREFIX + m.group(1))
        m = re.fullmatch(r"TM\s*\.\s*getCodeModel\(\)\s*==\s*CodeModel::(\w+)", text)
        if m:
            return self.feature_named(m.group(1) + "CodeModel", text)
        m = self._factory_pred(text)
        if m is not None:
            return m
        # Predicates that name a slot by a constructor variable: fold it first.
        # A call to a file-scope helper is left alone: its first argument is
        # the subtarget (`isRegisterType(ST, 0)`), not a slot index, and the
        # `local_*` / opaque paths below know what to do with it.
        helper = re.match(r"(\w+)\s*\(", text)
        if not (helper and helper.group(1) in (
                set(getattr(self, "pred_values", ()))
                | set(getattr(self, "helpers", {}))
                | set(getattr(self, "bool_helpers", {}))
                | set(getattr(self, "local_lambdas", {})))):
            text = re.sub(r"\(\s*(\w+)\s*,", lambda m: "(%s," % (
                m.group(1) if m.group(1).isdigit() else self.slot_index(m.group(1))
            ) if not m.group(1).isdigit() else m.group(0), text)
        for pat, body in (
                (r"sizeIs\s*\(\s*(\d+)\s*,\s*(\d+)\)", r"size_of(slot(\1)) == \2"),
                (r"immIs\s*\(\s*(\d+)\s*,\s*(-?\d+)\)", r"imm(\1) == \2"),
                (r"immInSet\s*\(\s*(\d+)\s*,\s*\{(.*)\}\)", r"in(imm(\1), [\2])"),
                (r"isScalar\s*\(\s*(\d+)\s*\)", r"sort_of(slot(\1)) == scalar"),
        ):
            m = re.fullmatch(pat, text, re.S)
            if m:
                out = re.sub(r"\\(\d)", lambda k: m.group(int(k.group(1))), body)
                return self._types_in_pred(out)
        # A subtarget read (or XLen) compared with a constant is one opaque
        # configuration axis.  Splitting it into `select(feature, ...) == k`
        # would still evaluate correctly, but the feature name is what the
        # `.inc` gates on, so keep it named.
        if re.fullmatch(r"(?:(?:ST|STI)\.\w+\(\)|XLen)\s*(?:==|!=|<=|>=|<|>)\s*"
                        r"(?:\d+|(?:ST|STI)\.\w+\(\)|XLen"
                        r"|(?:[A-Za-z_]\w*::)+\w+)", text):
            # The right side may be an enumerator (`ST.getGeneration() >=
            # AMDGPUSubtarget::SEA_ISLANDS`).  It is still one configuration
            # axis, so it becomes a named opaque feature rather than a `ty()`
            # operand the grammar cannot parse.
            return self.feature(text)
        cmp = self._split_cmp(text)
        if cmp is not None:
            lhs, op, rhs = cmp
            return "%s %s %s" % (self.expr_text(lhs), op, self.expr_text(rhs))
        try:
            local = self.local_bool(text)
        except Refuse:
            # The name is known as a `bool` helper but this call has another
            # arity (an overload); the same name may still be a predicate
            # value, which the escape below handles.
            local = None
        if local is not None:
            return local
        try:
            local = self.local_pred(text)
        except Refuse:
            # The helper is known, but this call is not the shape its body
            # inlines into (AMDGPU's predicate overload delegates to a `bool`
            # overload on `Query.Types[i]`).  The escape below can still copy
            # the call; only give up when it cannot.
            local = None
        if local is not None:
            return local
        # A bool helper declared in the constructor: inline its body so the
        # generated `.inc` does not depend on a local that the swap removes.
        m = re.fullmatch(r"(\w+)\s*\(\s*(?:Query|Q)\s*,\s*(.+)\)", text, re.S)
        if m and m.group(1) in getattr(self, "local_lambdas", {}):
            params, body = self.local_lambdas[m.group(1)]
            if len(params) < 2:
                raise Refuse("local lambda arity")
            flag = self.cpp_bool(m.group(2))
            pname = params[-1].split()[-1].lstrip("*&")
            body = re.sub(r"\b%s\b" % re.escape(pname), flag, body)
            return self.opaque_expr("([&]() -> bool { %s }())"
                                    % " ".join(body.split()))
        # A file-local helper the predicate algebra does not model but that
        # returns a predicate value: `isRegisterType(ST, 0)` is a
        # `std::function<bool(const LegalityQuery&)>`, so it only becomes a
        # bool when called with the rule's query.  Inlining above wins when
        # the helper's body is understood; this is the escape for the rest.
        # Only the slot-index form takes an integer -- an `LLT` argument
        # belongs to the `bool` overload and must not get `(Query)`.
        m = re.fullmatch(r"(\w+)\s*\((.*)\)", text, re.S)
        if m:
            name, args = m.group(1), m.group(2)
            parts = split_top(args)
            last = parts[-1].strip()
            resolved = last if re.fullmatch(r"-?\d+", last) else None
            if resolved is None:
                v = self.env.get(last)
                if isinstance(v, int) and not isinstance(v, bool):
                    resolved = str(v)
            if name in getattr(self, "pred_values", ()) and resolved is not None:
                parts[-1] = resolved
                return 'opaque("%s(%s)(Query)")' % (name, ", ".join(parts))
        # Subtarget reads and XLen comparisons are cached features.
        if re.fullmatch(r"(ST|STI)\.\w+\([^()]*\)", text) or \
                re.fullmatch(r"XLen\s*==\s*\d+", text):
            return self.feature(text)
        if "Query." in text:
            if _host_call(text):
                # A host helper inside a predicate
                # (`SIRegisterInfo::getX(Query.Types[i])`).  `opaque(...)` is
                # a predicate primary, so a *sub-expression* can be copied
                # through; slot indices named by constructor variables must
                # be folded first, because the generated `.inc` has no
                # `VecTypeIdx` in scope.
                return self.host_pred(text)
            return self.query_body(text)
        raise Refuse(text)

    def int_splits(self, text):
        """A constant or a configuration-chosen integer -> [(gate, value)]."""
        text = " ".join(text.split())
        if text in self.int_ternary:
            gate, a, b = self.int_ternary[text]
            return [(gate, a), ("not " + gate, b)]
        return [(None, const_eval(text, getattr(self, "env", {})))]

    def set_pred(self, idx, ref):
        """`in(slot(i), SET)` for a concrete or a configuration-chosen set."""
        ref = ref.strip()
        m = split_ternary(ref)
        if m:
            yes = self.set_pred(idx, m[1].strip())
            no = self.set_pred(idx, m[2].strip())
            g = self.pred(m[0])
            return "((%s and %s) or (not %s and %s))" % (g, yes, g, no)
        if ref.startswith("{") and ref.endswith("}"):
            return "in(slot(%s), [%s])" % (
                idx, ", ".join(self.t.name(x) for x in split_top(ref[1:-1])))
        if ref in self.t.set_alias:
            return self.set_pred(idx, self.t.set_alias[ref])
        if ref in self.t.sets:
            return "in(slot(%s), [%s])" % (
                idx, ", ".join(self.t.name(x) for x in self.t.sets[ref]))
        return "in(slot(%s), [%s])" % (idx, self.t.name(ref))

    def legality_helper(self, text):
        """LegalityPredicates spellings that are one observation of the query.

        Each one is inlined instead of becoming an opaque predicate, so the
        evaluator can still answer: a predicate the table cannot evaluate is a
        hole, and a hole is not a rule.
        """
        m = re.fullmatch(r"typeOfLongVectors\s*\(\s*(\d+)\s*,\s*(\w+)\s*\)",
                         text)
        if m:
            return "(%s and sort_of(slot(%s)) == vector)" % (
                self.pred(m.group(2)), m.group(1))
        m = re.fullmatch(r"typeOfExtendedScalars\s*\(\s*(\d+)\s*,"
                         r"\s*(\w+)\s*\)", text)
        if m:
            return "(%s and sort_of(slot(%s)) == scalar)" % (
                self.pred(m.group(2)), m.group(1))
        m = re.fullmatch(r"vectorElementCountIsGreaterThan\s*\(\s*(\d+)\s*,"
                         r"\s*(.+?)\s*\)", text, re.S)
        if m:
            return self._count_cmp(m.group(1), m.group(2), ">")
        m = re.fullmatch(r"vectorElementCountIsLessThanOrEqualTo\s*\("
                         r"\s*(\d+)\s*,\s*(.+?)\s*\)", text, re.S)
        if m:
            return self._count_cmp(m.group(1), m.group(2), "<=")
        m = re.fullmatch(r"numElementsNotPow2\s*\(\s*(\d+)\s*\)", text)
        if m:
            # Upstream: isFixedVector() && !isPowerOf2(getNumElements()).
            # Without the first half this also fires on scalable vectors,
            # whose known-min count is not a power of two either.
            return ("(is_fixed_vector(slot(%s)) and "
                    "not is_pow2(num_elems(slot(%s))))"
                    % (m.group(1), m.group(1)))
        m = re.fullmatch(r"typeInSet\s*\(\s*(\d+)\s*,\s*"
                         r"(\w+|\{[^{}]*\})\s*\)", text, re.S)
        if m:
            return self.set_pred(m.group(1), m.group(2))
        return None

    def _count_cmp(self, idx, amount, op):
        # vectorElementCountIs* are false for a non-fixed-vector; `num_elems`
        # alone reads 0 for a scalar, which makes `<="` true and would accept
        # types upstream rejects.
        alts = []
        for gate, value in self.int_splits(amount):
            cmp_ = "is_fixed_vector(slot(%s)) and num_elems(slot(%s)) %s %d" % (
                idx, idx, op, value)
            alts.append("(%s and %s)" % (gate, cmp_) if gate else cmp_)
        return alts[0] if len(alts) == 1 else "(%s)" % " or ".join(alts)

    def query_helper(self, text):
        """Named C++ query helpers that are one observation spelled in code.

        Both come from the Mips constructor's file-local helpers, and both are
        pure functions of the query, so inlining them keeps the rule form
        rather than hiding a rule behind an opaque predicate:

          `CheckTyN(N, Query, {t, ...})`                  -> `in(slot(N), [...])`
          `CheckTy0Ty1MemSizeAlign(Query, {{v, p, bits, allowUnaligned}, ...})`
              -> one disjunct per entry: the two slots, the memory type's bit
                 width, and -- unless the entry says unaligned access is fine --
                 an alignment at least that wide.
          `isUnalignedMemmoryAccess(memBits, alignBits)` -> `memBits > alignBits`

        Anything else returns None and the caller keeps refusing.
        """
        m = re.fullmatch(r"CheckTyN\s*\(\s*(\d+)\s*,\s*Query\s*,"
                         r"\s*\{(.*)\}\s*\)", text, re.S)
        if m:
            return "in(slot(%s), [%s])" % (
                m.group(1),
                ", ".join(self.t.name(x) for x in split_top(m.group(2))))
        m = re.fullmatch(r"CheckTy0Ty1MemSizeAlign\s*\(\s*Query\s*,"
                         r"\s*\{(.*)\}\s*\)", text, re.S)
        if m:
            alts = []
            for row in split_top(m.group(1)):
                row = row.strip()
                if not (row.startswith("{") and row.endswith("}")):
                    return None
                fields = split_top(row[1:-1])
                if len(fields) != 4:
                    return None
                val, ptr, bits, flag = (f.strip() for f in fields)
                parts = ["in(slot(0), [%s])" % self.t.name(val),
                         "in(slot(1), [%s])" % self.t.name(ptr),
                         "size_of(mem_ty(mem_slot(0))) == %s" % bits]
                try:
                    allowed = const_eval(flag, getattr(self, "env", {}))
                except Refuse:
                    allowed = None
                if allowed is True:
                    pass
                elif allowed is False:
                    parts.append("%s <= align_of(mem_slot(0))" % bits)
                else:
                    parts.append("(%s or %s <= align_of(mem_slot(0)))"
                                 % (self.pred(flag), bits))
                # `and`, not `all(...)`: the grammar's call arguments are
                # expressions, and a type list `[A, B]` is not one.
                alts.append("(%s)" % " and ".join(parts))
            return "(%s)" % " or ".join(alts)
        m = re.fullmatch(r"isUnalignedMemmoryAccess\s*\(\s*(.+?)\s*,"
                         r"\s*(.+?)\s*\)", text, re.S)
        if m:
            return "(%s > %s)" % (self.expr_text(m.group(1)),
                                  self.expr_text(m.group(2)))
        return None

    @staticmethod
    def _single_call(text):
        """True when `text` is one `name(...)` call and nothing else.

        The pass-through regexes use greedy `.*`, so `bits_of(x) != 0 or y`
        also matched them; that silently left `||`/`&&` in the output.
        """
        i = text.find("(")
        if i <= 0 or not re.fullmatch(r"[A-Za-z_]\w*", text[:i].strip()):
            return False
        try:
            return match_paren(text, i) == len(text)
        except Refuse:
            return False

    def _contained_braces(self, text):
        """`llvm::is_contained({v8s8, ...}, DstTy)` (a braced list) -> `in`."""
        key = "llvm::is_contained("
        out, i = [], 0
        while True:
            j = text.find(key, i)
            if j < 0:
                out.append(text[i:])
                return "".join(out)
            k = j + len(key) - 1
            end = match_paren(text, k)
            args = split_top(text[k + 1:end - 1])
            out.append(text[i:j])
            if len(args) == 2 and args[0].strip().startswith("{"):
                items = split_top(args[0].strip()[1:-1].strip())
                if all(re.fullmatch(r"-?\d+", i) for i in items):
                    # An int set only tests an `imm`; for an observation spell
                    # it out as the disjunction the C++ is doing.
                    x = args[1].strip()
                    out.append("(%s)" % " or ".join(
                        "%s == %s" % (x, i) for i in items))
                else:
                    out.append("in(%s, [%s])" % (
                        args[1].strip(),
                        ", ".join(self.t.name(x) for x in items)))
            else:
                out.append(text[j:end])
            i = end

    def _factory_pred(self, text):
        """Predicate factories from `LegalizerInfo.h` -> core predicates."""
        def idx(m):
            return self.slot_index(m.group(1))

        m = re.fullmatch(r"typeIs\s*\(\s*(\w+)\s*,\s*(.+)\)", text, re.S)
        if m:
            # `typeIs` is a free-form LegalityPredicate, not `legalFor`: the
            # spelling matters because LegalizerInfo marks the whole rule
            # opaque for coverage, which a type set would not.
            return "slot(%s) == %s" % (idx(m), self.t.name(m.group(2)))
        m = re.fullmatch(r"typeIsNot\s*\(\s*(\w+)\s*,\s*(.+)\)", text, re.S)
        if m:
            return "slot(%s) != %s" % (idx(m), self.t.name(m.group(2)))
        m = re.fullmatch(r"typeInSet\s*\(\s*(\w+)\s*,\s*\{(.*)\}\)", text, re.S)
        if m:
            return "in(slot(%s), [%s])" % (
                idx(m), ", ".join(self.t.name(x) for x in split_top(m.group(2))))
        m = re.fullmatch(r"typePairInSet\s*\(\s*(\w+)\s*,\s*(\w+)\s*,"
                         r"\s*\{(.*)\}\)", text, re.S)
        if m:
            rows = [split_top(r[1:-1]) for r in split_top(m.group(3))]
            return "in_pairs(slot(%s), slot(%s), [%s])" % (
                idx(m), self.slot_index(m.group(2)),
                ", ".join("[" + ", ".join(self.t.name(x) for x in r) + "]"
                          for r in rows))
        m = re.fullmatch(r"(is\w+)\s*\(\s*(\w+)\s*\)", text)
        if m:
            what = {"isScalar": "scalar", "isVector": "vector",
                    "isScalableVector": "scalable_vector",
                    "isPointer": "pointer",
                    "isPointerVector": "pointer_vector"}.get(m.group(1))
            i = self.slot_index(m.group(2))
            if what == "vector":
                return "sort_of(slot(%s)) == vector" % i
            if what:
                return "sort_of(slot(%s)) == %s" % (i, what)
        m = re.fullmatch(r"elementTypeIs\s*\(\s*(\w+)\s*,\s*(.+)\)", text, re.S)
        if m:
            i, ty = idx(m), self.t.name(m.group(2))
            return ("(sort_of(slot(%s)) == vector and elem_of(slot(%s)) == ty(%s))"
                    % (i, i, ty))
        for name, op in (("scalarNarrowerThan", "<"), ("scalarWiderThan", ">")):
            m = re.fullmatch(r"%s\s*\(\s*(\w+)\s*,\s*(.+)\)" % name, text, re.S)
            if m:
                return "(sort_of(slot(%s)) == scalar and size_of(slot(%s)) %s %s)" % (
                    idx(m), idx(m), op, self.expr_text(m.group(2)))
        for name, op in (("scalarOrEltNarrowerThan", "<"),
                         ("scalarOrEltWiderThan", ">")):
            m = re.fullmatch(r"%s\s*\(\s*(\w+)\s*,\s*(.+)\)" % name, text, re.S)
            if m:
                return "bits_of(slot(%s)) %s %s" % (idx(m), op,
                                                    self.expr_text(m.group(2)))
        m = re.fullmatch(r"scalarOrEltSizeNotPow2\s*\(\s*(\w+)\s*\)", text)
        if m:
            return "not is_pow2(bits_of(slot(%s)))" % idx(m)
        for name, op in (("smallerThan", "<"), ("largerThan", ">")):
            m = re.fullmatch(r"%s\s*\(\s*(\w+)\s*,\s*(\w+)\s*\)" % name, text)
            if m:
                return "size_of(slot(%s)) %s size_of(slot(%s))" % (
                    idx(m), op, self.slot_index(m.group(2)))
        m = re.fullmatch(r"sizeNotMultipleOf\s*\(\s*(\w+)\s*,\s*(.+)\)",
                         text, re.S)
        if m:
            return ("(sort_of(slot(%s)) == scalar and urem(size_of(slot(%s)), %s) != 0)"
                    % (idx(m), idx(m), self.expr_text(m.group(2))))
        m = re.fullmatch(r"atomicOrderingAtLeastOrStrongerThan\s*\(\s*(\w+)"
                         r"\s*,\s*(?:AtomicOrdering::)?(\w+)\s*\)", text)
        if m:
            name = m.group(2)
            if not name.startswith("ordering_"):
                name = _ordering(name)
            return "mem_ordering(mem_slot(%s)) >= %s" % (idx(m), name)
        return None

    # -- mutations -------------------------------------------------------
    def mutation(self, text):
        """`changeTo(i, T)` / `LegalizeMutations::changeTo(i, T)`.

        `changeTo` selects a specific type, so it is `replace(...)` in the DSL:
        the bare `widen(slot, ty)` form keeps the lane count and only moves the
        element size, which is a different mutation.
        """
        text = text.strip()
        m = re.fullmatch(r"(?:LegalizeMutations::)?changeTo\s*\(\s*(\d+)\s*,"
                         r"\s*(.+)\)", text, re.S)
        if not m:
            raise Refuse(text)
        return "slot(%s), replace(%s)" % (m.group(1), self._mut_ty(m.group(2)))

    def _mut_ty(self, text):
        """`changeTo(i, j)`'s numeric form means "the type of slot j"."""
        text = text.strip()
        # `numElements(Query.Types[k])` is the same observation as the
        # `getNumElements()` spelling the patterns below expect.
        text = re.sub(r"numElements\((Query\.Types\[\d+\])\)",
                      r"\1.getNumElements()", text)
        if re.fullmatch(r"\d+", text):
            return "same_as(%s)" % text
        m = re.fullmatch(r"LLT::(?:scalar|integer)\(\s*(.+?)\.getSizeInBits\(\)"
                         r"\s*\)", text, re.S)
        if m:
            src = re.fullmatch(r"Query\.Types\[(\d+)\]", m.group(1).strip())
            if src:
                return "int_of_size(size_of(slot(%s)))" % src.group(1)
        return "ty(%s)" % self.t.name(text)

    def mutation_lambda(self, text, env=None):
        """`bitcastIf`'s new-type lambda -> a `.gisel` type expression."""
        body = re.sub(r"\bQ\.", "Query.", text)
        body = body.replace(".getSizeInBits().getKnownMinValue()", ".getSizeInBits()")
        # A body whose local is reassigned inside an `if` is a `select`; the
        # specialised patterns below only cover a single return.  Try the
        # statement reader first, and keep the old path as the fallback.
        raw = self._lambda_body(text)
        if raw is not None and re.search(r"\bif\s*\(", raw):
            try:
                return self._mutation_body_value(
                    raw, getattr(self, "env", {}))
            except Refuse:
                pass
        body = re.sub(r"numElements\((Query\.Types\[\d+\])\)",
                      r"\1.getNumElements()", body)
        aliases = dict(re.findall(
            r"(?:const\s+)?LLT\s*&?\s*(\w+)\s*=\s*(Query\.Types\[\d+\]);", body))
        for var, val in aliases.items():
            body = re.sub(r"\b%s\b" % var, val, body)
        body = re.sub(r"numElements\((Query\.Types\[\d+\])\)",
                      r"\1.getNumElements()", body)
        m = re.search(r"LLT::vector\(\s*Query\.Types\[(\d+)\]\.getElementCount"
                      r"\(\)\.divideCoefficientBy\((\d+)\)\s*,\s*(\d+)\s*\)", body)
        if m:
            return ("slot(%s), replace(elt_type_to(div_lanes(slot(%s), %s), ty(%s)))"
                    % (m.group(1), m.group(1), m.group(2),
                       self.t.name("s%s" % m.group(3))))
        m = re.search(r"(?:std::)?(?:make_pair|pair(?:<[^>]*>)?)\s*\(?\s*(\d+)\s*,"
                      r"\s*(.+?)\s*\)?\s*;", body, re.S)
        if m:
            idx, ty = m.group(1), m.group(2).strip()
            mm = re.fullmatch(r"LLT::integer\(\s*(.+?)\.getSizeInBits\(\)\s*\)", ty, re.S)
            if mm:
                return "slot(%s), replace(int_of_size(size_of(slot(%s))))" % (
                    idx, idx)
            mm = re.fullmatch(r"(.+?)\.divide\(\s*(\d+)\s*\)", ty, re.S)
            if mm:
                src = re.fullmatch(r"Query\.Types\[(\d+)\]", mm.group(1).strip())
                if src:
                    return "slot(%s), replace(div_lanes(slot(%s), %s))" % (
                        idx, src.group(1), mm.group(2))
            mm = re.fullmatch(r"LLT::vector\(\s*(.+?)\s*,\s*LLT::integer"
                              r"\(\s*(\d+)\s*\)\s*\)", ty, re.S)
            if mm:
                elt = "ty(%s)" % self.t.name("LLT::integer(%s)" % mm.group(2))
                # `LLT::vector(X.getElementCount(), T)` is exactly
                # `X.changeElementType(T)`: both keep the ElementCount,
                # scalable flag included.  Spelling it as
                # `vec_of(num_elems(X), T)` would rebuild a *fixed* vector
                # and silently drop scalability (AArch64 shuffles pointer
                # vectors this way).
                src = re.fullmatch(
                    r"(?:Query\.)?Types\[(\d+)\]\.getElementCount\(\)",
                    mm.group(1).strip())
                if src:
                    return ("slot(%s), replace(elt_type_to(slot(%s), %s))"
                            % (idx, src.group(1), elt))
                return "slot(%s), replace(vec_of(%s, %s))" % (
                    idx, self.count_expr(mm.group(1)), elt)
            mm = re.fullmatch(r"LLT::fixed_vector\(\s*(.+?)\s*,\s*(.+?)\s*\)",
                              ty, re.S)
            if mm:
                lanes = self.int_expr(mm.group(1))
                em = re.fullmatch(r"Query\.Types\[(\d+)\]", mm.group(2).strip())
                elt = ("slot(%s)" % em.group(1) if em
                       else "ty(%s)" % self.t.name(mm.group(2)))
                return "slot(%s), replace(vec_of(%s, %s))" % (idx, lanes, elt)
            mm = re.fullmatch(
                r"Query\.Types\[(\d+)\]\.changeElementSize\(\s*"
                r"Query\.Types\[(\d+)\]\.getSizeInBits\(\)\s*\)\s*"
                r"\.changeElementCount\(\s*Query\.Types\[\1\]"
                r"\.getElementCount\(\)\s*\.divideCoefficientBy\(\s*"
                r"Query\.Types\[(\d+)\]\.getNumElements\(\)\s*\)\s*\)",
                ty, re.S)
            if mm:
                # dst: element size := src's total bits, lanes := dst/src.
                return ("slot(%s), replace(elt_size_to(div_lanes(slot(%s),"
                        " num_elems(slot(%s))), size_of(slot(%s))))"
                        % (idx, mm.group(1), mm.group(3), mm.group(2)))
            mm = re.fullmatch(r"LLT::integer\(\s*(\d+)\s*\)", ty)
            if mm:
                return "slot(%s), ty(%s)" % (
                    idx, self.t.name("LLT::scalar(%s)" % mm.group(1)))
            return "slot(%s), replace(%s)" % (idx, self._mut_ty(ty))
        body = self._lambda_body(text)
        if body is not None:
            try:
                return self._mutation_body_value(body, getattr(self, "env", {}))
            except Refuse:
                pass
        raise Refuse("bitcast lambda %r" % text[:60])

    def count_expr(self, text):
        """`Query.Types[k].getElementCount()` -> `num_elems(slot(k))`."""
        text = text.strip()
        m = re.fullmatch(r"(?:Query\.)?Types\[(\d+)\]\.get(?:ElementCount|NumElements)"
                         r"\(\)", text)
        if m:
            return "num_elems(slot(%s))" % m.group(1)
        m = re.fullmatch(r"(?:Query\.Types\[(\d+)\]|(\w+))", text)
        if m:
            i = m.group(1) or m.group(2)
            return "num_elems(%s)" % ("slot(%s)" % i if m.group(1)
                                      else "ty(%s)" % self.t.name(i))
        return self.int_expr(text)

    def local_bool(self, text):
        """Inline a `static bool F(...)` helper used as a constructor gate."""
        m = re.fullmatch(r"(\w+)\s*\((.*)\)", text.strip(), re.S)
        if not m or m.group(1) not in self.bool_helpers:
            return None
        tmpl = self.bool_helpers[m.group(1)]
        call = split_top(m.group(2))
        if len(call) != len(tmpl["params"]):
            raise Refuse(text)
        body = tmpl["expr"]
        for p, a in zip(tmpl["params"], call):
            body = re.sub(r"\b%s\b" % re.escape(p), a, body)
        return self.pred(body)

    # -- local LegalityPredicate helpers ---------------------------------
    def local_pred(self, text):
        """Inline a `static LegalityPredicate F(...)` helper."""
        m = re.fullmatch(r"(\w+)\s*\((.*)\)", text.strip(), re.S)
        if not m or m.group(1) not in self.helpers:
            return None
        # A helper whose body delegates to the overload of its own name
        # (`isRegisterType(ST, TypeIdx)` calls `isRegisterType(ST, LLT)`)
        # cannot be inlined: following it recurses without bound.  Refuse so
        # the caller can copy the call through `opaque` instead.
        inline = getattr(self, "_inline", None)
        if inline is None:
            inline = self._inline = set()
        if m.group(1) in inline:
            raise Refuse(text)
        tmpl = self.helpers[m.group(1)]
        if tmpl.get("bool"):
            # `parse_helpers` keys by name, so a `bool` overload can shadow
            # the predicate template; inlining the wrong one is not an option.
            raise Refuse(text)
        call = split_top(m.group(2))
        if len(call) != len(tmpl["params"]):
            raise Refuse(text)
        sub = dict(zip(tmpl["params"], call))
        if tmpl.get("stmt"):
            body = tmpl["body"]
            for p in tmpl["params"]:
                body = re.sub(r"\b%s\b" % re.escape(p), sub[p], body)
            inline.add(m.group(1))
            try:
                return self.body_pred(body)
            finally:
                inline.discard(m.group(1))
        idx = _subst(tmpl["idx"], sub)
        typeset = _subst(tmpl["set"], sub)
        elems = self.t.sets.get(typeset) or [typeset]
        body = tmpl["body"]
        for p in tmpl["params"]:
            body = re.sub(r"\b%s\b" % re.escape(p), sub[p], body)
        pred = self.query_body(body)
        return "(in(slot(%s), [%s]) and %s)" % (
            idx, ", ".join(self.t.name(e) for e in elems), pred)

    def query_body(self, body):
        """A `Query.Types[...]` lambda body -> a `.gisel` predicate."""
        body = re.sub(r"\bQ\.", "Query.", body)
        out = []
        for chunk in re.split(r"(Query\.Types\[[^\]]+\])", body):
            m = re.fullmatch(r"Query\.Types\[(.+)\]", chunk)
            if m:
                out.append("__T(%s)" % m.group(1).strip())
            else:
                out.append(chunk)
        text = "".join(out)
        text = re.sub(r"Query\.MMODescrs\[(\d+)\]\.MemoryTy", r"MEM(\1)", text)
        text = re.sub(r"Query\.MMODescrs\[(\d+)\]\.Ordering", r"ORD(\1)", text)
        m = re.fullmatch(r"(.+?)\s*(==|!=)\s*AtomicOrdering::NotAtomic", text)
        if m:
            # NotAtomic is the bottom of the ordering lattice, and the DSL has
            # no tag for it: `!= NotAtomic` is `>= Unordered`.
            lhs = self.pred(m.group(1))
            return "%s %s ordering_unordered" % (
                lhs, ">=" if m.group(2) == "!=" else "<")
        text = re.sub(r"AtomicOrdering::(\w+)",
                      lambda m: _ordering(m.group(1)), text)
        text = re.sub(r"isPowerOf2_32\(", "is_pow2(", text)
        text = re.sub(r"MEM\((\d+)\)", r"mem_ty(mem_slot(\1))", text)
        text = re.sub(r"mem_ty\(mem_slot\((\d+)\)\)\.getSizeInBits\(\)",
                      r"size_of(mem_ty(mem_slot(\1)))", text)
        text = re.sub(r"mem_ty\(mem_slot\((\d+)\)\)\.isScalar\(\)",
                      r"sort_of(mem_ty(mem_slot(\1))) == scalar", text)
        text = re.sub(r"mem_ty\(mem_slot\((\d+)\)\)\.isVector\(\)",
                      r"sort_of(mem_ty(mem_slot(\1))) == vector", text)
        text = re.sub(r"ORD\((\d+)\)", r"mem_ordering(mem_slot(\1))", text)
        # `isValid()` / `isPointerOrPointerVector()` must be folded before the
        # observation patterns below: those use a non-greedy `(.+?)` between
        # `__T(` and the call, so an unfolded method name would be swallowed
        # into the slot of the next observation.
        text = re.sub(r"__T\(([^()]*)\)\.isValid\(\)", "true", text)
        text = re.sub(
            r"__T\(([^()]*)\)\.isPointerOrPointerVector\(\)",
            r"(sort_of(slot(\1)) == pointer"
            r" || sort_of(slot(\1)) == pointer_vector)",
            text)
        for pat, rep in (
                (r"__T\((.+?)\)\.getScalarType\(\)\.isFloat16\(\)",
                 r"is_float16(scalar_of(slot(\1)))"),
                (r"__T\((.+?)\)\.getScalarType\(\)\.isBFloat16\(\)",
                 r"is_bfloat16(scalar_of(slot(\1)))"),
                (r"__T\((.+?)\)\.getScalarSizeInBits\(\)", r"bits_of(slot(\1))"),
                (r"__T\((.+?)\)\.getElementCount\(\)\.getKnownMinValue\(\)",
                 r"num_elems(slot(\1))"),
                (r"__T\((.+?)\)\.getElementCount\(\)\.getFixedValue\(\)",
                 r"num_elems(slot(\1))"),
                (r"__T\((.+?)\)\.getNumElements\(\)", r"num_elems(slot(\1))"),
                (r"__T\((.+?)\)\.getScalarType\(\)\.getSizeInBits\(\)",
                 r"bits_of(scalar_of(slot(\1)))"),
                (r"__T\((.+?)\)\.getElementType\(\)\.getSizeInBits\(\)",
                 r"bits_of(elem_of(slot(\1)))"),
                (r"__T\((.+?)\)\.getSizeInBits\(\)", r"size_of(slot(\1))"),
                (r"__T\((.+?)\)\.getElementType\(\)", r"elem_of(slot(\1))"),
                (r"__T\((.+?)\)\.getAddressSpace\(\)", r"addr_space(slot(\1))"),
                (r"__T\((.+?)\)\.isScalableVector\(\)",
                 r"sort_of(slot(\1)) == scalable_vector"),
                (r"__T\((.+?)\)\.isFixedVector\(\)",
                 r"(sort_of(slot(\1)) == vector and not sort_of(slot(\1)) == scalable_vector)"),
                (r"__T\((.+?)\)\.isPointerVector\(\)",
                 r"sort_of(slot(\1)) == pointer_vector"),
                (r"__T\((.+?)\)\.isPointer\(\)", r"sort_of(slot(\1)) == pointer"),
                (r"__T\((.+?)\)\.isVector\(\)", r"sort_of(slot(\1)) == vector"),
                (r"__T\((.+?)\)\.isScalar\(\)", r"sort_of(slot(\1)) == scalar"),
                (r"__T\((.+?)\)", r"slot(\1)"),
        ):
            text = re.sub(pat, rep, text)
        text = re.sub(r"LLT::scalar\(\s*(\d+)\s*\)", r"s\1", text)
        text = re.sub(r"LLT::fixed_vector\(\s*(\d+)\s*,\s*LLT::scalar\(\s*(\d+)\s*\)\s*\)",
                      r"v\1s\2", text)
        text = re.sub(r"LLT::pointer\(\s*(\d+)\s*,\s*(\d+)\s*\)", r"p\1:\2", text)
        text = re.sub(r"\bslot\(\s*(\w+)\s*\)",
                      lambda m: "slot(%s)" % self.slot_index(m.group(1)), text)
        m = re.fullmatch(r"(.+?)\s*&\s*(\d+)\s*", text, re.S)
        if m:
            # `x & 1` used as a boolean: C++ says "odd".
            if int(m.group(2)) != 1:
                raise Refuse("bit test %r" % text)
            return "(urem(%s, 2) != 0)" % m.group(1).strip()
        if "&&" in text or "||" in text or text.startswith("!"):
            return self.pred(text)
        cmp = self._split_cmp(text)
        if cmp is not None:
            lhs, op, rhs = cmp
            return "%s %s %s" % (_arith(lhs.strip()), op, _arith(rhs.strip()))
        out = _arith(text)
        m = re.match(r"([A-Za-z_]\w*)\s*\(", out.strip())
        if m and m.group(1) not in DSL_CALLS:
            # e.g. AMDGPU's `hasBufferRsrcWorkaround(T)`: a helper defined in
            # the target, not an observation of the query.
            raise Refuse("host helper %r" % m.group(1))
        return out

    # -- mutation helpers (static LegalizeMutation F(unsigned i)) ---------
    def mutation_helper_value(self, name, idx, env):
        """Expand `oneMoreElement(0)` and friends into a `.gisel` type value.

        The helper is recognised by the *shape* of its body, not by its name:
        any `static LegalizeMutation F(unsigned i)` whose lambda is a chain of
        local declarations plus `return std::pair(i, <type expr>)`.  A body
        that uses anything else is refused, not guessed.
        """
        spec = self._mutation_helper_spec(name)
        if spec is None:
            return None
        henv = dict(env or {})
        henv[spec["param"]] = idx
        try:
            return self._mutation_body_value(spec["body"], henv)
        except Refuse:
            return None

    def _mutation_helper_spec(self, name):
        cache = self.__dict__.setdefault("_mutation_helpers", {})
        if name in cache:
            return cache[name]
        cache[name] = None
        src = getattr(self, "source", "")
        m = re.search(r"static\s+LegalizeMutation\s+%s\s*\(([^)]*)\)\s*\{"
                      % re.escape(name), src)
        if not m:
            return None
        params = split_top(m.group(1))
        if len(params) != 1:
            return None
        param = params[0].replace("*", " ").replace("&", " ").split()[-1]
        try:
            close = _match_brace(src, m.end() - 1)
        except Refuse:
            return None
        body = src[m.end():close - 1]
        lam = self._lambda_body(body)
        if lam is None:
            return None
        spec = {"param": param, "body": lam}
        cache[name] = spec
        return spec

    @staticmethod
    def _lambda_body(text):
        """The statements inside the first lambda body in ``text``."""
        bopen = text.find("{")
        if bopen < 0:
            return None
        try:
            bclose = _match_brace(text, bopen)
        except Refuse:
            return None
        return text[bopen + 1:bclose - 1]

    def _mutation_body_value(self, body, env):
        """`{ decls; [if (...) X = ...;] return pair(i, T); }` -> a rule.

        An `if` that only reassigns local variables is a `select` in the value
        language; declarations inside the block do not escape it.  Anything
        else (a loop, a host call) is refused, not guessed.
        """
        body = re.sub(r"//[^\n]*", "", body)
        stmts = [st.strip().rstrip(";").strip()
                 for st, _ in split_statements(body)]
        stmts = [st for st in stmts if st and not _is_assert(st)]
        ret = None
        if stmts and stmts[-1].startswith("return "):
            ret = stmts.pop()[len("return "):].strip()
        elif any(st.startswith("return ") for st in stmts):
            raise Refuse("return in the middle")
        if ret is None:
            raise Refuse("no return")
        binds = {}
        self._mutation_stmts(stmts, binds, env)
        pm = re.fullmatch(
            r"(?:std::)?(?:make_pair|pair(?:\s*<[^>]*>)?)\s*\((.*)\)",
            ret, re.S)
        if not pm:
            raise Refuse(ret)
        args = split_top(pm.group(1))
        if len(args) != 2:
            raise Refuse(ret)
        idx_text, ty_expr = args[0].strip(), args[1].strip()
        value = self._helper_value(ty_expr, binds, env)
        return "slot(%s), replace(%s)" % (idx_of(idx_text, env), value)

    def _mutation_stmts(self, stmts, binds, env):
        """Apply declarations, assignments and `if` reassignments to `binds`."""
        for st in stmts:
            dm = re.fullmatch(
                r"(?:const\s+)?(?:unsigned|int|auto|LLT|bool)\s*&?\s*(\w+)"
                r"\s*=\s*(.+)", st, re.S)
            if dm:
                binds[dm.group(1)] = self._helper_value(
                    dm.group(2).strip(), binds, env)
                continue
            if st.startswith("if"):
                cond, then = self._if_parts(st)
                block = [x.strip() for x, _ in split_statements(then)
                         if x.strip() and not _is_assert(x)]
                inner = dict(binds)
                self._mutation_stmts(block, inner, env)
                g = self._helper_pred(cond, binds, env)
                for var, val in inner.items():
                    if var in binds and val != binds[var]:
                        binds[var] = "select(%s, %s, %s)" % (
                            g, val, binds[var])
                continue
            am = re.fullmatch(r"(\w+)\s*=\s*(.+)", st, re.S)
            if am and am.group(1) in binds:
                binds[am.group(1)] = self._helper_value(
                    am.group(2).strip(), binds, env)
                continue
            raise Refuse(st)

    def _helper_value(self, text, binds, env):
        """A C++ type expression inside a mutation helper -> `.gisel` value."""
        t = _strip_outer_parens(text.strip().rstrip(";"))
        if not t:
            raise Refuse(text)
        if re.fullmatch(r"\d+[uUlL]*", t):
            return str(int(re.sub(r"[uUlL]+$", "", t)))
        if t.startswith("-"):
            return "sub(0, %s)" % self._helper_value(t[1:], binds, env)
        m = re.fullmatch(r"1\s*<<\s*Log2_32_Ceil\((.*)\)", t, re.S)
        if m:
            # `1 << ceil(log2(n))` is pow2ceil(n), the value primitive.
            return "pow2_ceil(%s)" % self._helper_value(m.group(1), binds, env)
        m = re.fullmatch(r"(?:llvm::)?alignTo<\s*(\d+)\s*>\((.*)\)",
                         t, re.S)
        if m:
            return "align_to(%s, %s)" % (
                self._helper_value(m.group(2), binds, env), m.group(1))
        m = re.fullmatch(r"(?:llvm::)?alignTo\((.*)\)", t, re.S)
        if m:
            args = split_top(m.group(1))
            if len(args) != 2:
                raise Refuse(text)
            return "align_to(%s, %s)" % (
                self._helper_value(args[0], binds, env),
                self._helper_value(args[1], binds, env))
        tern = split_ternary(t)
        if tern:
            return "select(%s, %s, %s)" % (
                self._helper_pred(tern[0], binds, env),
                self._helper_value(tern[1], binds, env),
                self._helper_value(tern[2], binds, env))
        for ops, names in (("+-", {"+": "add", "-": "sub"}),
                           ("*/%", {"*": "mul", "/": "udiv", "%": "urem"})):
            i, c = _binop_at(t, ops)
            if i >= 0:
                return "%s(%s, %s)" % (
                    names[c],
                    self._helper_value(t[:i], binds, env),
                    self._helper_value(t[i + 1:], binds, env))
        if t in binds:
            return binds[t]
        m = re.fullmatch(r"Query\.Types\[(.+)\]", t, re.S)
        if m:
            return "slot(%s)" % idx_of(m.group(1), env)
        m = re.fullmatch(
            r"Query\.MMODescrs\[(.+)\]\.MemoryTy\."
            r"get(?:SizeInBits|ScalarSizeInBits)\(\)", t, re.S)
        if m:
            return "size_of(mem_ty(mem_slot(%s)))" % idx_of(m.group(1), env)
        m = re.fullmatch(r"Query\.MMODescrs\[(.+)\]\.MemoryTy", t, re.S)
        if m:
            return "mem_ty(mem_slot(%s))" % idx_of(m.group(1), env)
        m = re.fullmatch(r"Query\.MMODescrs\[(.+)\]\.AlignInBits", t, re.S)
        if m:
            return "align_of(mem_slot(%s))" % idx_of(m.group(1), env)
        m = re.fullmatch(r"(\w+)\.get(NumElements|SizeInBits|ElementType|"
                         r"ScalarSizeInBits|ScalarType)\(\)", t)
        if m:
            base = self._helper_value(m.group(1), binds, env)
            if m.group(2) == "NumElements":
                return "num_elems(%s)" % base
            if m.group(2) in ("SizeInBits", "ScalarSizeInBits"):
                return "size_of(%s)" % base
            if m.group(2) == "ElementType":
                return "elem_of(%s)" % base
            return "scalar_of(%s)" % base
        m = re.fullmatch(r"LLT::(?:integer|scalar)\((.*)\)", t, re.S)
        if m:
            return "int_of_size(%s)" % self._helper_value(
                m.group(1), binds, env)
        m = re.fullmatch(r"LLT::fixed_vector\((.*)\)", t, re.S)
        if m:
            args = split_top(m.group(1))
            if len(args) != 2:
                raise Refuse(text)
            return "vec_of(%s, %s)" % (
                self._helper_value(args[0], binds, env),
                self._as_elt(args[1], binds, env))
        m = re.fullmatch(r"LLT::scalarOrVector\((.*)\)", t, re.S)
        if m:
            args = split_top(m.group(1))
            if len(args) != 2:
                raise Refuse(text)
            cm = re.fullmatch(r"ElementCount::getFixed\((.*)\)",
                              _strip_outer_parens(args[0]), re.S)
            if not cm:
                raise Refuse(text)
            return "scalar_or_vec_of(%s, %s)" % (
                self._helper_value(cm.group(1), binds, env),
                self._as_elt(args[1], binds, env))
        m = re.fullmatch(r"([A-Za-z_]\w*)\s*\((.*)\)", t, re.S)
        if m and "::" not in t:
            value = self._llt_helper_value(
                m.group(1), split_top(m.group(2)), binds, env)
            if value is not None:
                return value
        if re.fullmatch(r"[A-Za-z_]\w*", t):
            name = self.t.name(t)
            if name != t:
                return "ty(%s)" % name
        raise Refuse(text)

    def _as_elt(self, text, binds, env):
        """`LLT::fixed_vector(n, X)`: X is an LLT, or a scalar width."""
        value = self._helper_value(text, binds, env)
        if not _looks_type(value):
            return "int_of_size(%s)" % value
        return value

    def _helper_pred(self, text, binds, env):
        t = text.strip()
        parts = _split_top_op(t, "||")
        if len(parts) > 1:
            return "(%s)" % " or ".join(
                self._helper_pred(p, binds, env) for p in parts)
        parts = _split_top_op(t, "&&")
        if len(parts) > 1:
            return "(%s)" % " and ".join(
                self._helper_pred(p, binds, env) for p in parts)
        if t.startswith("!") and not t.startswith("!="):
            return "not (%s)" % self._helper_pred(t[1:], binds, env)
        cmp = self._split_cmp(t)
        if cmp:
            lhs, op, rhs = cmp
            return "(%s %s %s)" % (
                self._helper_value(lhs.strip(), binds, env), op,
                self._helper_value(rhs.strip(), binds, env))
        raise Refuse(text)

    def _llt_helper_value(self, name, args, binds, env):
        """Inline a `static LLT F(...)` helper (declarations + early returns)."""
        cache = self.__dict__.setdefault("_llt_helpers", {})
        if name not in cache:
            cache[name] = self._llt_helper_spec(name)
        spec = cache[name]
        if spec is None or len(args) != len(spec["params"]):
            return None
        local = {}
        try:
            for param, arg in zip(spec["params"], args):
                local[param] = self._helper_value(arg, binds, env)
            return self._llt_body_value(spec["body"], local, env)
        except Refuse:
            return None

    def _llt_helper_spec(self, name):
        src = getattr(self, "source", "")
        m = re.search(r"static\s+LLT\s+%s\s*\(([^)]*)\)\s*\{"
                      % re.escape(name), src)
        if not m:
            return None
        params = [p.replace("*", " ").replace("&", " ").split()[-1]
                  for p in split_top(m.group(1))]
        try:
            close = _match_brace(src, m.end() - 1)
        except Refuse:
            return None
        return {"params": params, "body": src[m.end():close - 1]}

    def _llt_body_value(self, body, binds, env):
        body = re.sub(r"//[^\n]*", "", body)
        branches, final = [], None
        for st, _ in split_statements(body):
            st = st.strip().rstrip(";").strip()
            if not st or _is_assert(st):
                continue
            if st.startswith("return "):
                final = self._helper_value(st[len("return "):], binds, env)
                continue
            im = re.fullmatch(r"if\s*\((.*)\)\s*\{(.*)\}", st, re.S)
            if im:
                rm = re.fullmatch(r"\s*return\s+(.+?);?\s*", im.group(2), re.S)
                if not rm:
                    raise Refuse(st)
                branches.append((
                    self._helper_pred(im.group(1), binds, env),
                    self._helper_value(rm.group(1), binds, env)))
                continue
            dm = re.fullmatch(
                r"(?:const\s+)?(?:unsigned|int|auto|LLT|bool)\s*&?\s*(\w+)"
                r"\s*=\s*(.+)", st, re.S)
            if not dm:
                raise Refuse(st)
            binds[dm.group(1)] = self._helper_value(dm.group(2), binds, env)
        if final is None:
            raise Refuse(body)
        for cond, value in reversed(branches):
            final = "select(%s, %s, %s)" % (cond, value, final)
        return final

    # -- align helpers ---------------------------------------------------
    def align_variants(self, text):
        """`getScalarMemAlign(n)` -> [(gate, integer), ...], mutually exclusive."""
        m = re.fullmatch(r"(\w+)\s*\((.*)\)", text.strip(), re.S)
        if not m or m.group(1) not in self.aligns:
            raise Refuse("memdesc align %r" % text)
        param, cond, a, b = self.aligns[m.group(1)]
        arg = m.group(2).strip()
        if param:
            a = re.sub(r"\b%s\b" % re.escape(param), arg, a)
            b = re.sub(r"\b%s\b" % re.escape(param), arg, b)
        gate = self.pred(cond)
        out = []
        for g, val, neg in ((gate, a, False), ("not " + gate, b, True)):
            if re.fullmatch(r"-?\d+", val.strip()):
                out.append((None if not neg and val == b and val == a else g,
                            int(val)))
            elif val.strip() == "XLen":
                out.append(("%s and %s" % (g, self.pred("XLen == 64")), 64))
                out.append(("%s and %s" % (g, self.pred("XLen == 32")), 32))
            else:
                if not re.fullmatch(r"-?\d+", arg):
                    raise Refuse("memdesc align %r" % text)
                out.append((g, int(arg)))
        # `getScalarMemAlign(8)` is 8 either way: drop the gates.
        if all(v == out[0][1] for _, v in out):
            return [(None, out[0][1])] * 1 if False else [(None, out[0][1])]
        return out

    def slot_index(self, name):
        name = name.strip()
        if re.fullmatch(r"-?\d+", name):
            return name
        env = getattr(self, "env", {})
        v = env.get(name, name)
        if not isinstance(v, int):
            raise Refuse("slot index %r" % name)
        return str(v)

    def int_expr(self, text):
        """A C++ integer constant/expression -> a `.gisel` Int expression."""
        t = text.strip()
        if t.startswith("(") and t.endswith(")") and self._balanced(t[1:-1]):
            return self.int_expr(t[1:-1])
        m = split_ternary(t)
        if m:
            return "select(%s, %s, %s)" % (self.pred(m[0]),
                                           self.int_expr(m[1].strip()),
                                           self.int_expr(m[2].strip()))
        if re.fullmatch(r"-?\d+", t):
            return t
        if t in ("XLen", "ST.getXLen()", "STI.getXLen()"):
            return 'select(%s, 64, 32)' % self.feature("XLen == 64")
        if t.replace(" ", "") in ("2*XLen", "XLen*2"):
            return 'select(%s, 128, 64)' % self.feature("XLen == 64")
        m = re.fullmatch(r"(\w+)\.getNumElements\(\)", t)
        if m:
            return "num_elems(ty(%s))" % self.t.name(m.group(1))
        if t in ("ST.getELen()", "STI.getELen()"):
            return 'select(%s, 64, 32)' % self.feature("ST.getELen() == 64")
        if re.fullmatch(r"[A-Za-z_][\w.()]*", t):
            return 'select(%s, 64, 32)' % self.feature(t)
        raise Refuse(text)

    def expr_text(self, text):
        """One side of a comparison: an int, a type name, or an observation."""
        text = text.strip()
        if split_ternary(text):
            return self.int_expr(text)
        if text in self.features:
            return self.features[text]
        if text in self.named_preds:
            return "(%s)" % self.named_preds[text]
        if re.fullmatch(r"-?\d+", text):
            return text
        if text in ("true", "false") or re.fullmatch(r"ordering_\w+", text):
            return text
        if text in ("XLen", "ST.getXLen()", "STI.getXLen()", "ST.getELen()",
                    "STI.getELen()"):
            return self.int_expr(text)
        m = re.fullmatch(r"(ST|STI)\..+", text)
        if m:
            return self.feature(text)
        if "Query." in text:
            return self.query_body(text)
        if text in ("scalar", "vector", "scalable_vector", "pointer",
                    "pointer_vector"):
            return text          # a kind tag, not a type name
        if re.fullmatch(r"[A-Za-z_]\w*\s*\(.*\)", text, re.S):
            return text          # already `.gisel` (`bits_of(slot(0))`, ...)
        env = getattr(self, "env", {})
        if isinstance(env.get(text), int):
            return str(env[text])
        consts = getattr(self.t, "int_consts", {})
        if text in consts:
            return str(consts[text])
        named = self.t.name(text)
        if named == text.strip():
            # Not a declared type and not an observation: a local integer or a
            # host helper.  Wrapping it in `ty()` emits text the corpus parser
            # rejects, so refuse instead.
            raise Refuse("unknown name %r" % text)
        return "ty(%s)" % named

    @staticmethod
    def _split_cmp(text):
        depth, i = 0, 0
        while i < len(text):
            c = text[i]
            if c in "([":
                depth += 1
            elif c in ")]":
                depth -= 1
            elif depth == 0:
                if text.startswith(("==", "!=", "<=", ">="), i):
                    return text[:i], text[i:i + 2], text[i + 2:]
                if text.startswith(("<<", ">>"), i):
                    i += 2              # shift, not a comparison
                    continue
                if c in "<>":
                    return text[:i], c, text[i + 1:]
            i += 1
        return None

    @staticmethod
    def _single_return(body):
        """The expression of a lambda body that is one `return <expr>;`."""
        stmts = [x.strip() for x, _ in split_statements(body) if x.strip()]
        if len(stmts) != 1:
            return None
        st = stmts[0].rstrip(";").strip()
        if not st.startswith("return "):
            return None
        return st[len("return "):].strip() or None

    def pred_lambda(self, text):
        text = text.strip()
        if text.startswith("LegalityPredicate(") and text.endswith(")"):
            return self.pred_lambda(text[len("LegalityPredicate("):-1])
        m = re.match(r"\[.*?\]\s*\((.*?)\)\s*(?:->\s*[^({]+?)?\s*\{(.*)\}$",
                     text, re.S)
        if m:
            try:
                return self.body_pred(m.group(2))
            except Refuse:
                # A helper defined in the target file (`isLoadStoreLegal(ST,
                # Query)`) is not part of the predicate algebra.  Copy the
                # expression through `opaque(...)` instead of dropping the
                # rule: the generated C++ can evaluate it, gisel cannot, and
                # the escape is listed in the corpus header and in
                # `--emit=core` `escapes`.
                expr = self._single_return(m.group(2))
                if expr is None:
                    raise
                expr = " ".join(expr.split())
                # A call to a lambda declared in the constructor: inline it,
                # because the swapped constructor body no longer declares it.
                lm = re.fullmatch(r"(\w+)\s*\(\s*(?:Query|Q)\s*,\s*(.+)\)",
                                  expr, re.S)
                if lm and lm.group(1) in getattr(self, "local_lambdas", {}):
                    return self.pred(expr)
                self.opaque.append(expr)
                return 'opaque("%s")' % (expr.replace("\\", "\\\\")
                                              .replace('"', '\\"'))
        if re.match(r"\[[^\]]*\]\s*\(", text):
            # A lambda whose shape this parser does not know.  Calling
            # `pred()` back would re-dispatch to this function and recurse
            # until the interpreter gives up, with no line to point at.
            raise Refuse("lambda shape not understood")
        return self.pred(text)

    def body_pred(self, body):
        """A lambda body of `if (...)` early returns -> one predicate."""
        if self.opcode:
            body = re.sub(r"Query\.Opcode", self.opcode, body)
        # `assert(x && "msg");` states a contract the C++ build enforces; it
        # neither accepts nor rejects a query, so it is not part of the rule.
        stmts = [x.strip() for x, _ in split_statements(body)
                 if x.strip() and not _is_assert(x)]
        return self._body_stmts(self._subst_locals(stmts))

    def _subst_locals(self, stmts):
        """Fold `const LLT T = Query.Types[i];` aliases into their uses."""
        env, rest = {}, []
        for st in stmts:
            m = re.fullmatch(r"(?:const\s+)?(?:unsigned|int|bool|LLT|auto)\s*&?\s*"
                             r"(\w+)\s*=\s*(.+?);?", st, re.S)
            if m:
                # `const LLT Ty1 = Query.Types[0], Ty2 = Query.Types[1];`
                # declares two aliases in one statement; split them on the
                # top-level commas before folding.
                decls = [("".join((m.group(1), " =")), m.group(2))]
                head = st[:st.index(m.group(1))]
                decls = [(m.group(1), m.group(2))]
                if "," in m.group(2):
                    decls, _ = [], None
                    rest_text = st[st.index(m.group(1)):]
                    for part in split_top(rest_text):
                        pm = re.fullmatch(r"(\w+)\s*=\s*(.+)", part.strip(), re.S)
                        if not pm:
                            decls, _ = [], None
                            break
                        decls.append((pm.group(1), pm.group(2)))
                if not decls:
                    rest.append(st)
                    continue
                for name, val in decls:
                    val = val.strip().rstrip(";").strip()
                    if "Query." not in val:
                        try:
                            env[name] = str(const_eval(val, {}))
                            continue
                        except Refuse:
                            pass
                    env[name] = " ".join(val.split())
                continue
            rest.append(st)
        text = "; ".join(x.rstrip(";") for x in rest)
        for _ in range(4):          # names may alias other names
            before = text
            for name, val in env.items():
                text = re.sub(r"\b%s\b" % re.escape(name),
                              lambda _m, v=val: v, text)
            if text == before:
                break
        return [x.strip() for x, _ in split_statements(text)
                if x.strip() and x.strip() != ";"]

    def _body_stmts(self, stmts):
        stmts = [x for x in stmts if not _is_assert(x)]
        if not stmts:
            raise Refuse("empty predicate lambda")
        st = stmts[0].rstrip(";").strip()
        if st.startswith("if"):
            cond, then = self._if_parts(st)
            cond = self.pred(cond)
            rest = self._body_stmts(stmts[1:]) if len(stmts) > 1 else None
            # `_if_parts` flattens `if (c) { ... }` to its statements; more
            # than one (or a declaration first) means the block is a nested
            # predicate, not a `return X`.
            block = [x.strip() for x, _ in split_statements(then)
                     if x.strip() and not _is_assert(x)]
            if len(block) > 1:
                val = self._body_stmts(self._subst_locals(block))
                if rest is None:
                    return "(%s and %s)" % (cond, val)
                return "((%s and %s) or (not (%s) and %s))" % (
                    cond, val, cond, rest)
            if block:
                then = block[0]
            # A trailing `return false` adds nothing: the fold below would
            # spell `(c and v) or (not c and false)`, which is just `c and v`.
            if rest == "not true":
                if then in ("return false", "return true"):
                    return cond if then == "return true" else "not (%s)" % cond
                if then.startswith("return "):
                    val = self.pred(then[len("return "):])
                    return "(%s and %s)" % (cond, val)
            if then in ("return false", "return true"):
                val = "false" if then == "return false" else "true"
                if rest is None:
                    return cond if val == "true" else "not (%s)" % cond
                if val == "false":
                    return "(%s and %s)" % ("not (%s)" % cond, rest)
                return "((%s) or (not (%s) and %s))" % (cond, cond, rest)
            if not then.startswith("return "):
                raise Refuse(st)
            val = self.pred(then[len("return "):])
            if rest is None:
                return "(%s and %s)" % (cond, val)
            return "((%s and %s) or (not (%s) and %s))" % (cond, val, cond, rest)
        if st.startswith("switch"):
            i = st.index("(")
            close = match_paren(st, i) - 1
            body = st[close + 1:].strip()
            tail = ""
            if body.startswith("{"):
                end = match_brace(body, 0)
                st, tail = st[:close + 1] + body[:end], body[end:].strip()
            rest = (([tail] if tail else []) + stmts[1:])
            p = self.switch_pred(st)
            rp = self._body_stmts(rest) if rest else None
            return p if rp is None else "(%s and %s)" % (p, rp)
        if st.startswith("return"):
            expr = st[len("return"):].strip()
            if expr == "true":
                return "true"
            if expr == "false":
                return "not true"
            return self.pred(expr)
        raise Refuse(st)

    @staticmethod
    def _if_parts(st):
        i = st.index("(")
        close = match_paren(st, i) - 1
        cond, rest = st[i + 1:close], st[close + 1:].strip()
        while rest.startswith("{"):
            end = match_brace(rest, 0)
            rest = (rest[1:end - 1].strip() + " " + rest[end:].strip()).strip()
        return " ".join(cond.split()), rest.rstrip(";").strip()

    def switch_pred(self, st):
        """`switch (Q.Types[i].getSizeInBits()) { case N: ... }` -> `in`."""
        i = st.index("(")
        close = match_paren(st, i) - 1
        subject = st[i + 1:close].strip()
        body = st[close + 1:].strip()
        if body.startswith("{"):
            body = body[1:match_brace(body, 0) - 1]
        cases = [int(x) for x in re.findall(r"case\s+(-?\d+)\s*:", body)]
        if not cases:
            raise Refuse(st)
        negate = bool(re.search(r"default:\s*return\s+true", body))
        positive = bool(re.search(r"(return\s+true|break\s*;)", body))
        if negate:
            positive = not positive
        if not positive:
            raise Refuse(st)
        subj = self.pred(subject)
        if subj.startswith("imm("):
            out = "in(%s, [%s])" % (subj, ", ".join(str(c) for c in cases))
        else:
            # An int set only tests an `imm`; the C++ switch is a disjunction.
            out = "(%s)" % " or ".join("%s == %s" % (subj, c) for c in cases)
        return "not " + out if negate else out

    def _types_in_pred(self, out):
        # `in(slot(0), ty(s3))` -> the set/list members are type references
        def sub(m):
            return self.t.name(m.group(0))
        return re.sub(r"\b[A-Za-z_]\w*\b", sub, out)

    @staticmethod
    def _balanced(text):
        depth = 0
        for c in text:
            if c == "(":
                depth += 1
            elif c == ")":
                depth -= 1
                if depth < 0:
                    return False
        return depth == 0

    @staticmethod
    def _split_op(text, op):
        parts, depth, cur, i = [], 0, [], 0
        while i < len(text):
            c = text[i]
            if c == "(":
                depth += 1
            elif c == ")":
                depth -= 1
            if depth == 0 and text.startswith(op, i):
                parts.append("".join(cur).strip())
                cur = []
                i += len(op)
                continue
            cur.append(c)
            i += 1
        parts.append("".join(cur).strip())
        return parts


def _subst(text, sub):
    for k, v in sub.items():
        text = re.sub(r"\b%s\b" % re.escape(k), v, text)
    return text


def parse_helpers(source):
    """`static LegalityPredicate F(...) { ... return all(typeInSet(i, S), P); }`"""
    # Line comments would glue to the next statement in `split_statements`
    # (the `//` line has no `;` of its own), so the helper is read without
    # them.  Line count is preserved, and refusals carry constructor lines.
    source = "\n".join(strip_comments(source))
    out = {}
    for m in re.finditer(r"static\s+LegalityPredicate\s+(\w+)\s*\((.*?)\)\s*\{(.*?)^\}",
                         source, re.S | re.M):
        name, params, body = m.group(1), m.group(2), m.group(3)
        pm = re.search(r"(\w+)\s*=\s*\[[^\]]*\]\(const LegalityQuery\s*&\s*Query\)"
                       r"\s*\{(.*?)\};", body, re.S)
        rm = re.search(r"return\s+all\s*\(\s*typeInSet\s*\(\s*(\w+)\s*,\s*"
                       r"(\w+)\s*\)\s*,\s*(\w+)\s*\)", body, re.S)
        if not pm or not rm:
            continue
        names = [p.split()[-1].lstrip("&*") for p in split_top(params)]
        body = pm.group(2)
        body = re.sub(r"^\s*return\s+", "", body.strip())
        body = body.strip().rstrip(";")
        out[name] = {"params": names, "idx": rm.group(1), "set": rm.group(2),
                     "body": body}
        continue
    # A helper whose predicate is a whole lambda body: early returns, locals.
    # `local_pred` inlines it statement by statement, the same way it reads a
    # lambda written inline in the constructor.
    for m in re.finditer(r"static\s+LegalityPredicate\s+(\w+)\s*\((.*?)\)\s*\{"
                         r"(.*?)^\}", source, re.S | re.M):
        name, params, body = m.group(1), m.group(2), m.group(3)
        pm = re.search(r"return\s+\[[^\]]*\]\(const LegalityQuery\s*&\s*Query\)"
                       r"\s*\{(.*?)\};", body, re.S)
        if not pm:
            continue
        out[name] = {"params": [p.split()[-1].lstrip("&*")
                                for p in split_top(params)],
                     "body": pm.group(1), "stmt": True}
    # `static bool F(const T &ST) { return A || B; }`: a constructor-time gate
    # like `if (AEABI(ST))`.  Inlined as the disjunction it spells.
    for m in re.finditer(r"static\s+bool\s+(\w+)\s*\((.*?)\)\s*\{\s*"
                         r"return\s+(.+?);\s*\}", source, re.S):
        name, params, expr = m.group(1), m.group(2), m.group(3)
        out[name] = {"params": [p.split()[-1].lstrip("&*")
                                for p in split_top(params)],
                     "expr": " ".join(expr.split()), "bool": True}
    return out


def parse_aligns(source):
    """`auto F = [&ST](unsigned Size) { return Cond ? A : B; };`"""
    out = {}
    for m in re.finditer(r"auto\s+(\w+)\s*=\s*\[[^\]]*\]\s*\(([^)]*)\)\s*\{\s*"
                         r"return\s+(.+?)\?(.+?):(.+?);\s*\};", source, re.S):
        param = m.group(2).split()[-1].lstrip("&*") if m.group(2).strip() else ""
        out[m.group(1)] = (param, m.group(3).strip(), m.group(4).strip(),
                           m.group(5).strip())
    return out


class Refuse(Exception):
    pass


# ---------------------------------------------------------------------------
# Statements
# ---------------------------------------------------------------------------

class Chain:
    def __init__(self, name, opcodes, line):
        self.name = name          # ruleset name
        self.opcodes = opcodes
        self.rules = []           # [(line, text)]
        self.line = line


class Program:
    def __init__(self):
        self.rulesets = []        # [Chain]
        self.by_object = {}       # C++ chain variable -> Chain
        self.refused = []

    def refuse(self, line, why):
        self.refused.append((line, why))


INT_CONST = {}


def const_eval(text, env):
    """Evaluate the constant expressions the constructor uses for indices."""
    text = text.strip()
    if text in env:
        return env[text]
    if re.fullmatch(r"-?\d+", text):
        return int(text)
    if text in ("true", "false"):
        return text == "true"
    m = re.fullmatch(r"(.+?)\s*\?\s*(-?\d+)\s*:\s*(-?\d+)", text, re.S)
    if m:
        cond = const_eval(m.group(1), env)
        if not isinstance(cond, bool) and not isinstance(cond, int):
            raise Refuse(text)      # `XLen ? a : b` is not a compile-time
        return const_eval(m.group(2), env) if cond \
            else const_eval(m.group(3), env)
    m = re.fullmatch(r"(.+?)\s*==\s*(.+)", text, re.S)
    if m:
        a = const_eval(m.group(1), env)
        b = const_eval(m.group(2), env)
        if type(a) is not type(b):
            # `XLen == 64` is a *runtime* subtarget test.  Comparing the
            # spelling "XLen" with 64 would answer False and silently drop
            # the whole `if` body.
            raise Refuse(text)
        return a == b
    if text in env:
        return env[text]
    if re.fullmatch(r"G_\w+", text):
        return text          # an opcode name, not a value
    arith = re.sub(r"(?<=\d)[uUlL]+", "", text)
    if re.fullmatch(r"[\d\s()+*/%-]+", arith):
        try:
            return int(eval(arith, {"__builtins__": {}}, {}))
        except Exception:
            raise Refuse(text)
    raise Refuse(text)


def idx_of(text, env):
    """A slot index argument (always a small integer once constants fold)."""
    v = const_eval(text, env)
    if not isinstance(v, int) or isinstance(v, bool):
        raise Refuse("slot index %r" % text)
    return v


# A helper that hands back a fixed opcode list.  SPIRV builds one rule set per
# opcode in a loop, so the list has to be known here; `tests/test_import_cxx.py`
# reads it out of SPIRVUtils.cpp and fails if the two disagree.
OPCODE_LISTS = {
    "getTypeFoldingSupportedOpcodes": [
        "G_ADD", "G_FADD", "G_STRICT_FADD", "G_SUB", "G_FSUB", "G_STRICT_FSUB",
        "G_MUL", "G_FMUL", "G_STRICT_FMUL", "G_SDIV", "G_UDIV", "G_FDIV",
        "G_STRICT_FDIV", "G_SREM", "G_UREM", "G_FREM", "G_STRICT_FREM",
        "G_FNEG", "G_CONSTANT", "G_FCONSTANT", "G_AND", "G_OR", "G_XOR",
        "G_SHL", "G_ASHR", "G_LSHR", "G_SELECT", "G_EXTRACT_VECTOR_ELT",
    ],
}


def opcodes_of(text, env):
    """`{G_A, G_B}` / `G_A` / `Op` / `helper()` -> ["G_A", "G_B"]."""
    text = text.strip()
    if text.startswith("{") and text.endswith("}"):
        return [o.strip() for o in split_top(text[1:-1])]
    m = re.fullmatch(r"(\w+)\s*\(\s*\)", text)
    if m and m.group(1) in OPCODE_LISTS:
        return list(OPCODE_LISTS[m.group(1)])
    if text in env:
        return [env[text]]
    if re.fullmatch(r"G_\w+", text):
        return [text]
    raise Refuse(text)


STATS = {"calls": {}, "rules": {}}


def _product(lists):
    out = [[]]
    for xs in lists:
        out = [prev + [x] for prev in out for x in xs]
    return out


ACTIONS = {"legalFor": "legal", "customFor": "custom", "libcallFor": "libcall",
           "lowerFor": "lower", "unsupportedFor": "unsupported"}


def emit_chain_call(tr, p, chain, method, args, line, env=None):
    """One builder call -> zero or more `.gisel` rule lines."""
    args = split_top(args) if args.strip() else []
    env = env or {}
    out = []

    def type_preds(idx, text):
        """A `{t, t}` / `{{a,b}}` / named set -> [(gate, predicate)]."""
        text = text.strip()
        if text.startswith("{"):
            parts = split_top(text[1:-1])
            if parts and all(x.lstrip().startswith("{") for x in parts):
                rows = [split_top(r[1:-1]) for r in parts]
                arity = len(rows[0])
                if any(len(r) != arity for r in rows):
                    raise Refuse(text)
                if arity == 1:
                    # `{{S32}, {S64}}` is `initializer_list<LLT>`: C++ picks
                    # the one-type overload, so it constrains slot 0 only.
                    elems = [tr.t.name(r[0]) for r in rows]
                    return [(None, "in(slot(%d), [%s])"
                             % (idx, ", ".join(elems)))]
                triples = ["[" + ", ".join(tr.t.name(x) for x in r) + "]"
                           for r in rows]
                if arity == 2:
                    return [(None, "in_pairs(slot(0), slot(1), [%s])"
                             % ", ".join(triples))]
                if arity == 3:
                    return [(None, "in_triples(slot(0), slot(1), slot(2), [%s])"
                             % ", ".join(triples))]
                raise Refuse(text)
            elems = [tr.t.name(x) for x in parts]
            return [(None, "in(slot(%d), [%s])" % (idx, ", ".join(elems)))]
        m = split_ternary(text)
        if m:
            g = tr.pred(m[0])
            return ([(g, t) for _, t in type_preds(idx, m[1].strip())] +
                    [("not " + g, t) for _, t in type_preds(idx, m[2].strip())])
        if text in tr.t.sets:
            return [(None, "in(slot(%d), %s)" % (idx, PREFIX + text))]
        if text in tr.t.set_alias:
            return type_preds(idx, tr.t.set_alias[text])
        return [(None, "in(slot(%d), %s)" % (idx, tr.t.name(text)))]

    def expand_sets(ref):
        """A cartesian-product operand -> [(gate, [type name])]."""
        ref = ref.strip()
        m = split_ternary(ref)
        if m:
            g = tr.pred(m[0])
            return ([(g, xs) for _, xs in expand_sets(m[1].strip())] +
                    [("not " + g, xs) for _, xs in expand_sets(m[2].strip())])
        if ref.startswith("{"):
            return [(None, [tr.t.name(x) for x in split_top(ref[1:-1])])]
        if ref in tr.t.sets:
            return [(None, [tr.t.name(x) for x in tr.t.sets[ref]])]
        if ref in tr.t.set_alias:
            return expand_sets(tr.t.set_alias[ref])
        raise Refuse(ref)

    def mutation_variants(text):
        """A lane-count mutation whose count a configuration picks.

        `changeElementCountTo(i, ElementCount::getFixed(MaxVectorSize))` is one
        rule per value of the gate, so the caller emits one line per split.
        """
        m = re.fullmatch(r"(?:LegalizeMutations::)?changeElementCountTo"
                         r"\s*\(\s*(\d+)\s*,\s*ElementCount::getFixed"
                         r"\(\s*(.+?)\s*\)\s*\)", text.strip(), re.S)
        if m and not re.fullmatch(r"\d+", m.group(2).strip()):
            return [(gate, "slot(%s), %d" % (m.group(1), value))
                    for gate, value in tr.int_splits(m.group(2))]
        m = re.fullmatch(r"(?:LegalizeMutations::)?changeElementSizeTo"
                         r"\s*\(\s*(\d+)\s*,\s*(.+?)\s*\)",
                         text.strip(), re.S)
        if m and not re.fullmatch(r"\d+", m.group(2).strip()):
            idx = m.group(1)
            return [(gate, "slot(%s), replace(elt_size_to(slot(%s), %d))"
                     % (idx, idx, value))
                    for gate, value in tr.int_splits(m.group(2))]
        return None

    def mutation(text):
        """A `LegalizeMutations::` call or a lambda returning a pair."""
        text = text.strip()
        m = re.fullmatch(r"(?:LegalizeMutations::)?moreElementsToNextPow2"
                         r"\s*\(\s*(\d+)\s*\)", text)
        if m:
            return "slot(%s), pow2" % m.group(1)
        m = re.fullmatch(r"(?:LegalizeMutations::)?scalarize"
                         r"\s*\(\s*(.+?)\s*\)", text, re.S)
        if m:
            i = idx_of(m.group(1), env)
            return ("slot(%d), replace(elem_of(slot(%d)))" % (i, i))
        m = re.fullmatch(r"(?:LegalizeMutations::)?widenScalarOrEltToNextPow2"
                         r"\s*\((.*)\)", text, re.S)
        if m:
            args = split_top(m.group(1))
            i = idx_of(args[0], env)
            n = tr.int_expr(args[1]) if len(args) > 1 and args[1].strip() else "0"
            # The mutation itself has no pow2 guard: `widenScalarIf(P, M)`
            # applies it whenever P holds.  Spell the new size out so the
            # evaluator does not add `sizeNotPow2` on top of P.
            return ("slot(%d), replace(elt_size_to(slot(%d), "
                    "pow2_at_least(bits_of(scalar_of(slot(%d))), %s)))"
                    % (i, i, i, n))
        m = re.fullmatch(r"(?:LegalizeMutations::)?widenScalarOrEltToNextMultipleOf"
                         r"\s*\((.*)\)", text, re.S)
        if m:
            args = split_top(m.group(1))
            i = idx_of(args[0], env)
            return ("slot(%d), replace(elt_size_to(slot(%d), "
                    "align_to(bits_of(scalar_of(slot(%d))), %s)))"
                    % (i, i, i, tr.int_expr(args[1])))
        m = re.fullmatch(r"(?:LegalizeMutations::)?fewerElementsToNextPow2"
                         r"\s*\(\s*(\d+)\s*\)", text)
        if m:
            return "slot(%s), pow2" % m.group(1)
        m = re.fullmatch(r"(?:LegalizeMutations::)?changeTo"
                         r"\s*\(\s*(\d+)\s*,\s*(.+)\)", text, re.S)
        if m:
            return "slot(%s), replace(%s)" % (m.group(1), tr._mut_ty(m.group(2)))
        m = re.fullmatch(r"(?:LegalizeMutations::)?changeElementCountTo"
                         r"\s*\(\s*(\d+)\s*,\s*ElementCount::getFixed"
                         r"\(\s*(.+?)\s*\)\s*\)", text, re.S)
        if m and re.fullmatch(r"\d+", m.group(2).strip()):
            # FewerElements to a fixed lane count; the DSL's `fewer` takes the
            # count directly when it is a literal.
            return "slot(%s), %s" % (m.group(1), m.group(2).strip())
        m = re.fullmatch(r"(?:LegalizeMutations::)?changeElementSizeTo"
                         r"\s*\(\s*(.+?)\s*,\s*(.+)\)", text, re.S)
        if m:
            i, arg = idx_of(m.group(1), env), m.group(2).strip()
            if re.fullmatch(r"\d+", arg):
                src = "bits_of(slot(%s))" % arg
            else:
                src = "size_of(ty(%s))" % tr.t.name(arg)
            return "slot(%d), replace(elt_size_to(slot(%d), %s))" % (i, i, src)
        m = re.fullmatch(r"(?:LegalizeMutations::)?changeElementTo\s*\(\s*(\d+)"
                         r"\s*,\s*(.+)\)", text, re.S)
        if m:
            i, ty = m.group(1), tr.t.name(m.group(2))
            return "slot(%s), replace(elt_type_to(slot(%s), ty(%s)))" % (i, i, ty)
        m = re.fullmatch(r"(?:LegalizeMutations::)?narrowScalarTo\s*\(\s*(\d+)"
                         r"\s*,\s*(.+)\)", text, re.S)
        if m:
            return "slot(%s), ty(%s)" % (m.group(1), tr.t.name(m.group(2)))
        m = re.fullmatch(r"([A-Za-z_]\w*)\s*\((.*)\)", text, re.S)
        if m and "::" not in text:
            try:
                i = idx_of(m.group(2), env)
            except Refuse:
                i = None
            if isinstance(i, int):
                value = tr.mutation_helper_value(m.group(1), i, env)
                if value is not None:
                    return value
        if text.startswith("["):
            return tr.mutation_lambda(text, env)
        raise Refuse(text)

    # `legalFor(pred, {..})` / `minScalar(pred, 0, sXLen)`: the predicate is a
    # table-build-time gate, which is exactly what a `when`/`and` already means.
    BASE_ARGS = {"legalFor": 1, "customFor": 1, "libcallFor": 1, "lowerFor": 1,
                 "unsupportedFor": 1, "legalForTypesWithMemDesc": 1,
                 "minScalar": 2, "maxScalar": 2, "clampScalar": 3,
                 "minScalarOrElt": 2, "clampScalarOrElt": 3}
    pred_arg = None
    if method in BASE_ARGS and len(args) == BASE_ARGS[method] + 1:
        pred_arg, args = args[0], args[1:]
    if method.endswith("CartesianProduct") and args:
        head = args[0].lstrip()
        if not (head.startswith("{") or args[0] in tr.t.sets
                or args[0] in tr.t.set_alias or "?" in args[0]):
            pred_arg, args = args[0], args[1:]
    pg = tr.pred(pred_arg) if pred_arg is not None else None

    def act(op, pred, gate=None):
        gs = [x for x in (pg, gate) if x]
        if gs:
            pred = "(%s and %s)" % (" and ".join(gs), pred)
        return "%s %s" % (op, pred)

    def when(text, gate):
        return text if gate is None else "%s when %s" % (text, gate)

    if method in ACTIONS:
        for g, pred in type_preds(0, args[0]):
            out.append(act(ACTIONS[method], pred, g))
    elif method in ("legalForCartesianProduct", "customForCartesianProduct",
                    "lowerForCartesianProduct", "libcallForCartesianProduct"):
        op = {"legalForCartesianProduct": "legal",
              "customForCartesianProduct": "custom",
              "lowerForCartesianProduct": "lower",
              "libcallForCartesianProduct": "libcall"}[method]
        # A configuration-chosen set is one runtime value: when the same
        # alias appears in several argument positions, every position takes
        # the same branch.  Expanding each position independently invents
        # unreachable combinations (`isShader and not isShader`) with the wrong
        # operand sets, and the emitter cannot drop them reliably.
        refs = [a.strip() for a in args]
        unique = []
        for r in refs:
            if r not in unique:
                unique.append(r)
        expansions = {r: expand_sets(r) for r in unique}
        if len(args) <= 2:
            for combo in _product([expansions[r] for r in unique]):
                choice = dict(zip(unique, combo))
                gates = [g for g, _ in combo if g]
                a, b = (choice[refs[0]][1], choice[refs[1]][1]
                        if len(refs) > 1 else choice[refs[0]][1])
                rows = [[x, y] for x in a for y in b]
                pred = "in_pairs(slot(0), slot(1), [%s])" % ", ".join(
                    "[" + ", ".join(r) + "]" for r in rows)
                out.append(act(op, pred, " and ".join(gates) or None))
        else:
            for combo in _product([expansions[r] for r in unique]):
                choice = dict(zip(unique, combo))
                gates = [g for g, _ in combo if g]
                parts = ["in(slot(%d), [%s])" % (i, ", ".join(choice[r][1]))
                         for i, r in enumerate(refs)]
                out.append(act(op, " and ".join(parts),
                               " and ".join(gates) or None))
    elif method in ("legalForTypesWithMemDesc",):
        for g, pred in mem_desc_tables(tr, args[0]):
            out.append(act("legal", pred, g))
    elif method in ("legalIf", "customIf", "lowerIf", "unsupportedIf", "libcallIf"):
        out.append(act(method[:-2], tr.pred_lambda(args[0])))
    elif method in ("lower", "unsupported", "alwaysLegal", "libcall", "custom"):
        out.append({"lower": "lower true", "unsupported": "unsupported true",
                    "alwaysLegal": "legal true", "libcall": "libcall true",
                    "custom": "custom true"}[method])
    elif method == "scalarize":
        out.append("scalarize(slot(%s))" % (idx_of(args[0], env) if args else 0))
    elif method == "scalarizeIf":
        i = idx_of(args[1], env)
        out.append("scalarize(slot(%d)) when %s" % (i, tr.pred_lambda(args[0])))
    elif method == "clampScalar":
        idx = idx_of(args[0], env)
        out.append("widen(slot(%d), ty(%s))" % (idx, tr.t.name(args[1])))
        out.append("narrow(slot(%d), ty(%s))" % (idx, tr.t.name(args[2])))
    elif method == "clampScalarOrElt":
        idx = idx_of(args[0], env)
        out.append("widen(elem_of(slot(%d)), ty(%s))" % (idx, tr.t.name(args[1])))
        out.append("narrow(elem_of(slot(%d)), ty(%s))" % (idx, tr.t.name(args[2])))
    elif method in ("minScalar", "maxScalar"):
        idx = idx_of(args[0], env)
        ty = tr.t.name(args[1])
        out.append("widen(slot(%d), ty(%s))" % (idx, ty)
                   if method == "minScalar" else
                   "narrow(slot(%d), ty(%s))" % (idx, ty))
    elif method == "minScalarOrElt":
        idx = idx_of(args[0], env)
        out.append("widen(elem_of(slot(%d)), ty(%s))" % (idx, tr.t.name(args[1])))
    elif method == "minScalarOrEltIf":
        idx = idx_of(args[1], env)
        out.append("widen(elem_of(slot(%d)), ty(%s)) when %s"
                   % (idx, tr.t.name(args[2]), tr.pred_lambda(args[0])))
    elif method in ("minScalarSameAs", "maxScalarSameAs"):
        a, b = idx_of(args[0], env), idx_of(args[1], env)
        op = "widen" if method == "minScalarSameAs" else "narrow"
        out.append("%s(slot(%d), same_as(%d))" % (op, a, b))
    elif method in ("minScalarEltSameAsIf", "maxScalarEltSameAsIf"):
        a, b = idx_of(args[1], env), idx_of(args[2], env)
        g = tr.pred_lambda(args[0])
        # `same_as(k)` is a *scalar width* observation, and both
        # `coreMinScalarSameAs` and `coreMaxScalarSameAs` already compare
        # `getScalarSizeInBits()`.  `replace(same_as(k))` would instead make the
        # whole type of slot k the new type, which for a vector slot produces a
        # vector where a scalar is expected and trips mutationIsSane.
        op = "widen" if method == "minScalarEltSameAsIf" else "narrow"
        out.append("%s(slot(%d), same_as(%d)) when %s" % (op, a, b, g))
    elif method in ("minScalarIf", "maxScalarIf"):
        idx = idx_of(args[1], env)
        out.append("%s(slot(%d), ty(%s)) when %s"
                   % ("widen" if method == "minScalarIf" else "narrow",
                      idx, tr.t.name(args[2]), tr.pred_lambda(args[0])))
    elif method in ("widenScalarToNextPow2", "widenScalarOrEltToNextPow2",
                    "widenScalarOrEltToNextPow2OrMinSize"):
        idx = idx_of(args[0], env)
        if method == "widenScalarOrEltToNextPow2OrMinSize" and len(args) > 1:
            # C++: `ssz < Min || !isPow2(ssz)`, which is exactly the int form.
            out.append("widen(slot(%d), %s)" % (idx, tr.int_expr(args[1])))
        else:
            # `widenScalarToNextPow2` guards on `sizeNotPow2` (scalar only),
            # while both `...OrElt...` forms guard on `scalarOrEltSizeNotPow2`;
            # collapsing them onto the scalar spelling silently drops vector
            # rules.  With MinSize == 0 the two `...OrElt...` forms have the
            # same predicate, so the element form spells both.
            on = ("elem_of(slot(%d))" % idx if "OrElt" in method
                  else "slot(%d)" % idx)
            if len(args) > 1:
                out.append("widen(%s, pow2_min(%s))"
                           % (on, tr.int_expr(args[1])))
            else:
                out.append("widen(%s, pow2)" % on)
    elif method == "widenScalarToNextMultipleOf":
        # `sizeNotMultipleOf` is scalar-only and the mutation calls
        # `changeElementSize(alignTo(ssz, N))` (LegalizerInfo.h:989) -- exactly
        # the `multiple_of` target form, which already has a `core` shim.
        out.append("widen(slot(%s), multiple_of(%s))"
                   % (idx_of(args[0], env), tr.int_expr(args[1])))
    elif method == "widenVectorEltsToVectorMinSize":
        idx = idx_of(args[0], env)
        out.append("widen(elem_of(slot(%d)), min_vector_size(%s))"
                   % (idx, tr.int_expr(args[1])))
    elif method == "widenScalarFor":
        for g, pred in type_preds(0, args[0]):
            out.append("widen(%s) when %s" % (mutation(args[1]), pred))
    elif method == "scalarSameSizeAs":
        a, b = idx_of(args[0], env), idx_of(args[1], env)
        out.append("widen(slot(%d), same_as(%d))" % (a, b))
        out.append("narrow(slot(%d), same_as(%d))" % (a, b))
    elif method in ("moreElementsToNextPow2", "fewerElementsToNextPow2"):
        idx = idx_of(args[0], env)
        out.append("%s(slot(%d), pow2)"
                   % ("more" if method.startswith("more") else "fewer", idx))
    elif method in ("moreElementsIf", "fewerElementsIf"):
        op = "more" if method.startswith("more") else "fewer"
        if len(args) == 1:
            out.append("%s(%s)" % (op, mutation(args[0])))
        else:
            pred_ = tr.pred_lambda(args[0])
            variants = mutation_variants(args[1])
            if variants is None:
                out.append("%s(%s) when %s" % (op, mutation(args[1]), pred_))
            else:
                # The count is decided by the same configuration the predicate
                # reads, so each value gets its own rule with that gate.
                for gate, target in variants:
                    out.append("%s(%s) when (%s and %s)"
                               % (op, target, pred_, gate))
    elif method == "clampMinNumElements":
        out.append("clamp_min(%s, %s, %s)" % (idx_of(args[0], env),
                                              tr.t.name(args[1]),
                                              tr.int_expr(args[2])))
    elif method == "clampMaxNumElements":
        out.append("clamp_max(%s, %s, %s)" % (idx_of(args[0], env),
                                              tr.t.name(args[1]),
                                              tr.int_expr(args[2])))
    elif method == "clampMaxNumElementsStrict":
        # LegalizerInfo.h:1327 spells this as alignNumElementsTo followed by
        # clampMaxNumElements, so the corpus keeps both rules.  Folding them
        # into one statement would hide a mutation the evaluator must apply.
        idx = idx_of(args[0], env)
        elt, lanes = tr.t.name(args[1]), tr.int_expr(args[2])
        out.append("align_lanes(%s, %s, %s)" % (idx, elt, lanes))
        out.append("clamp_max(%s, %s, %s)" % (idx, elt, lanes))
    elif method == "clampNumElements":
        idx = idx_of(args[0], env)
        elt = tr.t.elem_of(args[1])
        out.append("clamp_min(%d, %s, num_elems(ty(%s)))"
                   % (idx, elt, tr.t.name(args[1])))
        out.append("clamp_max(%d, %s, num_elems(ty(%s)))"
                   % (idx, elt, tr.t.name(args[2])))
    elif method in ("widenScalarIf", "narrowScalarIf", "bitcastIf"):
        if len(args) != 2:
            raise Refuse("%s with %d args" % (method, len(args)))
        pred, mut = args[0], args[1]
        op = {"widenScalarIf": "widen", "narrowScalarIf": "narrow",
              "bitcastIf": "bitcast"}[method]
        if op == "bitcast" and re.fullmatch(
                r"(?:LegalizeMutations::)?changeTo\s*\(.*\)", mut.strip(), re.S):
            # `bitcastIf(P, changeTo(i, T))` names the target type directly;
            # `mutation_lambda` reads the pair/lambda spellings only.
            to = tr.mutation(mut)
        else:
            to = mutation(mut)
        out.append("%s(%s) when %s" % (op, to, tr.pred_lambda(pred)))
    elif method == "immIdx":
        out.append("imm_idx(%s)" % idx_of(args[0], env))
    elif method == "unsupportedIfMemSizeNotPow2":
        out.append("unsupported not is_pow2(size_in_bytes_of("
                   "mem_ty(mem_slot(0))))")
    elif method == "narrowScalarFor":
        pairs = split_top(args[0].strip()[1:-1])
        rows = [split_top(r[1:-1]) for r in pairs]
        to = mutation(args[1])
        out.append("narrow(%s) when in_pairs(slot(0), slot(1), [%s])"
                   % (to, ", ".join("[" + ", ".join(tr.t.name(x) for x in r) + "]"
                                    for r in rows)))
    elif method in ("lowerIfMemSizeNotByteSizePow2", "lowerIfMemSizeNotPow2"):
        mt = "mem_ty(mem_slot(0))"
        if method == "lowerIfMemSizeNotByteSizePow2":
            pred = ("(not is_byte_sized(%s) or not is_pow2(size_in_bytes_of(%s)))"
                    % (mt, mt))
        else:
            pred = "not is_pow2(size_in_bytes_of(%s))" % mt
        out.append("lower %s" % pred)
    elif method == "bitcast":
        out.append("bitcast(slot(0), %s)" % type_preds(0, args[0])[0][1][len("in(slot(0), "):-1])
    else:
        raise Refuse("%s(...)" % method)
    if pred_arg is not None and method in (
            "minScalar", "maxScalar", "clampScalar", "widenScalarToNextPow2",
            "widenScalarOrEltToNextPow2", "widenScalarOrEltToNextPow2OrMinSize",
            "scalarSameSizeAs", "minScalarOrElt", "clampScalarOrElt"):
        g = tr.pred(pred_arg)
        out = [l if " when " in l else l + " when " + g for l in out]
    STATS["calls"][method] = STATS["calls"].get(method, 0) + 1
    STATS["rules"][method] = STATS["rules"].get(method, 0) + len(out)
    return out


def mem_desc_tables(tr, text):
    """`{{t, p, mt, align}, ...}` -> [(gate|None, `in_mem_desc(...)`), ...].

    `getScalarMemAlign(n)` is `ST.enableUnalignedScalarMem() ? 8 : n`, so a row
    that uses it forces the whole table to be written once per variant, with
    mutually exclusive `when` gates -- exactly what the C++ does.
    """
    rows = [split_top(r[1:-1]) for r in split_top(text.strip()[1:-1])]
    parsed = []
    for parts in rows:
        if len(parts) != 4:
            raise Refuse(", ".join(parts))
        t0, t1, mt, al = [x.strip() for x in parts]
        if re.fullmatch(r"\d+", al):
            variants = [(None, int(al))]
        else:
            try:
                variants = tr.int_splits(al)
            except Refuse:
                variants = tr.align_variants(al)
        parsed.append((tr.t.name(t0), tr.t.name(t1), tr.t.name(mt), variants))

    n = max(len(p[3]) for p in parsed)
    if n == 1:
        return [(None, "in_mem_desc(slot(0), slot(1), mem_slot(0), [%s])" %
                 ", ".join("mem_desc(%s, %s, %s, %s)" % (a, b, c, v[0][1])
                           for a, b, c, v in parsed))]

    # The most specific row defines the table split; the others are the same
    # value under every variant (or pick by the first gate atom).
    template = max(parsed, key=lambda p: len(p[3]))[3]

    def atom(gate):
        return None if gate is None else gate.split(" and ")[0]

    tables = []
    for k, (gate, _val) in enumerate(template):
        entries, gates = [], []
        for a, b, c, variants in parsed:
            if len(variants) == 1:
                val = variants[0][1]
            else:
                val = variants[k][1] if len(variants) == len(template) else next(
                    v for g, v in variants if atom(g) == atom(gate))
            entries.append("mem_desc(%s, %s, %s, %s)" % (a, b, c, val))
            if gate:
                gates.append(gate)
        gates = list(dict.fromkeys(gates))
        tables.append((" and ".join(gates) or None,
                       "in_mem_desc(slot(0), slot(1), mem_slot(0), [%s])" %
                       ", ".join(entries)))
    return tables


def match_paren(text, i):
    """Index just past the `)` that closes the `(` at i."""
    depth = 0
    while i < len(text):
        if text[i] == "(":
            depth += 1
        elif text[i] == ")":
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    raise Refuse("unbalanced parentheses")


def match_brace(text, i):
    """Index just past the `}` that closes the `{` at i."""
    depth = 0
    while i < len(text):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    raise Refuse("unbalanced braces")


def consume_one(text, i):
    """Consume one statement (simple or braced) and return the next index."""
    while i < len(text) and text[i] in " \t\n\r":
        i += 1
    m = re.match(r"(if|for|while)\b", text[i:])
    if m:
        j = text.index("(", i)
        j = match_paren(text, j)
        j = consume_one(text, j)
        m2 = re.match(r"\s*else\b", text[j:])
        if m2:
            j += m2.end()
            j = consume_one(text, j)
        return j
    if i < len(text) and text[i] == "{":
        depth = 0
        while i < len(text):
            if text[i] == "{":
                depth += 1
            elif text[i] == "}":
                depth -= 1
                if depth == 0:
                    return i + 1
            i += 1
        raise Refuse("unbalanced braces")
    depth = 0
    while i < len(text):
        c = text[i]
        if c in "([{":
            depth += 1
        elif c in ")]}":
            depth -= 1
        elif c == ";" and depth == 0:
            return i + 1
        i += 1
    return i


def split_statements(text):
    """Top-level statements and their line offset from ``text``'s first line."""
    out, i, n = [], 0, len(text)
    while i < n:
        while i < n and text[i] in " \t\n\r":
            i += 1
        if i >= n:
            break
        start = i
        m = re.match(r"(if|for|while)\b", text[i:])
        if m:
            j = text.index("(", i)
            j = match_paren(text, j)
            j = consume_one(text, j)
            m2 = re.match(r"\s*else\b", text[j:])
            if m2:
                j += m2.end()
                j = consume_one(text, j)
            out.append((text[start:j].strip(), text[:start].count("\n")))
            i = j
            continue
        if text[i] == "{":
            j = consume_one(text, i)
            out.append((text[start:j].strip(), text[:start].count("\n")))
            i = j
            continue
        j = consume_one(text, i)
        out.append((text[start:j].strip(), text[:start].count("\n")))
        i = j
    return out


TERMINALS = ("legal", "custom", "lower", "unsupported", "libcall")

# Calls the `.gisel` predicate/value grammar knows.  `query_body` and `pred`
# rewrite C++ observations into these; anything else is a host helper the
# corpus cannot evaluate, and must be refused rather than copied through (a
# copied name parses to nothing and makes the whole corpus invalid).
DSL_CALLS = {
    "slot", "elem_of", "scalar_of", "sort_of", "num_elems", "bits_of",
    "size_of", "size_in_bytes_of", "addr_space", "align_of", "mem_ty",
    "mem_slot", "mem_ordering", "mem_failure_ordering", "is_pow2",
    "is_float16", "is_bfloat16", "is_byte_sized", "imm", "ty", "int_of_size",
    "urem", "udiv", "mul", "select", "replace", "scalar_or_vec_of", "vec_of",
    "elt_type_to", "elt_size_to", "div_lanes", "pow2_ceil", "same_as", "pow2",
    "pow2_min", "multiple_of", "clamp", "clamp_min", "clamp_max",
    "min_vector_size", "in", "in_pairs", "in_triples", "is_fixed_vector",
}


def apply_gate(line, gate):
    if not gate:
        return line
    # A constructor-level `if` whose condition folded to a literal is C++ that
    # does or does not run at all: `if (false)` registers no builder upstream,
    # and `not not true` is just `true`.
    while "not not " in gate:
        gate = gate.replace("not not ", "")
    if gate == "not true":
        return None
    head, _, rest = line.partition(" ")
    if head in TERMINALS:
        rest = rest.strip()
        return "%s %s" % (head, gate if rest == "true" else "(%s and %s)" % (gate, rest))
    if " when " in line:
        body, _, when = line.partition(" when ")
        return "%s when (%s and %s)" % (body, gate, when)
    if line.startswith("scalarize(") or line.startswith("imm_idx("):
        return line          # both are markers, not rules with predicates
    return line + " when " + gate


# A constructor statement that fills a data table read by custom legalisation
# rather than adding a rule.  `setFCmpLibcallsAEABI()` / `setFCmpLibcallsGNU()`
# only populate `FCmp32/64Libcalls`, which `legalizeCustom()` consumes when it
# folds a `G_FCMP` into libcalls; the rule form has nothing to say about them.
# Skipping such a call is not a transcription gap, so it is counted and named
# in the corpus header instead of being reported as a refusal.
DATA_TABLE_INITIALISERS = {"setFCmpLibcallsAEABI", "setFCmpLibcallsGNU"}


class Importer:
    def __init__(self, target, source="", prefix=None):
        global PREFIX, CURRENT_TARGET
        PREFIX = (prefix or target[0].upper()) + "_"
        CURRENT_TARGET = target
        self.p = Program()
        self.t = Types()
        self.t.int_ternary_text = {}
        self.tr = Translator(self.t)
        self.tr.source = source
        self.t.cond_fn = self.tr.pred
        self.t.feat_name = self.tr.feature
        self.tr.helpers = parse_helpers(source)
        self.tr.bool_helpers = {k: v for k, v in self.tr.helpers.items()
                                if v.get("bool")}
        self.tr.aligns = parse_aligns(source)
        self.named_preds = {}
        self.param_preds = {}
        self.local_lambdas = {}
        self.tr.named_preds = self.named_preds
        self.tr.param_preds = self.param_preds
        self.tr.local_lambdas = self.local_lambdas
        self.rulesets = []
        self.gate = []
        self.env = {}
        self.tr.env = self.env
        self.skipped = []   # statements that fill data tables, not rules
        self.ignored = []   # constructor plumbing, not rules

    # -- statements ------------------------------------------------------
    def block(self, text, line0=0):
        for st, offset in split_statements(text):
            line = line0 + offset
            try:
                self.statement(st, line)
            except Refuse as e:
                self.p.refuse(line, "%s: %s" % (st.splitlines()[0][:60], e))
            except Exception as e:
                # An importer bug must not abort the run: report the line and
                # keep going.  Every refusal already makes the exit code 1, so
                # a partial transcription can never be mistaken for a complete
                # one; the point is that a bug names the statement it choked on
                # instead of printing a traceback with no source location.
                self.p.refuse(line, "%s: internal error %s: %s"
                              % (st.splitlines()[0][:60], type(e).__name__,
                                 e))

    def statement(self, st, line):
        st = st.strip()
        if st.endswith(";"):
            st = st[:-1].rstrip()
        if not st:
            return
        if st.startswith("if "):
            return self.if_stmt(st, line)
        if st.startswith("for "):
            return self.for_stmt(st, line)
        if st.startswith("switch "):
            return self.switch_stmt(st, line)
        if st in ("break", "continue"):
            return
        if st.startswith("{"):
            return self.block(st[1:-1], line)
        if re.match(r"(const\s+)?LLT\s+\w+\s*=", st):
            m = re.match(r"(?:const\s+)?LLT\s+(\w+)\s*=\s*(.+)$", st, re.S)
            return self.t.add(m.group(1), m.group(2), line)
        if re.match(r"(const\s+)?bool\s+\w+\s*=", st):
            m = re.match(r"(?:const\s+)?bool\s+(\w+)\s*=\s*(.+)$", st, re.S)
            name, expr = m.group(1), " ".join(m.group(2).split())
            try:
                value = const_eval(expr, self.env)
            except Refuse:
                value = None
            if isinstance(value, bool):
                # `bool NoAlignRequirements = true;` is a named constant, not a
                # subtarget observation; folding it here keeps a literal out of
                # the feature table (and out of the evaluator's key space).
                self.env[name] = value
                return
            # A compound constant may name an earlier one; the emitted
            # constructor only has the `.gisel` names, so rewrite the
            # reference to the name the feature block declares.
            for alias, emitted in list(self.tr.feature_aliases.items()):
                expr = re.sub(r"\b%s\b" % re.escape(alias), emitted, expr)
            key = _norm(expr).replace("( ", "(")
            suffix = FEATURE_NAMES.get(CURRENT_TARGET, {}).get(key)
            if suffix:
                USED_RENAMES["feature"].add(key)
            self.tr.features[expr] = PREFIX + (suffix or name)
            self.tr.feature_aliases[name] = self.tr.features[expr]
            return
        if re.match(r"const\s+auto\s+\w+\s*=\s*\[", st):
            # A bool helper declared *inside* the constructor: the generated
            # `.inc` replaces the whole constructor body, so the name is not
            # in scope there.  Remember the body; each call site inlines it
            # into `opaque(...)`.
            m = re.match(r"const\s+auto\s+(\w+)\s*=\s*(.+)$", st, re.S)
            pm = re.match(r"\[.*?\]\s*\((.*?)\)\s*(?:->\s*\w+)?\s*\{(.*)\}$",
                          m.group(2), re.S)
            if not pm:
                raise Refuse("local lambda")
            params = [x.strip() for x in split_top(pm.group(1))]
            self.local_lambdas[m.group(1)] = (params, pm.group(2))
            return
        if re.match(r"auto\s+\w+\s*=\s*\[", st):
            if "LegalityQuery" in st:
                m = re.match(r"auto\s+(\w+)\s*=\s*(.+)$", st, re.S)
                pm = re.match(
                    r"\[.*?\]\s*\(\s*const\s+LegalityQuery\s*&?\s*\w+"
                    r"\s*,\s*(?:unsigned|int)\s+(\w+)\s*\)\s*\{(.*)\}$",
                    m.group(2), re.S)
                if pm:
                    # `auto F = [=](const LegalityQuery &Q, unsigned i)`: the
                    # second argument names a slot, so it is a helper to
                    # inline per call, not a fixed predicate.
                    self.param_preds[m.group(1)] = (pm.group(1), pm.group(2))
                else:
                    self.named_preds[m.group(1)] = self.tr.pred_lambda(
                        m.group(2))
            return          # otherwise an align helper, parsed from the source
        if re.match(r"auto\s+\w+\s*=\s*\{", st):
            m = re.match(r"auto\s+(\w+)\s*=\s*\{(.*)\}$", st, re.S)
            self.t.sets[m.group(1)] = [x.strip() for x in split_top(m.group(2))]
            return
        m = re.match(r"(?:const\s+)?std::initializer_list<LLT>\s+(\w+)\s*=\s*"
                     r"\{(.*)\}$", st, re.S)
        if m:
            self.t.sets[m.group(1)] = [x.strip() for x in split_top(m.group(2))]
            return
        m = re.match(r"SmallVector<LLT,\s*\d+>\s+(\w+)\s*\(\s*(\w+)\s*\)$", st)
        if m:
            src = self.t.sets.get(m.group(2))
            if src is None:
                raise Refuse("unknown type list %r" % m.group(2))
            self.t.sets[m.group(1)] = list(src)
            return
        if re.match(r"const\s+TargetMachine\s*&", st):
            return          # `TM` only appears inside C++ predicates
        if st == "return":
            return          # `if (...) return;` in a helper
        m = re.match(r"(?:auto|LegalityPredicate)\s+(\w+)\s*=\s*(\[.*\]\s*\(.*)$",
                     st, re.S)
        if m:
            self.named_preds[m.group(1)] = self.tr.pred_lambda(m.group(2))
            return
        m = re.match(r"(?:auto|LegalityPredicate)\s+(\w+)\s*=\s*(all\s*\(.*)$",
                     st, re.S)
        if m:
            self.named_preds[m.group(1)] = self.tr.pred(m.group(2))
            return
        if re.match(r"(?:auto|LegalizeRuleSet)\s*&\s*\w+\s*=", st):
            m = re.match(r"(?:auto|LegalizeRuleSet)\s*&\s*(\w+)\s*=\s*(.+)$",
                         st, re.S)
            if not m.group(2).lstrip().startswith("getActionDefinitionsBuilder"):
                # `auto &allowedVectorTypes = isShader ? shader : all;` names
                # a set, not a rule chain.
                self.t.set_alias[m.group(1)] = " ".join(m.group(2).split())
                return
            return self.chain(m.group(1), m.group(2), line)
        if st.startswith("getActionDefinitionsBuilder"):
            return self.chain(None, st, line)
        m = re.match(r"(\w+)\s*\.", st)
        if m and m.group(1) in self.p.by_object:
            return self.extend(self.p.by_object[m.group(1)], st[m.end(1):], line)
        if re.match(r"(?:const\s+)?(?:unsigned|uint32_t)\s+\w+\s*=", st):
            m = re.match(r"(?:const\s+)?(?:unsigned|uint32_t)\s+(\w+)\s*=\s*(.+)$",
                         st, re.S)
            name, expr = m.group(1), m.group(2)
            try:
                value = const_eval(expr, self.env)
            except Refuse:
                value = None
            if isinstance(value, int) and not isinstance(value, bool):
                self.env[name] = value
                return
            # `MaxVectorSize = ST.isShader() ? 4 : 16` and
            # `PSize = ST.getPointerSize()` are two numbers chosen by the
            # configuration.  A rule cannot spell that as one integer, so the
            # importer remembers the pair and splits the rule it appears in;
            # `int_splits()` does the split.
            tern = self._int_ternary(expr)
            if tern is not None:
                self.tr.int_ternary[name] = tern[:3]
                self.t.int_ternary_text[name] = tern[3], tern[1], tern[2]
                return
            raise Refuse(expr)
        if re.match(r"LegalityPredicate\s+\w+\s*=", st):
            return self.predicate_var(st, line)
        if re.match(r"verify\s*\(", st) or st.startswith("using namespace "):
            return
        if re.match(r"assert\s*\(", st):
            # `assert(ST.hasMad64_32());` states a contract the C++ build
            # enforces; it accepts and rejects no query, so it is plumbing.
            self.ignored.append((line, st))
            return
        if (re.match(r"this->\w+\s*=", st) or re.match(r"(?:GR|TM)\s*=", st)
                or st.startswith("LLVM_DEBUG(")):
            # Constructor plumbing (a member, the global registry, a debug
            # print), not a rule and not an observation.
            self.ignored.append((line, st))
            return
        m = re.match(r"(\w+)\s*\(\s*\)$", st)
        if m and m.group(1) in DATA_TABLE_INITIALISERS:
            self.skipped.append((line, st))
            return
        m = re.match(r"(\w+)\s*=\s*(.+)$", st, re.S)
        if m and m.group(1) in self.named_preds:
            return self.predicate_var("LegalityPredicate " + st, line)
        raise Refuse("unrecognised statement")

    def _int_ternary(self, expr):
        """`cond ? a : b` (or `ST.getPointerSize()`) -> a gated int pair."""
        expr = " ".join(expr.split())
        if expr == "ST.getPointerSize()":
            # The triple decides the width; the values are the only two LLVM
            # pointers have.  The gate is the feature the feature table holds.
            return (self.tr.feature("ST.getPointerSize() == 64"), 64, 32,
                    "ST.getPointerSize() == 64")
        m = split_ternary(expr)
        if not m:
            return None
        try:
            a = const_eval(m[1], self.env)
            b = const_eval(m[2], self.env)
        except Refuse:
            return None
        if not (isinstance(a, int) and isinstance(b, int)):
            return None
        return (self.tr.pred(m[0]), a, b, " ".join(m[0].split()))

    def predicate_var(self, st, line):
        m = re.match(r"LegalityPredicate\s+(\w+)\s*=\s*(.+)$", st, re.S)
        name, rhs = m.group(1), m.group(2).strip()
        if name in self.named_preds:
            cond = self.gate[-1] if self.gate else "true"
            self.named_preds[name] = "((%s and %s) or (not %s and %s))" % (
                cond, self._pred_text(rhs, line), cond, self.named_preds[name])
            return
        self.named_preds[name] = self._pred_text(rhs, line)

    def _pred_text(self, rhs, line):
        rhs = rhs.strip().rstrip(";")
        m = re.match(r"\[.*?\]\s*\((.*?)\)\s*\{(.*)\}$", rhs, re.S)
        if not m:
            # A plain predicate expression: `all(sizeIs(0, 64), immIs(0, 32))`.
            return self.tr.pred(rhs)
        body = m.group(2)
        aliases = dict(re.findall(
            r"(?:LLT\s+|,\s*)(\w+)\s*=\s*(Query\.Types\[[^\]]+\])", body))
        for var, val in aliases.items():
            body = re.sub(r"\b%s\b" % var, val, body)
        rm = re.search(r"return\s+(.+?);", body, re.S)
        if not rm:
            raise Refuse("predicate lambda")
        return self.tr.query_body(rm.group(1))

    def if_stmt(self, st, line):
        open_ = st.index("(")
        close = match_paren(st, open_) - 1
        cond, rest = st[open_ + 1:close], st[close + 1:]
        then, els = self._split_else(rest)
        try:
            v = const_eval(cond, self.env)
        except Refuse:
            v = None
        if isinstance(v, bool):
            body = then if v else els
            if body:
                self.block(body, line)
            return
        try:
            gate = self.tr.pred(cond)
        except Refuse:
            self.p.refuse(line, "if (%s)" % cond)
            return
        self.gate.append(gate)
        self.block(then, line)
        self.gate.pop()
        if els is not None:
            self.gate.append("not " + gate)
            self.block(els, line)
            self.gate.pop()

    @staticmethod
    def _split_else(rest):
        rest = rest.strip()
        if not rest.startswith("{"):
            # Brace-less `if (c) A; else B;` -- split at the first top-level else.
            depth, k = 0, 0
            while k < len(rest):
                c = rest[k]
                if c in "([{":
                    depth += 1
                elif c in ")]}":
                    depth -= 1
                elif depth == 0 and rest.startswith("else", k) and (
                        k == 0 or not (rest[k-1].isalnum() or rest[k-1] == "_")) and (
                        k + 4 == len(rest) or not (rest[k+4].isalnum() or rest[k+4] == "_")):
                    return Importer._strip_semicolon(rest[:k]), rest[k+4:].strip()
                k += 1
        if rest.startswith("{"):
            depth, i = 0, 0
            while i < len(rest):
                if rest[i] == "{":
                    depth += 1
                elif rest[i] == "}":
                    depth -= 1
                    if depth == 0:
                        break
                i += 1
            then, tail = rest[1:i], rest[i + 1:].strip()
        else:
            then, tail = rest, ""
        if tail.startswith("else"):
            tail = tail[4:].strip()
            if tail.startswith("{"):
                return then, tail[1:-1]
            # `else <one statement>`: take exactly that statement, not the rest
            # of the constructor.
            end = consume_one(tail, 0)
            return then, tail[:end].strip().rstrip(";")
        return then, None

    @staticmethod
    def _strip_semicolon(text):
        text = text.strip()
        return text[:-1] if text.endswith(";") else text

    def switch_stmt(self, st, line):
        """`switch (Opc) { case G_A: ... break; default: ... }` -> one branch.

        The selector is a constructor variable (a loop over an opcode list), so
        which branch runs is decided at transcription time, not by the DSL.
        Only the branch that runs is transcribed; the others are C++ control
        flow, not rules.
        """
        i = st.index("(")
        close = match_paren(st, i) - 1
        sel = " ".join(st[i + 1:close].split())
        body = st[close + 1:].strip()
        if not body.startswith("{") or not body.endswith("}"):
            raise Refuse("switch body")
        inner = body[1:-1]
        value = self.env.get(sel)
        if value is None:
            ops = opcodes_of(sel, self.env)
            value = ops[0] if len(ops) == 1 else None
        if value is None:
            raise Refuse("switch selector %r" % sel)
        chosen = None
        labels, buf, pos = [], [], 0
        for m in re.finditer(r"\b(case\s+[^:]+:|default\s*:)", inner):
            labels.append((m.group(1), inner[pos:m.start()]))
            pos = m.end()
        labels.append((None, inner[pos:]))
        # `labels` alternates label text and the text before it; rebuild the
        # (label, body) pairs by walking the matches in order.
        sections, pending, cur = [], [], None
        for m in re.finditer(r"\b(case\s+([^:]+):|default\s*:)", inner):
            if cur is not None:
                sections.append((pending, inner[cur:m.start()]))
                pending = []
            label = m.group(2)
            if label is not None and "::" in label:
                label = label.rsplit("::", 1)[1].strip()
            pending.append(label.strip() if label else "*")
            cur = m.end()
        if cur is not None:
            sections.append((pending, inner[cur:]))
        for names, chunk in sections:
            if "*" in names or value in names:
                chosen = chunk
                break
        if chosen is None:
            return
        self.block(chosen, line)

    def for_stmt(self, st, line):
        open_ = st.index("(")
        close = match_paren(st, open_) - 1
        head, body = st[open_ + 1:close], st[close + 1:].strip()
        m = re.fullmatch(r"(?:unsigned|auto)\s+(\w+)\s*:\s*(.+)", head, re.S)
        if not m or not body.startswith("{"):
            raise Refuse("for (unrecognised)")
        var, items, body = m.group(1), m.group(2), body[1:-1]
        for op in opcodes_of(items, self.env):
            self.env[var] = op
            self.block(body, line)
        self.env.pop(var, None)

    # -- chains ----------------------------------------------------------
    def chain(self, obj, text, line):
        m = re.match(r"getActionDefinitionsBuilder\s*\((.*?)\)\s*(.*)$", text, re.S)
        head = split_top(m.group(1))[0]
        ops = opcodes_of(head, self.env)
        if len(ops) > 1 and "Query.Opcode" in text:
            # A chain that branches on `Query.Opcode` loses that branch when
            # one rule set covers several opcodes; split it per opcode.
            for op in ops:
                self.tr.opcode = op
                try:
                    self.chain_one(obj, text, line, [op])
                finally:
                    self.tr.opcode = None
            return
        self.chain_one(obj, text, line, ops)

    def chain_one(self, obj, text, line, ops):
        m = re.match(r"getActionDefinitionsBuilder\s*\((.*?)\)\s*(.*)$", text, re.S)
        name = PREFIX + ops[0]
        # Repeated builders for one opcode are separate rule sets in C++ (both
        # apply, in order).  Keep both, but give the def a unique name.
        taken = {r.name for r in self.rulesets}
        if name in taken:
            k = 2
            while "%s_%d" % (name, k) in taken:
                k += 1
            name = "%s_%d" % (name, k)
        rs = Chain(name, ops, line)
        self.rulesets.append(rs)
        if obj:
            self.p.by_object[obj] = rs
        tail = m.group(2).strip().lstrip(". ")
        if tail:
            self.extend(rs, "." + tail, line, bare=True)

    def extend(self, rs, text, line, bare=False):
        text = text.strip()
        if not text.startswith("."):
            text = "." + text
        i = 0
        while i < len(text):
            text_slice = text[i:]
            m = re.match(r"\s*\.\s*([A-Za-z_]\w*)\s*\(", text_slice)
            if not m:
                break
            got = take_call(text_slice, m.start() + len(m.group(0)) - len(m.group(1)) - 1)
            if not got:
                break
            _, args, end = got
            method = m.group(1)
            try:
                rules = emit_chain_call(self.tr, self.p, rs, method, args, line,
                                        self.env)
            except Refuse as e:
                self.p.refuse(line, "%s.%s: %s" % (rs.name, method, e))
                rules = []
            gate = " and ".join(self.gate) if self.gate else ""
            if len(self.gate) > 1:
                gate = "(" + " and ".join(self.gate) + ")"
            for r in rules:
                gated = apply_gate(r, gate)
                if gated is not None:
                    rs.rules.append((line, gated))
            i += end
        rest = text[i:].strip()
        if rest not in ("", ";"):
            self.p.refuse(line, "%s: trailing %r" % (rs.name, rest[:40]))


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------

HEADER = """//===- {name}.gisel - imported from {src} -*- gisel -*-===//
//
// One-shot transcription: `tools/import-cxx.py {target} {src}`.
// Source sha256 {sha}.  Do not edit by hand; re-run the importer instead.
//
// The importer refuses what it cannot translate; every refusal below is a
// statement that still has to be written by hand (or taught to the importer).
//
{notes}//===----------------------------------------------------------------------===//
"""


def member_types(path):
    """`sXLen(LLT::scalar(XLen))` from the constructor's init list."""
    text = "\n".join(strip_comments(open(path).read()))
    m = re.search(r"::\w+LegalizerInfo\s*\([^)]*\)\s*:\s*([^{]*)\{", text)
    if not m:
        return []
    out = []
    for name, rhs in re.findall(r"(\w+)\s*\(([^()]*(?:\([^()]*\))?[^()]*)\)", m.group(1)):
        if rhs.strip().startswith("LLT::"):
            out.append((name, rhs.strip()))
    return out


def file_consts(path):
    """File-scope `LLT` constants and type lists the constructor reads.

    AMDGPU declares `constexpr LLT S96 = LLT::scalar(96);` and
    `constexpr std::initializer_list<LLT> AllS32Vectors = {...};` above the
    constructor.  They are not class members and not constructor locals, so
    without this scan a rule that names one emits a dangling reference
    (`legal in(slot(0), AllS32Vectors)` with no set of that name) instead of
    a refusal.
    """
    text = "\n".join(strip_comments(open(path).read()))
    types = [(m.group(1), m.group(2).strip())
             for m in re.finditer(
                 r"^(?:static\s+)?(?:constexpr\s+)?LLT\s+(\w+)\s*=\s*"
                 r"([^;]+);\s*$", text, re.M)]
    sets = [(m.group(1), [x.strip() for x in split_top(m.group(2))])
            for m in re.finditer(
                r"^(?:constexpr\s+)?std::initializer_list<LLT>\s+(\w+)\s*"
                r"(?:=\s*)?\{([^}]*)\}\s*;?\s*$", text, re.M)]
    ints = {m.group(1): int(m.group(2)) for m in re.finditer(
        r"^(?:static\s+)?(?:constexpr\s+)?(?:unsigned|int)\s+(\w+)\s*=\s*"
        r"(\d+)\s*;\s*$", text, re.M)}
    return types, sets, ints


def constructor_text(path):
    lines = strip_comments(open(path).read())
    text = "\n".join(lines)
    m = re.search(r"::\w+LegalizerInfo\s*\([^)]*const\s+\w+Subtarget\s*&[^)]*\)"
                  r"[^{]*\{", text)
    if not m:
        raise SystemExit("no constructor found")
    start = m.end() - 1
    depth = 0
    for i in range(start, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                body = text[start + 1:i]
                # `strip_comments` keeps one entry per source line, so the
                # offset is a real line number in the input file.
                return body, text[:start + 1].count("\n") + 1
    raise SystemExit("unbalanced constructor")


# ---------------------------------------------------------------------------
# Reviewed deviations
#
# This tool is a one-shot transcriber for the three constructors under
# `lib/Target/*/GISel`, not a general C++ parser.  The shipped corpus is the
# transcription plus the edits below; each one asserts that the text it
# rewrites is still present, so an upstream change fails loudly instead of
# silently applying to nothing.
# ---------------------------------------------------------------------------

def deviation_x86_build_vector(imp):
    """Drop the minimum-element clamp on G_BUILD_VECTOR.

    Upstream clamps both ends (X86LegalizerInfo.cpp:559-563) and then asks for
    the next power of two.  For a narrow build vector that means *adding*
    undef elements, and narrowing the result back recreates the same query, so
    the legalizer re-enqueues it forever (the cycle `gisel graph` finds on the
    unpatched transcription).  Keep the maximum clamp only: a narrow build
    vector is Unsupported and falls back to SelectionDAG.
    """
    rs = next(r for r in imp.rulesets if r.opcodes == ["G_BUILD_VECTOR"])
    before = len(rs.rules)
    keep = [(l, t) for l, t in rs.rules
            if not t.startswith(("clamp_min(", "more("))]
    dropped = before - len(keep)
    if dropped != 5:            # four clamp_min + one more
        raise SystemExit("X86 G_BUILD_VECTOR: expected 5 droppable rules, "
                         "found %d" % dropped)
    rs.rules = keep


def deviation_x86_ctpop(imp):
    """PR 235976: vector CTPOP is legal with VPOPCNTDQ at every width.

    The pinned constructor only lists the 512-bit shapes and gates the 128/256
    bit ones behind VLX.  With VPOPCNTDQ and without VLX that leaves
    `G_CTPOP <4 x s32>` unable to legalize, which upstream's own
    `CodeGen/X86/isel-ctpop-vec.ll` expects to work.  The same patch also
    legalizes byte/halfword vectors via BITALG.
    """
    rs = next(r for r in imp.rulesets if r.opcodes == ["G_CTPOP"])
    if not rs.rules or not rs.rules[1][1].startswith("legal "):
        raise SystemExit("X86 G_CTPOP: expected two leading legal rules")
    if not rs.rules[2][1].startswith("widen("):
        raise SystemExit("X86 G_CTPOP: expected the first widen after the "
                         "scalar legal rules")
    vp = imp.tr.feature_named("HasVPOPCNTDQ", "Subtarget.hasVPOPCNTDQ()")
    bitalg = imp.tr.feature_named("HasBITALG", "Subtarget.hasBITALG()")
    p = PREFIX

    def pairs(types):
        return "[" + ", ".join("[%s%s, %s%s]" % (p, t, p, t)
                               for t in types) + "]"

    rs.rules[2:2] = [
        (0, "legal (%s and in_pairs(slot(0), slot(1), %s))"
            % (vp, pairs(["v4s32", "v8s32", "v16s32",
                          "v2s64", "v4s64", "v8s64"]))),
        (0, "legal (%s and in_pairs(slot(0), slot(1), %s))"
            % (bitalg, pairs(["v16s8", "v32s8", "v64s8",
                              "v8s16", "v16s16", "v32s16"]))),
    ]


X86_DEVIATIONS = (deviation_x86_build_vector, deviation_x86_ctpop)


def render(imp):
    t = imp.t
    out = []
    out.append('include "llvm/Target/LegalizerInfo.td"')
    out.append('include "llvm/Target/Target.td"')
    out.append("")
    # A width select's guard is a feature like any other: resolve the guard
    # names first, so the `feature` block below already contains them.
    select_lines = [
        "select %s = if %s then %s else %s"
        % (emitted, imp.tr.feature(cond), then_name, else_name)
        for emitted, then_name, else_name, cond in getattr(t, "selects", [])]
    for cpp, name in sorted(imp.tr.features.items(), key=lambda kv: kv[1]):
        out.append("feature %s = %s" % (name, cpp))
    out.append("")
    for _, name, rhs in t.decl:
        out.append("type %s = %s" % (name, rhs))
    out.extend(select_lines)
    for name, text in getattr(t, "preds", []):
        out.append("pred %s = %s" % (name, text))
    for name, cond, then, els in getattr(t, "anon", []):
        out.append("select %s = if %s then %s else %s"
                   % (name, cond, then, els))
    for cpp, elems in t.sets.items():
        out.append("type_set %s = [%s]" % (PREFIX + cpp,
                                           ", ".join(t.name(e) for e in elems)))
    out.append("")
    for rs in imp.rulesets:
        if not rs.rules:
            continue
        out.append("ruleset %s: %s" % (rs.name, " ".join(rs.opcodes)))
        for _, text in rs.rules:
            out.append("    " + text)
        out.append("")
    return "\n".join(out)


class Target:
    """One pinned constructor: relative source, prefix, expected counts."""

    def __init__(self, name, prefix, rel, rulesets, rules, deviations=(),
                 pending=False):
        self.name = name
        self.prefix = prefix
        self.rel = rel
        self.rulesets = rulesets
        self.rules = rules
        self.deviations = deviations
        # A pending target has no reviewed corpus yet: `--probe` transcribes
        # it to measure the gap, and the pinned-count drift check does not
        # apply because there is no count to pin.
        self.pending = pending

    @property
    def env(self):
        return "GS_%s_SRC" % self.name.upper()

    @property
    def corpus_name(self):
        return "%sLegalizerInfo.gisel" % self.name


TARGETS = {
    "X86": Target(
        "X86", "X",
        "lib/Target/X86/GISel/X86LegalizerInfo.cpp",
        63, 328, X86_DEVIATIONS),
    "AArch64": Target(
        "AArch64", "A",
        "lib/Target/AArch64/GISel/AArch64LegalizerInfo.cpp",
        117, 744),
    "RISCV": Target(
        "RISCV", "R",
        "lib/Target/RISCV/GISel/RISCVLegalizerInfo.cpp",
        96, 402),
    "M68k": Target(
        "M68k", "M68k",
        "lib/Target/M68k/GISel/M68kLegalizerInfo.cpp",
        6, 13),
    "WebAssembly": Target(
        "WebAssembly", "Wasm",
        "lib/Target/WebAssembly/GISel/WebAssemblyLegalizerInfo.cpp",
        29, 109),
    "PowerPC": Target(
        "PowerPC", "P",
        "lib/Target/PowerPC/GISel/PPCLegalizerInfo.cpp",
        15, 25),
    "ARM": Target(
        "ARM", "A",
        "lib/Target/ARM/ARMLegalizerInfo.cpp",
        52, 96),
    "Mips": Target(
        "Mips", "Mips",
        "lib/Target/Mips/MipsLegalizerInfo.cpp",
        48, 95),
    "SPIRV": Target(
        "SPIRV", "Spv",
        "lib/Target/SPIRV/SPIRVLegalizerInfo.cpp",
        83, 353),
}


_PINNED_SOURCES = {}


def pinned_source(spec, pin_path, git_repo):
    """Materialise a target's source from the pinned commit.

    The provenance of every rule is a commit, not a working tree: a developer
    may carry local patches (the A/B harness does), and those must not change
    what the corpus was transcribed from.  ``git show`` reads the blob the pin
    names, and the recorded sha256 is checked before the importer sees it.
    """
    key = (str(pin_path), str(git_repo), spec.name)
    if key in _PINNED_SOURCES:
        return _PINNED_SOURCES[key]
    pin = json.loads(Path(pin_path).read_text())
    entry = pin["sources"][spec.name]
    proc = subprocess.run(
        ["git", "-C", str(git_repo), "show",
         "%s:%s" % (pin["commit"], entry["path"])],
        capture_output=True, text=True)
    if proc.returncode != 0:
        raise SystemExit("%s: %s is not in %s: %s"
                         % (spec.name, pin["commit"], git_repo,
                            proc.stderr.strip()))
    digest = hashlib.sha256(proc.stdout.encode()).hexdigest()
    if digest != entry["sha256"]:
        raise SystemExit(
            "%s: %s:%s is sha256 %s, pin says %s"
            % (spec.name, pin["commit"], entry["path"], digest,
               entry["sha256"]))
    tmp = Path(tempfile.mkdtemp(prefix="gisel-pin-")) / Path(entry["path"]).name
    tmp.write_bytes(proc.stdout.encode())
    _PINNED_SOURCES[key] = tmp
    print("%s: read %s from %s" % (spec.name, entry["path"], pin["commit"][:12]))
    return tmp


def find_source(spec, args):
    """--source, --pin + --git-repo, $GS_<TARGET>_SRC, or --llvm-src."""
    if args.source:
        return Path(args.source)
    if args.pin:
        if not args.git_repo:
            raise SystemExit("--pin needs --git-repo (the LLVM checkout)")
        return pinned_source(spec, args.pin, args.git_repo)
    env = os.environ.get(spec.env)
    if env:
        return Path(env)
    if args.llvm_src:
        return Path(args.llvm_src) / spec.rel
    raise SystemExit(
        "%s: no source; pass --source, --pin/--git-repo, --llvm-src, "
        "or set %s" % (spec.name, spec.env))


def parse_problem(text):
    """Why `text` is not a gisel corpus, or None when it parses.

    The check goes through the stable CLI (`gisel gen --emit=core`) instead of
    importing `gisel.ast`.  `--emit=core` is the closest published command to
    "this text is a corpus": it rejects syntax the parser does not know but,
    unlike `lint`, does not require every predicate to have a C++ shim (the
    fixtures and probes are deliberately partial).
    """
    with tempfile.NamedTemporaryFile("w", suffix=".gisel",
                                     delete=False) as fh:
        fh.write(text)
        path = fh.name
    core = path + ".core.json"
    env = dict(os.environ)
    prefix = str(GISEL_SRC)
    env["PYTHONPATH"] = (prefix + os.pathsep + env["PYTHONPATH"]
                         if env.get("PYTHONPATH") else prefix)
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "gisel.cli", "gen", path,
             "--emit=core", "--out", core],
            capture_output=True, text=True, env=env,
            cwd=str(GISEL_SRC) if GISEL_SRC.is_dir() else None)
    except OSError as exc:                      # pragma: no cover
        return "cannot run gisel gen: %s" % exc
    finally:
        for leftover in (path, core):
            try:
                os.unlink(leftover)
            except OSError:
                pass
    if proc.returncode == 0:
        return None
    detail = (proc.stderr or proc.stdout).strip()
    return detail or "gisel gen exited %d" % proc.returncode


def transcribe(spec, source, out):
    """Run the transcription; return (exit code, generated text, report)."""
    source = Path(source)
    text = source.read_text()
    STATS["calls"].clear()
    STATS["rules"].clear()
    USED_RENAMES["select"].clear()
    USED_RENAMES["feature"].clear()
    imp = Importer(spec.name, text, spec.prefix)
    # A file-scope helper that returns `LegalityPredicate` is a predicate
    # value, not a bool: `isRegisterType(ST, 0)` only answers when called with
    # the rule's query.  Trust the return type in the file rather than the
    # call, so the `bool` overload of the same name is never rewritten.
    imp.tr.pred_values = set(re.findall(r"LegalityPredicate\s+(\w+)\s*\(", text))
    for name, rhs in member_types(source):
        imp.t.add(name, rhs, 0)
    consts, lists, ints = file_consts(source)
    imp.t.int_consts = ints
    for name, rhs in consts:
        imp.t.add(name, rhs, 0)
    for name, elems in lists:
        imp.t.sets.setdefault(name, elems)
    body, first_line = constructor_text(source)
    imp.block(body, first_line)
    for fix in spec.deviations:
        fix(imp)

    refusals = imp.p.refused + [(l, w) for l, w in imp.t.refused] + \
        [(0, w) for w in imp.tr.refused]
    body = render(imp)
    # `tr.opaque` records every host predicate the parser saw, including one
    # inside a statement that was refused for a different reason (the
    # mutation beside it).  The header must name only the escapes that are
    # actually in the file, or it claims rules that were dropped.
    emitted_opaque = []
    for m in re.finditer(r'opaque\("((?:[^"\\]|\\.)*)"\)', body):
        expr = m.group(1).replace('\\"', '"').replace("\\\\", "\\")
        if expr not in emitted_opaque:
            emitted_opaque.append(expr)
    notes = ""
    if refusals:
        notes = "// Refused (%d statement(s), not transcribed):\n" % len(refusals)
        for line, why in refusals[:40]:
            notes += "//   line %d: %s\n" % (line, why)
        notes += "//\n"
    if imp.skipped:
        notes += ("// Skipped (%d statement(s), data tables for custom "
                  "legalisation, not rules):\n" % len(imp.skipped))
        for line, why in imp.skipped:
            notes += "//   line %d: %s\n" % (line, why)
        notes += "//\n"
    if emitted_opaque:
        notes += ("// Opaque (%d host predicate(s), copied as `opaque(...)`; the "
                  "generated C++ evaluates them, gisel cannot):\n"
                  % len(emitted_opaque))
        for expr in emitted_opaque:
            notes += "//   %s\n" % expr
        notes += "//\n"
    if imp.ignored:
        notes += ("// Ignored (%d statement(s), constructor plumbing, not "
                  "rules):\n" % len(imp.ignored))
        for line, why in imp.ignored:
            notes += "//   line %d: %s\n" % (line, why)
        notes += "//\n"
    name = spec.name + "LegalizerInfo"
    header = HEADER.format(
        name=name, src=source.name, target=spec.name,
        sha=hashlib.sha256(text.encode()).hexdigest(), notes=notes)
    generated = header + body

    rules = sum(len(rs.rules) for rs in imp.rulesets)
    rulesets = sum(1 for rs in imp.rulesets if rs.rules)
    report = {
        "target": spec.name,
        "opcode_source": spec.rel,
        "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
        "corpus_name": spec.corpus_name,
        "corpus_sha256": hashlib.sha256(generated.encode()).hexdigest(),
        "rulesets": rulesets,
        "rules": rules,
        "refused": [{"line": line, "why": why} for line, why in refusals],
        "skipped": [{"line": line, "why": why} for line, why in imp.skipped],
        "ignored": [{"line": line, "why": why} for line, why in imp.ignored],
        "opaque": emitted_opaque,
        "counts": {
            "rulesets": rulesets,
            "rules": rules,
            "refused": len(refusals),
            "skipped": len(imp.skipped),
            "ignored": len(imp.ignored),
            "opaque": len(emitted_opaque),
        },
    }
    if not spec.pending:
        report["pinned"] = {"rulesets": spec.rulesets, "rules": spec.rules}
        report["drift"] = (rulesets, rules) != (spec.rulesets, spec.rules)
    where = str(out) if out else source.name
    print("%s: %d rulesets, %d rules, %d refused"
          % (where, rulesets, rules, len(refusals)))
    for line, why in refusals:
        print("  refused line %s: %s" % (line, why))
    for line, why in imp.skipped:
        print("  skipped line %s: %s (data table, not a rule)" % (line, why))
    for line, why in imp.ignored:
        print("  ignored line %s: %s (not a rule)" % (line, why))

    rc = 1 if refusals else 0
    # The pin only applies to the canonical upstream file, not to fixtures.
    if not spec.pending and source.name == "%sLegalizerInfo.cpp" % spec.name:
        if (rulesets, rules) != (spec.rulesets, spec.rules):
            print("  DRIFT: %s pinned at %d rulesets / %d rules, got %d / %d"
                  % (spec.name, spec.rulesets, spec.rules, rulesets, rules))
            rc = 1
        for kind, table in (("select", SELECT_NAMES),
                            ("feature", FEATURE_NAMES)):
            stale = set(table.get(spec.name, {})) - USED_RENAMES[kind]
            if stale:
                print("  DRIFT: %s %s rename keys no longer match: %s"
                      % (spec.name, kind, ", ".join(sorted(stale))))
                rc = 1
        unnamed = getattr(imp.t, "unnamed", 0)
        if unnamed:
            print("  DRIFT: %s has %d unnamed select(s); extend SELECT_NAMES"
                  % (spec.name, unnamed))
            rc = 1

    # The corpus is gisel source: text the parser rejects is a refusal like
    # any other, not something to write.  A probe of AMDGPU used to emit
    # `replace(ty(LLT::integer(32)))` and `clamp_max(...) when ...`, both of
    # which parse to nothing, so "N rules transcribed" overstated the result.
    problem = parse_problem(generated)
    if problem is not None:
        print("  emitted text does not parse: %s" % problem)
        rc = 1
    elif out is not None:
        Path(out).write_text(generated)
    return rc, generated, report


def show_stats():
    print("%-40s %6s %6s" % ("method", "calls", "rules"))
    for k in sorted(STATS["calls"]):
        print("%-40s %6d %6d" % (k, STATS["calls"][k], STATS["rules"][k]))
    print("%-40s %6d %6d" % ("TOTAL", sum(STATS["calls"].values()),
                             sum(STATS["rules"].values())))


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="One-shot transcription of the three pinned legalizer "
                    "constructors; not a general C++ importer.")
    ap.add_argument("target", nargs="?", choices=sorted(TARGETS))
    ap.add_argument("source", nargs="?",
                    help="source .cpp (default: $GS_<TARGET>_SRC or --llvm-src)")
    ap.add_argument("--all", action="store_true",
                    help="transcribe every pinned target")
    ap.add_argument("--probe", metavar="NAME:PREFIX",
                    help="transcribe a target that has no reviewed corpus "
                         "yet: report counts and refusals, skip the pinned "
                         "counts check, and write nothing unless --out is given")
    ap.add_argument("--src", help="source .cpp for --probe")
    ap.add_argument("--llvm-src", help="LLVM source root containing lib/Target")
    ap.add_argument("--pin",
                    help="pin file naming the upstream commit and source shas "
                         "(the corpus provenance); with --git-repo, defaults "
                         "to the pin shipped in this package")
    ap.add_argument("--git-repo",
                    help="LLVM checkout that contains the pinned commit")
    ap.add_argument("--out", help="output .gisel (single target)")
    ap.add_argument("--out-dir", help="output directory in --all mode")
    ap.add_argument("--check", action="store_true",
                    help="compare with the checked-in corpus instead of writing")
    ap.add_argument("--stats", action="store_true")
    args = ap.parse_args(argv)

    if sum(map(bool, (args.target, args.all, args.probe))) != 1:
        ap.error("pass exactly one of TARGET, --all or --probe")
    if args.all and (args.source or args.out):
        ap.error("a source path and --out are single-target; "
                 "use --llvm-src/--out-dir")
    if not args.all and (args.out_dir or args.check):
        ap.error("--out-dir/--check are --all options")
    if args.git_repo and not args.pin:
        # The wheel ships the pin it was built from, so "--git-repo" alone is
        # enough to materialise a target: the commit and the shas travel with
        # the code that checks them.  A checkout still passes pin/PIN.json
        # explicitly, which wins over the bundled copy.
        bundled = Path(__file__).resolve().parent / "data" / "PIN.json"
        if bundled.is_file():
            args.pin = str(bundled)
    if args.probe and args.src:
        args.source = args.src
    if args.probe and not args.source:
        ap.error("--probe needs --src PATH (it has no pinned path)")
    if args.probe and args.target:
        ap.error("--probe cannot be combined with a TARGET")

    if args.probe:
        name, _, prefix = args.probe.partition(":")
        specs = [Target(name, prefix or name[:1].upper(), "", 0, 0,
                        pending=True)]
    elif args.all:
        specs = [TARGETS[t] for t in sorted(TARGETS)]
    else:
        specs = [TARGETS[args.target]]
    out_dir = Path(args.out_dir) if args.out_dir else CORPUS_DIR

    rc = 0
    reports = {}
    for spec in specs:
        source = find_source(spec, args)
        if args.all:
            dest = out_dir / spec.corpus_name
        elif spec.pending:
            dest = Path(args.out) if args.out else None
        else:
            dest = Path(args.out) if args.out else out_dir / spec.corpus_name
        if args.check:
            digest = hashlib.sha256(source.read_bytes()).hexdigest()
            try:
                old = dest.read_text()
            except FileNotFoundError:
                old = None
            code, generated, report = transcribe(spec, source, None)
            reports[spec.name] = report
            if old is None:
                print("%s: missing (%s)" % (dest, digest))
                rc = 1
            elif generated != old:
                print("%s: DIFFERS (source sha256 %s)" % (dest, digest))
                rc = 1
            else:
                print("%s: matches (source sha256 %s)" % (dest, digest))
            rc = rc or code
        else:
            code, _, report = transcribe(spec, source, dest)
            reports[spec.name] = report
            rc = rc or code
        if args.stats:
            show_stats()

    if args.all:
        # The machine-readable denominator.  The generated file header keeps
        # `refusals[:40]` as a comment; this file is never truncated, so a
        # consumer can tell whether a migration added or removed a refusal.
        report_path = out_dir / "import-report.json"
        payload = {
            "schema": 1,
            "targets": {name: reports[name] for name in sorted(reports)},
        }
        text = json.dumps(payload, indent=2, sort_keys=True) + "\n"
        if args.check:
            try:
                old = report_path.read_text()
            except FileNotFoundError:
                print("%s: missing" % report_path)
                rc = 1
            else:
                if old != text:
                    print("%s: DIFFERS" % report_path)
                    rc = 1
                else:
                    print("%s: matches" % report_path)
        else:
            report_path.write_text(text)
    return rc


if __name__ == "__main__":
    sys.exit(main())
