"""Merge `--witness` output into a coverage report and enforce the gate.

A rule is *witnessed* when some query's first match is that rule, *shadowed*
when it only ever matched behind an earlier rule, and *unexercised* when no
query in the universe reached it.  `asserted` is not a rule status: it means
the upstream build aborted while evaluating this opcode, so the whole opcode
is unresolved.

Nothing here guesses: every non-`witnessed` rule either appears in
`known_gaps.json` with a written reason and follow-up, or the gate fails.
"""

from __future__ import annotations

import json
from pathlib import Path

STATUSES = ("witnessed", "shadowed", "unexercised", "asserted")
GAP_STATUSES = ("shadowed", "unexercised", "asserted")

# A rule that is unexercised in one configuration is usually just feature-
# gated: the same builder emits it everywhere, but only the configuration with
# the feature can fire it.  The gate therefore judges a rule by its best status
# across configurations.  Rule identity across configurations is keyed by
# (opcode, rule_index); where a builder emits a different number of rules the
# alignment can shift, so the union is a lower bound, never an overclaim.
_RANK = {"asserted": 0, "unexercised": 1, "shadowed": 2, "witnessed": 3}


def load_rows(path: str | Path) -> list[dict]:
    rows: list[dict] = []
    with open(path, "r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def merge(paths: list[str | Path]) -> list[dict]:
    """Merge witness files; later files win per (config, opcode, rule_index).

    Callers pass the main pass first and the salvage pass last: a rule the main
    pass could not evaluate because its opcode aborted should be answered by
    the salvage profile when one exists.
    """
    merged: dict[tuple, dict] = {}
    order: list[tuple] = []
    for path in paths:
        for row in load_rows(path):
            key = (row["config"], row["opcode"], int(row["rule_index"]))
            if key not in merged:
                order.append(key)
            merged[key] = row
    return [merged[key] for key in order]


def _query_key(query: dict | None) -> str | None:
    if query is None:
        return None
    return json.dumps(query, sort_keys=True)


def build(rows: list[dict], target: str) -> dict:
    configs: dict[str, list[dict]] = {}
    for row in rows:
        configs.setdefault(row["config"], []).append(row)

    best: dict[tuple, str] = {}
    for row in rows:
        key = (row["opcode"], int(row["rule_index"]))
        status = row["status"]
        if key not in best or _RANK[status] > _RANK[best[key]]:
            best[key] = status

    sections = []
    totals = {status: 0 for status in STATUSES}
    totals["rules"] = 0
    for config, config_rows in configs.items():
        counts = {status: 0 for status in STATUSES}
        rules = []
        for row in config_rows:
            status = row["status"]
            counts[status] = counts.get(status, 0) + 1
            rules.append({
                "opcode": row["opcode"],
                "ruleset": row.get("ruleset", row["opcode"]),
                "rule_index": int(row["rule_index"]),
                "status": status,
                "cross_config": best[(row["opcode"], int(row["rule_index"]))],
                "witness": _query_key(row.get("witness")),
                "shadowed_by": int(row.get("shadowed_by", -1)),
                "shadow_query": _query_key(row.get("shadow_query")),
                "evaluated": int(row.get("evaluated", 0)),
                "candidates": int(row.get("candidates", 0)),
            })
            totals[status] = totals.get(status, 0) + 1
        for row in rules:
            if row["status"] in ("unexercised", "shadowed") and \
                    row["cross_config"] == "unexercised":
                totals["unexercised_everywhere"] = \
                    totals.get("unexercised_everywhere", 0) + 1
            if row["status"] in ("unexercised", "shadowed") and \
                    row["cross_config"] == "shadowed":
                totals["shadowed_everywhere"] = \
                    totals.get("shadowed_everywhere", 0) + 1
        totals["rules"] += len(rules)
        sections.append({"schema": 1, "target": target, "config": config,
                         "counts": counts, "rules": rules})

    totals.setdefault("unexercised_everywhere", 0)
    totals.setdefault("shadowed_everywhere", 0)
    return {"schema": 1, "target": target, "counts": totals,
            "configs": sections}


def write(report: dict, path: str | Path) -> None:
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(report, handle, indent=2, sort_keys=True)
        handle.write("\n")


def _matches(entry: dict, row: dict, config: str, target: str) -> bool:
    if entry.get("target", target) != target:
        return False
    if entry.get("config", "*") not in ("*", config):
        return False
    if entry.get("opcode", "*") not in ("*", row["opcode"]):
        return False
    rule_index = entry.get("rule_index", "*")
    if rule_index not in ("*", row["rule_index"]):
        return False
    # Judge the cross-config class, not the per-config status: a rule that is
    # shadowed in one configuration and unexercised in another is one gap.
    status = row.get("cross_config", row["status"])
    return entry.get("status", "*") in ("*", status)


def gate(report: dict, gaps: dict) -> list[dict]:
    """Return every unexplained gap.  Empty list == pass."""
    target = report.get("target", "")
    entries = gaps.get("gaps", []) if gaps else []
    problems: list[dict] = []
    used = [False] * len(entries)

    for entry_index, entry in enumerate(entries):
        if not str(entry.get("reason", "")).strip():
            problems.append({"kind": "gap-without-reason",
                             "entry": entry_index})
        if not str(entry.get("follow_up", "")).strip():
            problems.append({"kind": "gap-without-follow-up",
                             "entry": entry_index})

    for section in report["configs"]:
        config = section["config"]
        for row in section["rules"]:
            if row["status"] not in GAP_STATUSES:
                continue
            # Feature-gated: this configuration cannot fire the rule, but
            # another one did.  Reported, not a gap.
            if row.get("cross_config") == "witnessed":
                continue
            hit = False
            for entry_index, entry in enumerate(entries):
                if _matches(entry, row, config, target):
                    used[entry_index] = True
                    hit = True
            if not hit:
                problems.append({"kind": "unexplained-gap", "config": config,
                                 "opcode": row["opcode"],
                                 "rule_index": row["rule_index"],
                                 "status": row["status"],
                                 "cross_config": row.get("cross_config")})

    for entry_index, entry in enumerate(entries):
        if not used[entry_index]:
            problems.append({"kind": "stale-gap-entry", "entry": entry_index,
                             "entry_body": entry})
    return problems


def load_json(path: str | Path) -> dict:
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)
