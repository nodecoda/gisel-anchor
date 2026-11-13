"""`gisel-import`: run the one-shot transcriber without knowing its path.

The transcriber stays a script because it is a one-off: it hardcodes the
quirks of nine constructors at one pin, and a fourth target is a new tool,
not a flag.  This shim exists so an installed copy can be invoked as
`gisel-import ...`; it must not grow a second set of rules.
"""

from __future__ import annotations

import runpy
import sys
from pathlib import Path


def main(argv=None) -> int:
    script = Path(__file__).with_name("import-cxx.py")
    saved = sys.argv
    sys.argv = [str(script), *(saved[1:] if argv is None else argv)]
    try:
        runpy.run_path(str(script), run_name="__main__")
    finally:
        sys.argv = saved
    return 0
