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
