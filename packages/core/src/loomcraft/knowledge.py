"""Small version-pinned knowledge provider useful for demos and tests."""

from __future__ import annotations

import hashlib
import posixpath
from typing import Any, Dict, List, Mapping, Optional


class KnowledgePathError(ValueError):
    pass


def normalize_path(value: str, allow_empty: bool = True) -> str:
    if not isinstance(value, str):
        raise KnowledgePathError("knowledge path must be a string")
    value = value.replace("\\", "/")
    if value.startswith("/") or "\x00" in value:
        raise KnowledgePathError("knowledge path must be logical and relative")
    normalized = posixpath.normpath(value)
    if normalized == "." and allow_empty:
        return ""
    if normalized == "." or normalized == ".." or normalized.startswith("../"):
        raise KnowledgePathError("knowledge path escapes the snapshot")
    return normalized


class InMemoryKnowledgeProvider:
    """Read-only provider with deterministic search and a pinned version."""

    def __init__(self, resources: Mapping[str, str], version: Optional[str] = None) -> None:
        self.resources = {normalize_path(path, allow_empty=False): str(content) for path, content in resources.items()}
        digest = hashlib.sha256(b"loomcraft.knowledge.v1\0")
        for path, content in sorted(self.resources.items()):
            digest.update(path.encode("utf-8"))
            digest.update(b"\0")
            digest.update(content.encode("utf-8"))
            digest.update(b"\0")
        self.version = version or digest.hexdigest()

    def _rows(self, scope: str = "bundle") -> List[Dict[str, Any]]:
        return [{"path": path, "kind": "text", "size": len(content.encode("utf-8")), "scope": scope} for path, content in sorted(self.resources.items())]

    def list(self, payload: Mapping[str, Any]) -> Dict[str, Any]:
        path = normalize_path(str(payload.get("path", "")))
        limit = max(1, min(int(payload.get("limit", 100)), 100))
        rows = [row for row in self._rows(str(payload.get("scope", "bundle"))) if not path or row["path"] == path or row["path"].startswith(path.rstrip("/") + "/")]
        return {"version": self.version, "path": path, "entries": rows[:limit], "total": len(rows), "next_offset": limit if len(rows) > limit else None}

    def search(self, payload: Mapping[str, Any]) -> Dict[str, Any]:
        query = str(payload.get("query", "")).strip().casefold()
        if not query:
            raise ValueError("knowledge query is required")
        terms = [term for term in query.split() if term]
        results = []
        for path, content in sorted(self.resources.items()):
            for line_number, line in enumerate(content.splitlines(), 1):
                if all(term in line.casefold() or term in path.casefold() for term in terms):
                    results.append({"path": path, "line": line_number, "text": line[:1000], "href": "knowledge:%s" % path})
        limit = max(1, min(int(payload.get("limit", 50)), 100))
        return {"version": self.version, "query": query, "results": results[:limit], "truncated": len(results) > limit}

    def read(self, payload: Mapping[str, Any]) -> Dict[str, Any]:
        path = normalize_path(str(payload.get("path", "")), allow_empty=False)
        if path not in self.resources:
            raise KnowledgePathError("knowledge resource was not found")
        content = self.resources[path]
        offset = max(0, int(payload.get("offset", 0)))
        limit = max(1, min(int(payload.get("limit", 49152)), 49152))
        chunk = content[offset:offset + limit]
        return {"version": self.version, "path": path, "content": chunk, "offset": offset, "next_offset": offset + len(chunk) if offset + len(chunk) < len(content) else None, "size": len(content), "done": offset + len(chunk) >= len(content), "media_type": "text/plain"}
