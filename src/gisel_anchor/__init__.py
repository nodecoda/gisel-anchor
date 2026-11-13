"""Pinned LLVM GlobalISel facts: rule import and the U-side oracle.

Two console scripts are the product (`gisel-import`, `gisel-oracle`); the
package itself is not an import surface anyone should depend on.  Both
subpackages are deliberately named after what they do, not after a target.
"""

__all__ = ["importer", "oracle"]
