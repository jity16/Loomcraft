"""Optional FastAPI embedding sketch (requires ``pip install loomcraft[server]``)."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "core"))

try:
    from fastapi import FastAPI
except ImportError as exc:  # pragma: no cover
    raise SystemExit("Install loomcraft[server] to run this example") from exc

from loomcraft import LoomcraftRuntime, Registry, create_fastapi_router  # noqa: E402

registry = Registry()
runtime = LoomcraftRuntime(registry)
router = create_fastapi_router(runtime)

app = FastAPI(title="Loomcraft example")
app.include_router(router)
