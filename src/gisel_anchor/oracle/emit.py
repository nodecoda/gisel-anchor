"""Assemble the U query file: one witness per rule plus a fixed sample.

The witness list alone is unbalanced -- an opcode with no rules contributes
nothing, and an opcode whose rules are all hit by `s32` never asks about
vectors.  So each `(config, opcode)` also gets a deterministic, evenly spaced
sample from the same universe.  Opcodes whose upstream evaluation aborted
(`asserted`) are excluded entirely: asking them a question crashes the runner
instead of producing a comparable action.
"""

from __future__ import annotations

import json
from pathlib import Path


def load_universe(path: str | Path) -> dict[str, list[dict]]:
    by_opcode: dict[str, list[dict]] = {}
    with open(path, "r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            by_opcode.setdefault(row["name"], []).append(row)
    return by_opcode


def safe_opcodes(rows: list[dict]) -> set[tuple[str, str]]:
    """(config, opcode) pairs where no rule evaluation aborted upstream."""
    bad = {(row["config"], row["opcode"])
           for row in rows if row["status"] == "asserted"}
    return bad


def _key(config: str, row: dict) -> tuple:
    return (config, row.get("name"), json.dumps(row.get("types"), sort_keys=True),
            json.dumps(row.get("mem"), sort_keys=True),
            json.dumps(row.get("imms"), sort_keys=True))


def _evenly(items: list[dict], count: int) -> list[dict]:
    if count <= 0 or not items:
        return []
    if len(items) <= count:
        return list(items)
    step = len(items) / float(count)
    return [items[int(i * step)] for i in range(count)]


def build(rows: list[dict], universe: dict[str, list[dict]],
          sample: int = 8) -> list[dict]:
    aborted = safe_opcodes(rows)
    configs: list[str] = []
    for row in rows:
        if row["config"] not in configs:
            configs.append(row["config"])

    out: dict[tuple, dict] = {}
    order: list[tuple] = []

    def add(config: str, row: dict, origin: str, rule) -> None:
        record = {"config": config, "name": row["name"],
                  "types": row["types"], "mem": row.get("mem"),
                  "imms": row.get("imms", []),
                  "ruleset": row["name"], "rule": rule, "origin": origin}
        key = _key(config, record)
        if key not in out:
            order.append(key)
        if key not in out or origin == "witness":
            out[key] = record

    for row in rows:
        if row["status"] == "witnessed" and row.get("witness"):
            add(row["config"], row["witness"], "witness", row["rule_index"])

    for config in configs:
        for opcode in sorted(universe):
            if (config, opcode) in aborted:
                continue
            for row in _evenly(universe[opcode], sample):
                add(config, row, "sample", None)

    return [out[key] for key in order]


def write(rows: list[dict], path: str | Path) -> int:
    with open(path, "w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, sort_keys=True) + "\n")
    return len(rows)
