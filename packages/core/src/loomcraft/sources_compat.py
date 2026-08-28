"""Session-owned source reference resolution with integrity checks."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Mapping


class SourceResolutionError(ValueError):
    pass


class SourceResolver:
    """Resolve only upload:/artifact: references through injected stores."""

    def __init__(self, *, upload_store: Any = None, artifact_store: Any = None, scratch_root: Any = None) -> None:
        self.upload_store = upload_store
        self.artifact_store = artifact_store
        self.scratch_root = Path(scratch_root).resolve() if scratch_root is not None else None

    def resolve(self, session_id: str, source_ref: str, *, verify: bool = True) -> Dict[str, Any]:
        if not isinstance(source_ref, str) or not source_ref.strip():
            raise SourceResolutionError("source_ref is required")
        value = source_ref.strip()
        if value.startswith("upload:"):
            identifier = value[7:]
            if self.upload_store is None:
                raise SourceResolutionError("upload store is not configured")
            try:
                row = next((item for item in self.upload_store.list_uploads(session_id) if item.get("id") == identifier), None)
                if row is None:
                    raise SourceResolutionError("upload was not found")
                path = self.upload_store.path(session_id, identifier)
                if verify and callable(getattr(self.upload_store, "verify", None)):
                    self.upload_store.verify(session_id, identifier)
            except SourceResolutionError:
                raise
            except Exception as exc:
                raise SourceResolutionError("upload failed integrity or availability checks") from exc
            return {"source_ref": value, "id": identifier, "filename": row.get("filename", path.name), "path": str(path), "checksum": row.get("checksum")}
        if value.startswith("artifact:"):
            identifier = value[9:]
            if self.artifact_store is None:
                raise SourceResolutionError("artifact store is not configured")
            artifacts = getattr(self.artifact_store, "list_artifacts", None)
            row = None
            if callable(artifacts):
                row = next((item for item in artifacts(session_id) if item.get("id") == identifier), None)
            filename = row.get("filename") if isinstance(row, Mapping) else None
            path_method = getattr(self.artifact_store, "artifact_path", None)
            if not callable(path_method):
                raise SourceResolutionError("artifact store cannot resolve files")
            try:
                path = path_method(session_id, identifier, filename)
                if verify and isinstance(row, Mapping) and callable(getattr(self.artifact_store, "verify", None)):
                    self.artifact_store.verify(session_id, row)
            except Exception as exc:
                raise SourceResolutionError("artifact failed integrity or availability checks") from exc
            return {"source_ref": value, "id": identifier, "filename": filename or Path(path).name, "path": str(path), "checksum": row.get("checksum") if isinstance(row, Mapping) else None}
        if value.startswith("scratch:"):
            if self.scratch_root is None:
                raise SourceResolutionError("scratch root is not configured")
            relative = value[8:]
            if not relative or relative.startswith("/") or ".." in Path(relative).parts:
                raise SourceResolutionError("scratch path is invalid")
            candidate = (self.scratch_root / relative).resolve(strict=True)
            try:
                candidate.relative_to(self.scratch_root)
            except ValueError as exc:
                raise SourceResolutionError("scratch path escapes its root") from exc
            if not candidate.is_file() or candidate.is_symlink():
                raise SourceResolutionError("scratch file is unavailable")
            return {"source_ref": value, "filename": candidate.name, "path": str(candidate)}
        raise SourceResolutionError("source_ref must use an owned upload: or artifact: reference")
