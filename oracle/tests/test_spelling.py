"""The local LLT spelling must stay identical to gisel's."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from oracle.src import universe

GISEL = Path(os.environ.get("GISEL_SRC") or Path(__file__).resolve().parents[2] / "gisel")
sys.path.insert(0, str(GISEL))
try:
    from gisel.llt import spell as gisel_spell
except ImportError:  # pragma: no cover - only when the sibling checkout is absent
    gisel_spell = None


@pytest.mark.skipif(gisel_spell is None, reason="sibling gisel checkout not present")
def test_every_candidate_type_spells_the_same_way():
    for target in ("x86", "aarch64", "riscv"):
        for value in universe.type_values(target):
            assert universe.spell(value) == gisel_spell(value), (target, value)


def test_edge_shapes_spell_the_same_way():
    cases = [
        ("invalid",),
        ("bf16",),
        ("pointer", 0, "*"),
        ("pointer", 2, 64),
        ("vector", 4, False, ("scalar", 32)),
        ("vector", 2, True, ("scalar", 64)),
        ("integer", 1),
        ("float", 80),
    ]
    for value in cases:
        expected = {
            ("invalid",): "LLT_invalid",
            ("bf16",): "bf16",
            ("pointer", 0, "*"): "p0:*",
            ("pointer", 2, 64): "p2:64",
            ("vector", 4, False, ("scalar", 32)): "v4s32",
            ("vector", 2, True, ("scalar", 64)): "nxv2s64",
            ("integer", 1): "i1",
            ("float", 80): "f80",
        }[value]
        assert universe.spell(value) == expected
        if gisel_spell is not None:
            assert universe.spell(value) == gisel_spell(value)
