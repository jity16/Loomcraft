"""Write the machine-readable contracts to ``packages/core/schema/``.

The schemas are generated rather than hand-written so they cannot drift from
the validators that actually run. ``--check`` verifies the committed files are
current; CI uses it to fail a change that alters the contract without
regenerating.

    python tools/export_schema.py          # write
    python tools/export_schema.py --check  # verify
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "packages" / "core" / "src"))

from loomcraft import __version__  # noqa: E402
from loomcraft.events import EVENT_TYPES  # noqa: E402
from loomcraft.tools import PLAN_SCHEMA, tool_specs  # noqa: E402

SCHEMA_DIR = ROOT / "packages" / "core" / "schema"
DIALECT = "https://json-schema.org/draft/2020-12/schema"


def plan_schema() -> dict:
    return {
        "$schema": DIALECT,
        "$id": "https://github.com/jity16/Loomcraft/schema/plan.schema.json",
        "title": "LoomCraft plan",
        "description": (
            "The versioned task DAG an agent publishes through publish_plan. "
            f"Generated from loomcraft {__version__}; do not edit by hand."
        ),
        **PLAN_SCHEMA,
    }


def event_schema() -> dict:
    return {
        "$schema": DIALECT,
        "$id": "https://github.com/jity16/Loomcraft/schema/event.schema.json",
        "title": "LoomCraft event",
        "description": (
            "One record from a session's append-only log. Generated from "
            f"loomcraft {__version__}; do not edit by hand."
        ),
        "type": "object",
        "properties": {
            "seq": {
                "type": "integer",
                "minimum": 1,
                "description": "Total order within the session; use it to resume.",
            },
            "event": {"type": "string", "enum": list(EVENT_TYPES)},
            "data": {"type": "object"},
            "ts": {"type": "string", "format": "date-time"},
        },
        "required": ["seq", "event", "data", "ts"],
        "additionalProperties": False,
    }


def tools_schema() -> dict:
    return {
        "$schema": DIALECT,
        "$id": "https://github.com/jity16/Loomcraft/schema/tools.schema.json",
        "title": "LoomCraft agent tools",
        "description": (
            "The canonical tool surface offered to a model. Generated from "
            f"loomcraft {__version__}; do not edit by hand."
        ),
        "type": "array",
        "items": {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "description": {"type": "string"},
                "parameters": {"type": "object"},
            },
            "required": ["name", "description", "parameters"],
        },
        "examples": [
            [
                {
                    "name": spec.name,
                    "description": spec.description,
                    "parameters": spec.parameters,
                }
                for spec in tool_specs()
            ]
        ],
    }


FILES = {
    "plan.schema.json": plan_schema,
    "event.schema.json": event_schema,
    "tools.schema.json": tools_schema,
}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check", action="store_true", help="verify without writing"
    )
    args = parser.parse_args()

    SCHEMA_DIR.mkdir(parents=True, exist_ok=True)
    stale: list[str] = []
    for name, build in FILES.items():
        path = SCHEMA_DIR / name
        rendered = json.dumps(build(), indent=2, ensure_ascii=False) + "\n"
        if args.check:
            current = path.read_text(encoding="utf-8") if path.exists() else ""
            if current != rendered:
                stale.append(name)
        else:
            path.write_text(rendered, encoding="utf-8")

    if args.check and stale:
        print(
            "schema files are out of date: "
            + ", ".join(stale)
            + "\nrun: python tools/export_schema.py",
            file=sys.stderr,
        )
        return 1
    print("schemas are current" if args.check else f"wrote {len(FILES)} schema files")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
