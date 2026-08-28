"""Check that documentation links resolve and code samples still import.

Two failure modes this catches, both of which make docs actively misleading:
a link to a file that was renamed, and a symbol in an example that no longer
exists in the package.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "packages" / "core" / "src"))

SKIP_DIRS = {
    "node_modules",
    "dist",
    ".git",
    ".venv",
    "venv",
    ".pytest_cache",
    "__pycache__",
    ".ruff_cache",
    ".mypy_cache",
}
SKIP_FILES = {
    # Local working notes are deliberately ignored by the repository.  The
    # checker must make the same distinction, otherwise a stale note can make
    # a clean checkout pass while a developer workspace fails.
    "extraction-notes.md",
    "integration-comparison.md",
    "migration.md",
    "TODO.md",
}
SKIP_PATH_PARTS = {"internal", "_internal", "plans", "notes", "scratch"}

LINK = re.compile(r"\[[^\]]*\]\(([^)]+)\)")
IMPORT_FROM = re.compile(r"^from (loomcraft(?:\.\w+)*) import ([^\n#]+)", re.MULTILINE)


def markdown_files() -> list[Path]:
    return [
        path
        for path in ROOT.rglob("*.md")
        if path.name not in SKIP_FILES
        and not any(part in SKIP_DIRS for part in path.parts)
        and not any(part in SKIP_PATH_PARTS for part in path.parts)
    ]


def check_links(errors: list[str]) -> None:
    for path in markdown_files():
        text = path.read_text(encoding="utf-8")
        for target in LINK.findall(text):
            target = target.split("#", 1)[0].strip()
            if not target or target.startswith(("http://", "https://", "mailto:")):
                continue
            if not (path.parent / target).exists():
                errors.append(f"{path.relative_to(ROOT)}: broken link -> {target}")


def check_documented_symbols(errors: list[str]) -> None:
    """Every ``from loomcraft… import X`` in the docs must actually resolve."""
    import importlib

    cache: dict[str, object | None] = {}

    for path in markdown_files():
        text = path.read_text(encoding="utf-8")
        for module_name, names in IMPORT_FROM.findall(text):
            if "*" in names:
                continue
            if module_name not in cache:
                try:
                    cache[module_name] = importlib.import_module(module_name)
                except ImportError:
                    cache[module_name] = None
            module = cache[module_name]
            if module is None:
                # An optional extra (FastAPI, anthropic) may be absent here;
                # that is a deployment choice, not a documentation error.
                continue
            for raw in names.split(","):
                name = raw.strip().split(" as ")[0].strip().strip("()")
                if not name or not name.isidentifier():
                    continue
                if not hasattr(module, name):
                    errors.append(
                        f"{path.relative_to(ROOT)}: {module_name} has no {name!r}"
                    )


def main() -> int:
    errors: list[str] = []
    check_links(errors)
    check_documented_symbols(errors)
    if errors:
        for error in sorted(set(errors)):
            print(error, file=sys.stderr)
        return 1
    print("documentation links and symbols check out")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
