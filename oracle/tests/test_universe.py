import json

from oracle.src import universe


def test_scalars_cover_odd_and_wide_widths():
    values = set(universe.type_values("x86"))
    for bits in (1, 7, 24, 31, 33, 128, 512):
        assert ("scalar", bits) in values, bits


def test_x86_has_no_scalable_vectors():
    assert not any(t[0] == "vector" and t[2] for t in universe.type_values("x86"))
    assert any(t[0] == "vector" and t[2]
               for t in universe.type_values("aarch64"))


def test_pointer_vectors_exist_for_clamp_max_num_elements():
    assert any(t[0] == "vector" and t[3][0] == "pointer"
               for t in universe.type_values("x86"))


def test_candidates_are_deterministic():
    first = universe.candidates("G_ADD", 1, 0, seed=7, limit=40)
    second = universe.candidates("G_ADD", 1, 0, seed=7, limit=40)
    assert json.dumps(first, sort_keys=True) == json.dumps(second, sort_keys=True)


def test_candidates_carry_a_mem_desc():
    for row in universe.candidates("G_ADD", 1, 0, limit=10):
        assert row["mem"] is not None


def test_icmp_safe_profile_keeps_slots_the_same_shape():
    arity = {"G_ICMP": (2, 0), "G_ADD": (1, 0)}
    rows = list(universe.safe_build(arity, ["G_ICMP"]))
    assert rows
    assert all(row["types"][0] == row["types"][1] for row in rows)


def test_safe_profile_extract_keeps_a_vector_in_slot_one():
    rows = list(universe.safe_build({"G_EXTRACT_VECTOR_ELT": (3, 0)},
                                    ["G_EXTRACT_VECTOR_ELT"]))
    assert rows
    assert all(row["types"][1].startswith(("v", "nxv")) for row in rows)


def test_coherent_combos_carry_scalable_shapes_for_vector_targets():
    combos = universe._coherent_combos(2, "riscv")
    assert any(a[0] == "vector" and a[2] and a[3][0] == "pointer"
               for a, _ in combos), "pointer-vector/ptr shape is missing"
    assert any(a[0] == "vector" and a[2] and a[3] == ("scalar", 1)
               for a, _ in combos), "i1 mask vector shape is missing"
    assert not any(a[0] == "vector" and a[2]
                   for a, _ in universe._coherent_combos(2, "x86")), \
        "x86 must not ask about scalable vectors"


def test_cmpxchg_with_success_asks_the_trailing_pointer_first():
    rows = universe.candidates("G_ATOMIC_CMPXCHG_WITH_SUCCESS", 3, 0,
                               limit=1200, target="riscv")
    early = [(row["types"][0], row["types"][2]) for row in rows[:40]]
    assert any(a in ("s8", "s16", "s32", "s64") and b.startswith("p0:")
               for a, b in early), early


def test_s128_pairs_are_asked():
    rows = universe.candidates("G_FPEXT", 2, 0, limit=1200, target="riscv")
    keys = {(row["types"][0], row["types"][1]) for row in rows}
    assert ("s128", "s32") in keys
    assert ("s128", "s64") in keys


def test_aarch64_asks_about_atomic_ordering_on_pointer_forms():
    rows = universe.candidates("G_STORE", 2, 0, limit=1200, target="aarch64")
    orderings = {row["mem"][2] for row in rows if row["mem"]}
    assert "Release" in orderings and "AcquireRelease" in orderings, orderings
    # The axis is AArch64-only until each target's U side is regenerated.
    for target in ("x86", "riscv"):
        rows = universe.candidates("G_STORE", 2, 0, limit=1200, target=target)
        assert {row["mem"][2] for row in rows if row["mem"]} == {"NotAtomic"}
