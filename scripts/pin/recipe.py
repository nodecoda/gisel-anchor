#!/usr/bin/env python3
"""Emit the deviation patch paths make-base applies, in order.

    recipe.py PIN HERE

`PIN.json` names each deviation twice: the fork commit it came from and the
patch file that reproduces it.  The digest is checked here, before anything
is applied, so a truncated or regenerated patch stops the build instead of
quietly producing a different official base.
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path


def deviation_patches(pin_path, here):
    pin = json.loads(Path(pin_path).read_text())
    out = []
    for dev in pin["official_base"]["deviations"]:
        rel = dev.get("patch")
        want = dev.get("patch_sha256")
        if not rel or not want:
            raise ValueError("%s: deviation has no patch/patch_sha256"
                             % dev.get("path", "?"))
        path = Path(here) / rel
        if not path.is_file():
            raise ValueError("%s: missing deviation patch %s" % (dev["path"], path))
        got = hashlib.sha256(path.read_bytes()).hexdigest()
        if got != want:
            raise ValueError("%s: sha256 %s != %s" % (path, got, want))
        out.append(path)
    return out


def main(argv):
    if len(argv) != 3:
        print("usage: recipe.py PIN HERE", file=sys.stderr)
        return 2
    try:
        for path in deviation_patches(argv[1], argv[2]):
            print(path)
    except (OSError, ValueError) as exc:
        print("pin: %s" % exc, file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
