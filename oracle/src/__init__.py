"""Oracle implementation modules.

The oracle does not import gisel at runtime: LLT spelling is local to
`universe.py`, and a test compares it with `gisel.llt.spell` so the two
cannot drift quietly.
"""

from __future__ import annotations
