"""Small diagnostics CLI: ``python -m loomcraft --version``."""

from __future__ import annotations

import argparse
import json

from . import __version__, dynamic_tool_specs


def main() -> int:
    parser = argparse.ArgumentParser(description="Loomcraft diagnostics")
    parser.add_argument("--version", action="store_true", help="print the package version")
    parser.add_argument("--tools", action="store_true", help="print native tool names as JSON")
    args = parser.parse_args()
    if args.tools:
        print(json.dumps([item["name"] for item in dynamic_tool_specs()]))
    else:
        print(__version__ if args.version or not args.tools else "")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
