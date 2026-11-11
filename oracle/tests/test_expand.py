import json

from oracle.src import expand


ROWS = [
    {"name": "G_ADD", "types": ["s32"], "mem": ["s32", 32, "NotAtomic", "NotAtomic"],
     "imms": []},
    {"name": "G_ADD", "types": ["s64"], "mem": ["s64", 64, "NotAtomic", "NotAtomic"],
     "imms": []},
]


def test_one_row_per_config_in_config_major_order():
    rows = list(expand.build(ROWS, ["C0", "C1"]))
    assert [row["config"] for row in rows] == ["C0", "C0", "C1", "C1"]
    assert [row["types"][0] for row in rows] == ["s32", "s64", "s32", "s64"]


def test_expansion_does_not_mutate_the_universe_rows():
    list(expand.build(ROWS, ["C0"]))
    assert "config" not in ROWS[0]


def test_write_counts_rows(tmp_path):
    path = tmp_path / "full.jsonl"
    count = expand.write(path, expand.build(ROWS, ["C0", "C1", "C2"]))
    assert count == 6
    lines = [json.loads(line) for line in path.read_text().splitlines()]
    assert {row["config"] for row in lines} == {"C0", "C1", "C2"}
