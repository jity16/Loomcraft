"""Source-tree compatibility shim.

The publishable package lives in ``packages/core/src/loomcraft``.  Keeping this
shim lets older checkouts that put ``core`` on ``PYTHONPATH`` resolve the same
canonical modules without maintaining a second implementation.
"""

from __future__ import annotations

from pathlib import Path

_canonical_root = Path(__file__).resolve().parents[2] / "packages" / "core" / "src" / "loomcraft"
if not _canonical_root.is_dir():  # pragma: no cover - only possible in a partial checkout
    raise ImportError("canonical Loomcraft package is missing")

# Relative imports in the canonical __init__ resolve against this package name,
# but all submodules are loaded from the one source of truth.
__path__[:] = [str(_canonical_root)]
_canonical_init = _canonical_root / "__init__.py"
exec(compile(_canonical_init.read_text(encoding="utf-8"), str(_canonical_init), "exec"), globals(), globals())
