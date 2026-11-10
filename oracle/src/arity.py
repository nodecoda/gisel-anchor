"""Load opcode slot counts from the upstream `--dump-arity` output.

The universe must not read `gisel/data/opcode-slots.json`: that snapshot is
frozen at an older LLVM.  Arity comes from the pinned build instead.
"""

from __future__ import annotations

import json
from pathlib import Path


def load_arity(path: str | Path) -> dict[str, tuple[int, int]]:
    """Return {opcode: (type_slots, imm_slots)}, max across configurations."""
    out: dict[str, tuple[int, int]] = {}
    with open(path, "r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            opcode = row["opcode"]
            slots = (int(row["type_slots"]), int(row["imm_slots"]))
            old = out.get(opcode, (0, 0))
            out[opcode] = (max(old[0], slots[0]), max(old[1], slots[1]))
    return out
