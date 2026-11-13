#!/usr/bin/env python3
"""Copy the pin into the package tree so an installed wheel can use it.

`pin/PIN.json` stays canonical: `ab` reads it, the docs cite it, and the
corpus provenance names it.  The wheel needs the same bytes under
`importer/data/` so `gisel-import --git-repo ...` has a pin to read without a
checkout.

    tools/sync-package-data.py          # refresh the copy
    tools/sync-package-data.py --check  # rc=1 if the copy drifted
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "pin" / "PIN.json"
TARGET = ROOT / "importer" / "data" / "PIN.json"


def check() -> int:
    if not TARGET.is_file():
        print("missing: %s" % TARGET.relative_to(ROOT), file=sys.stderr)
        return 1
    if TARGET.read_bytes() != SOURCE.read_bytes():
        print("differs: %s" % TARGET.relative_to(ROOT), file=sys.stderr)
        print("run tools/sync-package-data.py", file=sys.stderr)
        return 1
    print("package data: in sync")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--check", action="store_true",
                    help="compare instead of writing; rc=1 on drift")
    args = ap.parse_args(argv)
    if args.check:
        return check()
    TARGET.parent.mkdir(parents=True, exist_ok=True)
    TARGET.write_bytes(SOURCE.read_bytes())
    print("package data: synced")
    return 0


if __name__ == "__main__":
    sys.exit(main())
