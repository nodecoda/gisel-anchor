"""Point the importer tests at the gisel checkout they validate against.

The dependency is one-way: gisel-anchor -> gisel.  A test that runs
`python3 -m gisel` needs the package importable, so this file puts
`$GISEL_SRC` (default: the sibling `../gisel` checkout) on `sys.path` and on
the `PYTHONPATH` inherited by subprocesses.
"""

import os
import sys
from pathlib import Path

GISEL_SRC = Path(os.environ.get(
    "GISEL_SRC", Path(__file__).resolve().parent.parent / "gisel")).resolve()
os.environ.setdefault("GISEL_SRC", str(GISEL_SRC))

parts = [p for p in os.environ.get("PYTHONPATH", "").split(os.pathsep) if p]
if str(GISEL_SRC) not in parts:
    os.environ["PYTHONPATH"] = os.pathsep.join([str(GISEL_SRC), *parts])
if str(GISEL_SRC) not in sys.path:
    sys.path.insert(0, str(GISEL_SRC))
