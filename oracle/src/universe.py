"""A rule-independent LLT / memory / immediate universe for the U queries.

Nothing here reads `.gisel` rules.  The only inputs are the opcode slot counts
from `--dump-arity` and a fixed set of LLT shapes; the only thing borrowed from
gisel is the *spelling* (`gisel.jsonl.llt_text`), so keys stay comparable.

Candidate order is deliberate: representatives first, then one-slot sweeps, then
a seeded random fill.  The runner stops as soon as every rule has a witness, so
the cheap structured part does most of the work.
"""

from __future__ import annotations

import json
import random
from typing import Iterable, Iterator

from gisel.jsonl import llt_text

# --- LLT building blocks -------------------------------------------------

# 1..128 covers the widths the legalizers actually clamp to, including the
# non-power-of-two ones (24/31/33/48/63/80/96).  256/512/1024 catch the
# "widen to a declared width" rules that live above 128.
SCALAR_BITS = list(range(1, 129)) + [256, 512, 1024]
INTERESTING_SCALARS = [8, 16, 32, 64, 1, 128, 24, 31, 33, 48, 63, 80, 96,
                       256, 512, 1024]
INTEGER_BITS = [1, 8, 16, 32, 64, 128]
FLOAT_BITS = [16, 32, 64, 128]

FIXED_LANES = [2, 3, 4, 6, 8, 12, 16, 32, 64, 128]
SCALABLE_LANES = [1, 2, 3, 4, 8, 16]
VECTOR_ELEMS = [("scalar", 8), ("scalar", 16), ("scalar", 32),
                ("scalar", 64), ("scalar", 128), ("float", 16),
                ("float", 32), ("float", 64), ("bf16",)]

POINTER_AS = [0, 1, 2, 3, 4]
POINTER_BITS = [32, 64]

MEM_TYPES = [("scalar", 8), ("scalar", 16), ("scalar", 32), ("scalar", 64),
             ("float", 32), ("float", 64), ("vector", 2, False, ("scalar", 32)),
             ("vector", 4, False, ("scalar", 8))]
MEM_ALIGNS = [1, 8, 16, 32, 64, 128]
# Pairs that are legal for a cmpxchg; a handful is enough to exercise the
# ordering predicates without exploding the Cartesian product.
MEM_ORDERINGS = [
    ("NotAtomic", "NotAtomic"),
    ("Monotonic", "Monotonic"),
    ("Acquire", "Acquire"),
    ("Release", "Monotonic"),
    ("AcquireRelease", "Acquire"),
    ("SequentiallyConsistent", "SequentiallyConsistent"),
]

IMMEDIATES = [-1, 0, 1, 2, 3, 4, 8, 16, 31, 32, 63, 64, 127, 128, 255, 256]

# The runner cannot represent fixed one-lane vectors (LLT::vector asserts that
# the element count is not scalar), so `v1s8` is normalised to `s8` by the
# spelling helper.  Scalable one-lane vectors are fine.
MEM_OPCODE_HINTS = ("LOAD", "STORE", "ATOMIC", "CMPXCHG", "MEMCPY", "MEMSET")


def _dedup(items: Iterable) -> list:
    seen, out = set(), []
    for item in items:
        if item in seen:
            continue
        seen.add(item)
        out.append(item)
    return out


def scalars() -> list[tuple]:
    return [("scalar", n) for n in SCALAR_BITS]


def interesting_scalars() -> list[tuple]:
    return [("scalar", n) for n in INTERESTING_SCALARS]


def integers() -> list[tuple]:
    return [("integer", n) for n in INTEGER_BITS]


def floats() -> list[tuple]:
    return [("float", n) for n in FLOAT_BITS] + [("bf16",)]


def vectors(scalable: bool = True) -> list[tuple]:
    out = []
    for elem in VECTOR_ELEMS:
        for lanes in FIXED_LANES:
            out.append(("vector", lanes, False, elem))
        if scalable:
            for lanes in SCALABLE_LANES:
                out.append(("vector", lanes, True, elem))
    return _dedup(out)


def pointers() -> list[tuple]:
    return [("pointer", as_, bits)
            for as_ in POINTER_AS for bits in POINTER_BITS]


def pointer_vectors() -> list[tuple]:
    """Vectors of pointers; `clampMaxNumElements(0, p0, N)` needs them."""
    out = []
    for bits in (32, 64):
        for lanes in (2, 4, 8, 16):
            out.append(("vector", lanes, False, ("pointer", 0, bits)))
    return out


def type_values(target: str = "generic") -> list[tuple]:
    """Every type a slot may take, common shapes first.

    X86 has no scalable vectors; asking its legalizer about `nxv*` trips
    `TypeSize::operator ScalarTy()` inside upstream predicates.
    """
    head = interesting_scalars() + integers() + floats()
    tail = [t for t in scalars() if t not in head]
    scalable = target != "x86"
    return _dedup(head + vectors(scalable) + pointer_vectors() + pointers()
                  + tail)


REPRESENTATIVES = [("scalar", 8), ("scalar", 16), ("scalar", 32),
                   ("scalar", 64), ("vector", 4, False, ("scalar", 8)),
                   ("vector", 2, False, ("scalar", 64)),
                   ("pointer", 0, 32), ("pointer", 0, 64)]


def spell(t: tuple) -> str:
    return llt_text(t)


def spell_mem(mem: tuple | None) -> list | None:
    if mem is None:
        return None
    mty, align, ordering, failure = mem
    return [spell(mty), align, ordering, failure]


def query(opcode: str, types: Iterable[tuple], mem: tuple | None = None,
          imms: Iterable[int] = ()) -> dict:
    return {"name": opcode,
            "types": [spell(t) for t in types],
            "mem": spell_mem(mem),
            "imms": list(imms)}


def _imms_for(imm_slots: int, index: int) -> list[int]:
    if imm_slots <= 0:
        return []
    values = [IMMEDIATES[index % len(IMMEDIATES)]]
    while len(values) < imm_slots:
        values.append(0)
    return values


DEFAULT_MEM = (("scalar", 32), 32, "NotAtomic", "NotAtomic")


def _mem_variants(opcode: str) -> list[tuple]:
    # Always carry a mem desc: some upstream predicates index MMOs[0] without
    # checking, and an empty list aborts the process instead of answering.
    # For opcodes that ignore mem this is inert; for the hinted ones it is the
    # axis under test.
    if not any(hint in opcode for hint in MEM_OPCODE_HINTS):
        return [DEFAULT_MEM]
    out: list[tuple] = [DEFAULT_MEM]
    for mty in MEM_TYPES[:4]:
        for align in MEM_ALIGNS:
            out.append((mty, align, "NotAtomic", "NotAtomic"))
    for mty in MEM_TYPES[:4]:
        for ordering, failure in MEM_ORDERINGS[1:]:
            out.append((mty, 32, ordering, failure))
    return out


COHERENT_SHAPES = [
    ("scalar", 8), ("scalar", 16), ("scalar", 32), ("scalar", 64),
    ("scalar", 80), ("scalar", 128), ("float", 16), ("float", 32),
    ("float", 64), ("float", 128),
    ("vector", 2, False, ("scalar", 8)),
    ("vector", 4, False, ("scalar", 8)),
    ("vector", 8, False, ("scalar", 8)),
    ("vector", 16, False, ("scalar", 8)),
    ("vector", 32, False, ("scalar", 8)),
    ("vector", 64, False, ("scalar", 8)),
    ("vector", 2, False, ("scalar", 16)),
    ("vector", 4, False, ("scalar", 16)),
    ("vector", 8, False, ("scalar", 16)),
    ("vector", 16, False, ("scalar", 16)),
    ("vector", 2, False, ("scalar", 32)),
    ("vector", 4, False, ("scalar", 32)),
    ("vector", 8, False, ("scalar", 32)),
    ("vector", 16, False, ("scalar", 32)),
    ("vector", 2, False, ("scalar", 64)),
    ("vector", 4, False, ("scalar", 64)),
    ("vector", 8, False, ("scalar", 64)),
    ("vector", 2, False, ("pointer", 0, 64)),
    ("vector", 4, False, ("pointer", 0, 64)),
    ("vector", 2, False, ("pointer", 0, 32)),
    ("vector", 4, False, ("pointer", 0, 32)),
    ("vector", 8, False, ("pointer", 0, 64)),
    ("vector", 16, False, ("pointer", 0, 64)),
]

VEC_ELEM = [(t, t[3]) for t in COHERENT_SHAPES if t[0] == "vector"]


def _coherent_combos(type_slots: int) -> list[tuple]:
    """Pointer/value, vector pair, vector/element and mixed scalar tuples.

    Slot order matters: X86 load/store are `{value, ptr}`, while G_PTR_ADD is
    `{ptr, offset}`, so both orders are emitted.  Pointer combinations come
    first so a small per-opcode cap keeps the mem-reading rules reachable.
    """
    out: list[tuple] = []
    pointers = (("pointer", 0, 32), ("pointer", 0, 64))
    shapes = COHERENT_SHAPES
    if type_slots >= 2:
        for ptr in pointers:
            for shape in shapes:
                out.append((shape, ptr) + (shape,) * (type_slots - 2))
                out.append((ptr, shape) + (shape,) * (type_slots - 2))
        # Vector pairs: {sub, full} for G_EXTRACT/G_INSERT and friends.
        for elem in (("scalar", 8), ("scalar", 16), ("scalar", 32),
                     ("scalar", 64)):
            for small, large in ((16, 32), (32, 64), (8, 16), (4, 8), (2, 4)):
                out.append((("vector", small, False, elem),
                            ("vector", large, False, elem)))
                out.append((("vector", large, False, elem),
                            ("vector", small, False, elem)))
        for vec, elem in VEC_ELEM:
            out.append((vec, elem) + (elem,) * (type_slots - 2))
            out.append((vec,) + (vec,) * (type_slots - 1))
            for index in (("scalar", 32), ("scalar", 64)):
                out.append((vec, elem, index) if type_slots >= 3
                           else (vec, index))
    # Mixed scalar pairs used by G_SELECT / G_IS_FPCLASS / G_FPEXT /
    # G_FPTRUNC / G_FPTOUI / G_UITOFP.
    for pair in ((("float", 16), ("float", 32)),
                 (("float", 32), ("float", 64)),
                 (("float", 64), ("float", 128)),
                 (("float", 16), ("float", 64)),
                 (("float", 16), ("float", 128)),
                 (("scalar", 16), ("scalar", 32)),
                 (("scalar", 8), ("scalar", 32)),
                 (("scalar", 64), ("scalar", 32)),
                 (("scalar", 80), ("scalar", 32)),
                 (("scalar", 80), ("scalar", 64)),
                 (("scalar", 32), ("scalar", 32)),
                 (("scalar", 64), ("scalar", 64)),
                 (("scalar", 32), ("scalar", 64)),
                 (("scalar", 32), ("scalar", 16)),
                 (("scalar", 1), ("scalar", 32)),
                 (("scalar", 1), ("scalar", 64)),
                 (("scalar", 1), ("scalar", 80))):
        out.append(pair + (pair[1],) * (type_slots - 2))
    # Same lane count, different element width: {f64, f32}, {f32, f64}, ...
    if type_slots >= 2:
        for lanes in (2, 4, 8, 16):
            for narrow, wide in ((32, 64), (16, 32), (64, 128)):
                small = ("vector", lanes, False, ("scalar", narrow))
                big = ("vector", lanes, False, ("scalar", wide))
                out.append((big, small) + (small,) * (type_slots - 2))
                out.append((small, big) + (big,) * (type_slots - 2))
    for shape in shapes:
        out.append((shape,) * type_slots)
    return _dedup(out)


def _coherent_mem(types: tuple) -> tuple:
    """A mem desc whose size/alignment matches the value slot."""
    t = types[-1] if types else ("scalar", 32)
    if t[0] == "pointer" and len(types) > 1:
        t = types[-2]
    if t[0] == "vector":
        size = t[1] * t[3][1] if t[3][0] in ("scalar", "integer", "float") else 32
        mty = t
    elif t[0] in ("scalar", "integer", "float"):
        size = t[1]
        mty = t
    elif t[0] == "bf16":
        size, mty = 16, t
    else:
        size, mty = 32, ("scalar", 32)
    align = size if size and size <= 128 else 32
    return (mty, align, "NotAtomic", "NotAtomic")


def candidates(opcode: str, type_slots: int, imm_slots: int, seed: int = 0,
               limit: int = 1200, target: str = "generic") -> list[dict]:
    """Structured candidate queries for one opcode.

    `limit` caps the total (0 = no cap); the representative and one-slot
    sweeps are emitted first so a small cap still covers the useful shapes.
    """
    values = type_values(target)
    reps = REPRESENTATIVES
    mems = _mem_variants(opcode)

    combos: list[tuple] = []
    if type_slots == 0:
        combos.append(())
    elif type_slots == 1:
        combos.extend((v,) for v in values)
        combos.extend((v,) for v in reps)
    else:
        combos.extend(_coherent_combos(type_slots))
        # All-representative combinations.
        def product(prefix, depth):
            if depth == 0:
                combos.append(tuple(prefix))
                return
            for v in reps:
                product(prefix + [v], depth - 1)
        product([], type_slots)
        # One-slot-at-a-time sweeps with the other slots at their first rep.
        for slot in range(type_slots):
            for v in values:
                row = [reps[0]] * type_slots
                row[slot] = v
                combos.append(tuple(row))

    combos = _dedup(combos)

    out: list[dict] = []
    # Memory variants multiply fast (types x mem shapes).  Only the leading
    # representative combinations get the full mem axis; the rest are queried
    # without one.  `--dump-hits` does not care about a missing mem operand,
    # and the mem-reading rules are concentrated on a few opcodes anyway.
    mem_prefix = 0
    if type_slots and len(mems) > 1:
        mem_prefix = max(1, 200 // len(mems))
    for index, types in enumerate(combos):
        coherent = _coherent_mem(types) if type_slots else DEFAULT_MEM
        has_pointer = any(t[0] == "pointer" for t in types)
        if has_pointer and len(mems) > 1:
            mty = coherent[0]
            mem_choices = _dedup([coherent, (mty, 1, "NotAtomic", "NotAtomic")]
                                 + mems[:6])
        elif index < mem_prefix and len(mems) > 1:
            mem_choices = _dedup([coherent] + mems)
        else:
            mem_choices = [coherent]
        for mem in mem_choices:
            out.append(query(opcode, types, mem, _imms_for(imm_slots, index)))

    # Seeded random fill: pair/triple combinations that the sweeps miss.
    rng = random.Random(seed)
    extra: list[dict] = []
    for _ in range(max(0, limit - len(out)) if limit else 0):
        types = tuple(rng.choice(values) for _ in range(type_slots))
        mem = rng.choice(mems)
        extra.append(query(opcode, types, mem,
                           _imms_for(imm_slots, rng.randrange(len(IMMEDIATES)))))
    out.extend(extra)

    uniq, seen = [], set()
    for row in out:
        key = json.dumps(row, sort_keys=True)
        if key in seen:
            continue
        seen.add(key)
        uniq.append(row)
    if limit:
        uniq = uniq[:limit]
    return uniq


def build(arity: dict[str, tuple[int, int]], seed: int = 0, limit: int = 1200,
          opcodes: Iterable[str] | None = None,
          target: str = "generic") -> Iterator[dict]:
    wanted = set(opcodes) if opcodes else None
    for opcode in sorted(arity):
        if wanted is not None and opcode not in wanted:
            continue
        type_slots, imm_slots = arity[opcode]
        for row in candidates(opcode, type_slots, imm_slots, seed, limit,
                              target):
            yield row


def write(rows: Iterable[dict], path: str) -> int:
    count = 0
    with open(path, "w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, sort_keys=True) + "\n")
            count += 1
    return count


# --- conservative salvage profile ---------------------------------------
#
# Some opcodes assert on shapes the DSL can still spell (scalable sizes,
# scalar where a vector is required).  After the main pass marks an opcode
# `asserted`, this profile retries it with combinations the upstream predicates
# are known to accept.  It is deliberately narrow: scalar-only, fixed vectors,
# vector+element, and vector+mask.

SAFE_SHAPES = [
    ("scalar", 8), ("scalar", 16), ("scalar", 32), ("scalar", 64),
    ("scalar", 1), ("float", 32), ("float", 64),
    ("vector", 2, False, ("scalar", 8)),
    ("vector", 4, False, ("scalar", 8)),
    ("vector", 8, False, ("scalar", 8)),
    ("vector", 16, False, ("scalar", 8)),
    ("vector", 2, False, ("scalar", 16)),
    ("vector", 4, False, ("scalar", 16)),
    ("vector", 2, False, ("scalar", 32)),
    ("vector", 4, False, ("scalar", 32)),
    ("vector", 2, False, ("scalar", 64)),
]


def safe_candidates(opcode: str, type_slots: int, imm_slots: int,
                    target: str = "generic") -> list[dict]:
    combos: list[tuple] = []
    if type_slots == 0:
        combos.append(())
    elif opcode in ("G_ICMP", "G_FCMP"):
        # Same-shape only: the rules call getElementType on a slot without
        # checking isVector, so a scalar/vector mix aborts upstream.
        for shape in SAFE_SHAPES:
            combos.append((shape, shape))
    elif opcode == "G_EXTRACT_VECTOR_ELT":
        # {result, vector, index}; the vector slot must stay a vector.
        vecs = [t for t in SAFE_SHAPES if t[0] == "vector"]
        scalars = [t for t in SAFE_SHAPES if t[0] != "vector"]
        for v in vecs:
            for s in scalars:
                for idx in (("scalar", 32), ("scalar", 64)):
                    combos.append((s, v, idx))
    else:
        for shape in SAFE_SHAPES:
            combos.append((shape,) * type_slots)
        for vec in [t for t in SAFE_SHAPES if t[0] == "vector"]:
            elem = vec[3]
            for other in (elem, ("scalar", 1), ("scalar", 32),
                          ("scalar", 64)):
                combos.append((vec, other) + (other,) * (type_slots - 2))
                combos.append((other, vec) + (other,) * (type_slots - 2))
        for small in SAFE_SHAPES:
            for large in SAFE_SHAPES:
                if small[0] == "scalar" and large[0] == "scalar":
                    combos.append((small, large)
                                  + (large,) * (type_slots - 2))
    combos = _dedup(combos)
    out = []
    for index, types in enumerate(combos):
        mem = _coherent_mem(types) if type_slots else DEFAULT_MEM
        out.append(query(opcode, types, mem, _imms_for(imm_slots, index)))
    return out


def safe_build(arity: dict[str, tuple[int, int]], opcodes: Iterable[str],
               target: str = "generic") -> Iterator[dict]:
    for opcode in sorted(set(opcodes)):
        if opcode not in arity:
            continue
        type_slots, imm_slots = arity[opcode]
        for row in safe_candidates(opcode, type_slots, imm_slots, target):
            yield row
