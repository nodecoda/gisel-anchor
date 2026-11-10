"""Oracle implementation modules.

The only external dependency is the gisel package, used for LLT spelling so
that U keys and G keys compare byte for byte.  If gisel is not installed, fall
back to the sibling checkout (`$GISEL_SRC`, default `../../gisel`).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

try:  # pragma: no cover - exercised only in a fresh checkout
    import gisel  # noqa: F401
except ImportError:  # pragma: no cover
    _default = Path(__file__).resolve().parents[3] / "gisel"
    _src = Path(os.environ.get("GISEL_SRC", _default)).resolve()
    if str(_src) not in sys.path:
        sys.path.insert(0, str(_src))
