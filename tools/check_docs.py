"""Check local Markdown links and JSON schema syntax."""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    errors = []
    for path in root.rglob("*.md"):
        if any(
            part in {
                "node_modules",
                "dist",
                ".git",
                ".venv",
                ".pytest_cache",
                "__pycache__",
                "_internal",
            }
            or part.endswith(".egg-info")
            for part in path.parts
        ):
            continue
        if path.name in {"extraction-notes.md", "integration-comparison.md", "migration.md"}:
            continue
        text = path.read_text(encoding="utf-8")
        for target in re.findall(r"\[[^\]]+\]\(([^)]+)\)", text):
            target = target.split("#", 1)[0]
            if not target or target.startswith(("http://", "https://", "mailto:")):
                continue
            if not (path.parent / target).exists():
                errors.append("%s: missing %s" % (path.relative_to(root), target))
    schema_roots = [root / "packages" / "core" / "schema", root / "core" / "schema"]
    for schema_root in schema_roots:
        if not schema_root.exists():
            continue
        for path in schema_root.glob("*.json"):
            try:
                json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                errors.append("%s: invalid JSON (%s)" % (path.relative_to(root), exc))
    for file_name in ("plan.schema.json", "event.schema.json", "tools.schema.json"):
        canonical_path = root / "packages" / "core" / "schema" / file_name
        mirror_path = root / "core" / "schema" / file_name
        if canonical_path.exists() and mirror_path.exists():
            try:
                canonical = json.loads(canonical_path.read_text(encoding="utf-8"))
                mirror = json.loads(mirror_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if canonical != mirror:
                errors.append(
                    "core/schema/%s: differs from packages/core/schema/%s"
                    % (file_name, file_name)
                )
    if errors:
        for error in errors:
            print(error, file=sys.stderr)
        return 1
    print("documentation and schema checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
