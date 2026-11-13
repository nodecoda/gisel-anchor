import json

from gisel_anchor.oracle import emit


def witness_row(config="C1", opcode="G_ADD", index=0):
    return {"config": config, "opcode": opcode, "ruleset": opcode,
            "rule_index": index, "status": "witnessed",
            "witness": {"name": opcode, "types": ["s64"], "mem": None,
                        "imms": []}}


def asserted_row(config="C1", opcode="G_TRUNC", index=0):
    return {"config": config, "opcode": opcode, "ruleset": opcode,
            "rule_index": index, "status": "asserted", "witness": None}


def universe():
    return {"G_ADD": [{"name": "G_ADD", "types": [f"s{8 * i}"], "mem": None,
                       "imms": []} for i in range(1, 5)],
            "G_TRUNC": [{"name": "G_TRUNC", "types": ["s64"], "mem": None,
                         "imms": []}]}


def test_witness_queries_carry_the_rule_label():
    rows = emit.build([witness_row()], universe(), sample=0)
    assert len(rows) == 1
    assert rows[0]["origin"] == "witness"
    assert rows[0]["rule"] == 0


def test_asserted_opcodes_are_never_asked():
    rows = emit.build([witness_row(), asserted_row()], universe(), sample=4)
    assert all(row["name"] != "G_TRUNC" for row in rows)


def test_sample_is_deterministic_and_capped():
    first = emit.build([witness_row()], universe(), sample=2)
    second = emit.build([witness_row()], universe(), sample=2)
    assert json.dumps(first, sort_keys=True) == json.dumps(second, sort_keys=True)
    samples = [r for r in first
               if r["origin"] == "sample" and r["name"] == "G_ADD"]
    assert len(samples) == 2


def test_witness_query_is_not_overwritten_by_a_sample():
    rows = emit.build([witness_row()], universe(), sample=8)
    keys = [(r["name"], tuple(r["types"])) for r in rows]
    assert len(keys) == len(set(keys))
    match = [r for r in rows if r["name"] == "G_ADD" and r["types"] == ["s64"]]
    assert match[0]["origin"] == "witness"
