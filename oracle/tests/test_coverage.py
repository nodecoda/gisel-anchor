import json
from pathlib import Path

from oracle.src import coverage


def write_jsonl(path: Path, rows):
    path.write_text("".join(json.dumps(r) + "\n" for r in rows),
                    encoding="utf-8")
    return path


def row(config="C1", opcode="G_ADD", index=0, status="witnessed"):
    return {"config": config, "opcode": opcode, "ruleset": opcode,
            "rule_index": index, "status": status,
            "witness": {"name": opcode, "types": ["s64"], "mem": None,
                        "imms": []} if status == "witnessed" else None,
            "shadowed_by": -1, "shadow_query": None, "evaluated": 1,
            "candidates": 1}


def test_merge_lets_the_salvage_pass_replace_asserted(tmp_path):
    main = write_jsonl(tmp_path / "a.jsonl", [row(status="asserted")])
    salvage = write_jsonl(tmp_path / "b.jsonl", [row(status="witnessed")])
    merged = coverage.merge([main, salvage])
    assert len(merged) == 1
    assert merged[0]["status"] == "witnessed"


def test_build_counts_and_sections():
    report = coverage.build([row(), row(index=1, status="unexercised")],
                            "x86")
    assert report["counts"]["rules"] == 2
    assert report["counts"]["witnessed"] == 1
    assert report["configs"][0]["config"] == "C1"
    assert report["configs"][0]["rules"][0]["witness"] is not None


def test_gate_requires_a_reason_and_follow_up():
    report = coverage.build([row(status="unexercised")], "x86")
    problems = coverage.gate(report, {"gaps": [
        {"opcode": "G_ADD", "rule_index": 0, "status": "unexercised",
         "reason": "", "follow_up": ""}]})
    kinds = {p["kind"] for p in problems}
    assert "gap-without-reason" in kinds
    assert "gap-without-follow-up" in kinds


def test_gate_flags_unexplained_and_stale_entries():
    report = coverage.build([row(status="unexercised")], "x86")
    problems = coverage.gate(report, {"gaps": [
        {"opcode": "G_OTHER", "reason": "r", "follow_up": "f"}]})
    kinds = {p["kind"] for p in problems}
    assert "unexplained-gap" in kinds
    assert "stale-gap-entry" in kinds


def test_gate_passes_when_every_gap_is_explained():
    report = coverage.build([row(status="unexercised")], "x86")
    problems = coverage.gate(report, {"gaps": [
        {"config": "*", "opcode": "G_ADD", "rule_index": 0,
         "status": "unexercised", "reason": "r", "follow_up": "f"}]})
    assert problems == []


def test_rule_witnessed_in_another_config_is_not_a_gap():
    report = coverage.build([row(config="C0", status="unexercised"),
                             row(config="C2", status="witnessed")], "x86")
    assert report["counts"]["unexercised_everywhere"] == 0
    assert coverage.gate(report, {"gaps": []}) == []


def test_rule_never_witnessed_anywhere_is_a_gap():
    report = coverage.build([row(config="C0", status="unexercised"),
                             row(config="C2", status="unexercised")], "x86")
    assert report["counts"]["unexercised_everywhere"] == 2
    problems = coverage.gate(report, {"gaps": []})
    assert {p["kind"] for p in problems} == {"unexplained-gap"}
