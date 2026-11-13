"""One entry point for the two modules: ``gisel-anchor <verb>``.

The old console scripts stay: `gisel-import` and `gisel-oracle` are what the
docs and the scripts already call.
"""

from __future__ import annotations

import importlib
import sys

from gisel_anchor import __version__

VERBS = {
    "import": ("gisel_anchor.importer.import_cxx", "main",
               "transcribe pinned LLVM legalizer C++ into .gisel"),
    "oracle": ("gisel_anchor.oracle.__main__", "main",
               "build the U-side question set and coverage"),
}


def _usage() -> str:
    width = max(len(name) for name in VERBS)
    lines = ["  %-*s  %s" % (width, name, VERBS[name][2]) for name in VERBS]
    return (
        "usage: gisel-anchor <verb> [options]\n"
        "       (checkout: PYTHONPATH=src python3 -m gisel_anchor.cli <verb>)\n"
        "\n"
        "verbs (each takes --help; gisel-anchor --version):\n"
        + "\n".join(lines)
        + "\n"
    )


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] in ("-V", "--version"):
        print("gisel-anchor %s" % __version__)
        return 0
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(_usage(), end="")
        return 0 if argv else 2
    entry = VERBS.get(argv[0])
    if entry is None:
        print("gisel-anchor: unknown verb %r\n\n%s" % (argv[0], _usage()),
              file=sys.stderr, end="")
        return 2
    module, name, _ = entry
    return getattr(importlib.import_module(module), name)(argv[1:]) or 0


if __name__ == "__main__":
    sys.exit(main())
