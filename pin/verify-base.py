#!/usr/bin/env python3
"""Hash the files that can change a legalizer answer, and verify a tree.

`pin/make-base.sh` builds the pin plus every declared patch.  This script is
the second half: it computes the sha256 of `official_base.files` (or prints the
legacy pin-source subset when the map is absent) and, with `--verify`, compares
the tree against the map.

    verify-base.py TREE [PIN] [--emit-json|--verify]

Exit codes: 0 = printed / verified; 1 = missing file or content mismatch;
2 = usage or PIN error (including `--verify` without a content map).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

SHARED = (
    "llvm/include/llvm/CodeGen/GlobalISel/LegalizerInfo.h",
    "llvm/lib/CodeGen/GlobalISel/LegalityPredicates.cpp",
    "llvm/lib/CodeGen/GlobalISel/LegalizerInfo.cpp",
)


def fail(message):
    print("verify-base: %s" % message, file=sys.stderr)
    return 2


def load_pin(path):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError) as exc:
        raise SystemExit(fail("cannot read %s: %s" % (path, exc)))


def expected_map(pin):
    """The content map, or the legacy pin-source subset if there is none."""
    base = pin.get("official_base") or {}
    files = base.get("files")
    if files:
        for rel in files:
            if rel.startswith("/") or ".." in Path(rel).parts:
                raise ValueError("official_base.files contains an unsafe path: %r"
                                 % (rel,))
        return dict(files), True
    paths = [value["path"] for value in pin.get("sources", {}).values()]
    paths.extend(SHARED)
    return {rel: None for rel in paths}, False


def sha256(path):
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except FileNotFoundError:
        return None


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="hash or verify the legalizer files of a pin base tree")
    parser.add_argument("tree")
    parser.add_argument("pin", nargs="?", default=str(Path(__file__).with_name("PIN.json")))
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--emit-json", action="store_true",
                      help="print the actual map as JSON")
    mode.add_argument("--verify", action="store_true",
                      help="compare the tree with official_base.files")
    args = parser.parse_args(argv)

    tree = Path(args.tree)
    if not tree.is_dir():
        return fail("%s is not a directory" % tree)
    pin = load_pin(args.pin)
    try:
        expected, has_map = expected_map(pin)
    except ValueError as exc:
        return fail(str(exc))

    if args.verify and not has_map:
        return fail("%s has no official_base.files; cannot verify" % args.pin)

    actual = {rel: sha256(tree / rel) for rel in sorted(expected)}
    if args.emit_json:
        print(json.dumps(actual, indent=2, sort_keys=True))
        return 0

    if args.verify:
        bad = []
        for rel in sorted(expected):
            want, got = expected[rel], actual[rel]
            if got is None:
                bad.append((rel, want, "missing"))
            elif got != want:
                bad.append((rel, want, got))
        if bad:
            for rel, want, got in bad:
                print("mismatch %s: expected %s got %s" % (rel, want, got),
                      file=sys.stderr)
            print("verify-base: %d of %d file(s) do not match"
                  % (len(bad), len(expected)), file=sys.stderr)
            return 1
        print("verify-base: verified %d file(s)" % len(expected))
        return 0

    for rel in sorted(actual):
        got = actual[rel]
        print("  %s  %s" % ((got or "MISSING")[:16], rel))
    if has_map:
        print("This is the pin-only cross-check tree; the A/B baseline is the "
              "frozen official side in PIN.json `official_base`.  Run with "
              "--verify to compare.")
    else:
        print("This is the pin-only cross-check tree.  PIN.json has no "
              "official_base.files map, so there is nothing to verify against.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
