"""The one-shot C++ constructor -> `.gisel` transcriber.

`import-cxx.py` is a script, not a module: it is deliberately a one-off tool
whose knowledge is pinned to nine constructors at one commit (see its module
docstring).  `importer.cli` is only the console-script shim that runs it, so
there is exactly one implementation.
"""
