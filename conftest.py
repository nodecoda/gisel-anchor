"""Test-session bootstrap.

Two things have to be true before a test can run:

* `gisel_anchor` is importable from the checkout (`src/` layout, no install),
* `$GISEL_SRC` points at the sibling `gisel` checkout, because the importer
  tests drive `python3 -m gisel` in a subprocess; `$GISEL_CORPUS` defaults to
  that checkout's corpus so an unpacked sdist can run its own tests.

The dependency is one-way: gisel-anchor -> gisel.  Nothing here writes to the
gisel checkout.
"""

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SRC = ROOT / "src"
GISEL_SRC = Path(os.environ.get(
    "GISEL_SRC", ROOT.parent / "gisel")).resolve()
os.environ.setdefault("GISEL_SRC", str(GISEL_SRC))
os.environ.setdefault("GISEL_CORPUS", str(GISEL_SRC / "tests" / "corpus"))

parts = [p for p in os.environ.get("PYTHONPATH", "").split(os.pathsep) if p]
for entry in (str(GISEL_SRC), str(SRC)):
    if entry not in parts:
        parts.insert(0, entry)
os.environ["PYTHONPATH"] = os.pathsep.join(parts)

for entry in (str(GISEL_SRC), str(SRC)):
    if entry not in sys.path:
        sys.path.insert(0, entry)
