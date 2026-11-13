"""Expand a rule-independent universe across the configurations under test.

`universe` asks each shape once; an answer depends on the subtarget, so the
runners need one row per (config, query).  The limited pipeline gets that from
`emit` (one witness per rule plus a sample).  The full pipeline has no witness
pass -- it wants every shape in every configuration -- so it expands the
universe directly.  Keeping this in the oracle (not in an ad-hoc snippet) means
the full U file has one definition, the same one `gen-u.sh --full` uses.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Iterable, Iterator


def build(rows: Iterable[dict], configs: Iterable[str]) -> Iterator[dict]:
    """Yield each universe row once per configuration, in config-major order."""
    materialised = [row for row in rows]
    for config in configs:
        for row in materialised:
            expanded = dict(row)
            expanded["config"] = config
            yield expanded


def load(path: str | Path) -> list[dict]:
    with open(path, "r", encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def write(path: str | Path, rows: Iterable[dict]) -> int:
    count = 0
    with open(path, "w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row) + "\n")
            count += 1
    return count
