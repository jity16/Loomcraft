"""Optional safe local artifact staging.

The engine's public contract only requires artifact metadata.  This adapter is
provided for hosts that use a scratch directory and want the same guarded copy
semantics as the extracted application: relative paths only, regular files,
size limits, checksums, and session ownership.
"""

from __future__ import annotations

import hashlib
import mimetypes
import os
import secrets
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _safe_name(value: str) -> str:
    name = value.replace("\\", "/").split("/")[-1] or "artifact"
    cleaned = "".join(char if char.isalnum() or char in ".-_+" else "_" for char in name)
    return cleaned[:200] or "artifact"


class ArtifactStoreError(ValueError):
    pass


MAX_ARTIFACT_BATCH = 12


class LocalArtifactStore:
    """Copy scratch files into a private, session-scoped output directory."""

    def __init__(self, root: os.PathLike, *, max_bytes: int = 32 * 1024 * 1024, max_batch: int = MAX_ARTIFACT_BATCH, url_builder: Any = None) -> None:
        self.root = Path(root).resolve()
        self.max_bytes = max(1, int(max_bytes))
        self.max_batch = max(1, int(max_batch))
        self.url_builder = url_builder

    def scratch_dir(self, session_id: str) -> Path:
        self._validate_session(session_id)
        session_root = self.root / session_id
        if session_root.is_symlink() or (session_root.exists() and not session_root.is_dir()):
            raise ArtifactStoreError("session root is invalid")
        session_root.mkdir(parents=True, exist_ok=True)
        try:
            session_root.chmod(0o700)
        except OSError:
            pass
        path = session_root / "scratch"
        if path.is_symlink() or (path.exists() and not path.is_dir()):
            raise ArtifactStoreError("scratch root is invalid")
        path.mkdir(parents=True, exist_ok=True)
        return path

    def output_dir(self, session_id: str) -> Path:
        self._validate_session(session_id)
        session_root = self.root / session_id
        if session_root.is_symlink() or (session_root.exists() and not session_root.is_dir()):
            raise ArtifactStoreError("session root is invalid")
        session_root.mkdir(parents=True, exist_ok=True)
        path = session_root / "outputs"
        if path.is_symlink() or (path.exists() and not path.is_dir()):
            raise ArtifactStoreError("output root is invalid")
        path.mkdir(parents=True, exist_ok=True)
        return path

    def register_scratch(self, session_id: str, relative_path: str, *, step_id: Optional[str] = None, display_name: Optional[str] = None) -> Dict[str, Any]:
        if not isinstance(relative_path, str) or not relative_path.strip():
            raise ArtifactStoreError("artifact path must be a non-empty relative string")
        relative = relative_path.strip().replace("\\", "/")
        while relative.startswith("./"):
            relative = relative[2:]
        if relative.startswith("scratch/"):
            relative = relative[8:]
        if not relative or relative.startswith("/") or any(part in {"", ".", ".."} for part in relative.split("/")):
            raise ArtifactStoreError("artifact path must stay inside scratch")
        scratch = self.scratch_dir(session_id).resolve()
        candidate = scratch / relative
        if candidate.is_symlink():
            raise ArtifactStoreError("artifact source must not be a symlink")
        try:
            source = candidate.resolve(strict=True)
        except (FileNotFoundError, OSError) as exc:
            raise ArtifactStoreError("artifact source does not exist inside scratch") from exc
        try:
            source.relative_to(scratch)
        except ValueError as exc:
            raise ArtifactStoreError("artifact path must stay inside scratch") from exc
        if source.is_symlink() or not source.is_file():
            raise ArtifactStoreError("artifact source must be a regular file")
        size = source.stat().st_size
        if size > self.max_bytes:
            raise ArtifactStoreError("artifact exceeds the size limit")
        artifact_id = "art-%s" % secrets.token_hex(8)
        filename = _safe_name(source.name)
        target_dir = self.output_dir(session_id) / artifact_id
        target_dir.mkdir(parents=True, exist_ok=False)
        target = target_dir / filename
        try:
            shutil.copyfile(source, target)
            target.chmod(0o600)
            data = target.read_bytes()
            if len(data) != size:
                raise ArtifactStoreError("artifact changed during registration")
        except BaseException:
            shutil.rmtree(target_dir, ignore_errors=True)
            raise
        row = {
            "id": artifact_id,
            "source_ref": "artifact:%s" % artifact_id,
            "filename": filename,
            "display_name": (display_name or filename)[:200],
            "size": size,
            "checksum": hashlib.sha256(data).hexdigest(),
            "content_type": mimetypes.guess_type(filename)[0] or "application/octet-stream",
            "step_id": step_id,
            "created_at": _now(),
            "path": str(Path(session_id) / "outputs" / artifact_id / filename),
        }
        if callable(self.url_builder):
            row["download_url"] = str(self.url_builder(session_id, artifact_id, filename))
        return row

    def register_batch(self, session_id: str, items: List[Mapping[str, Any]], *, step_id: Optional[str] = None) -> List[Dict[str, Any]]:
        if not 1 <= len(items) <= self.max_batch:
            raise ArtifactStoreError("artifact batch must contain 1..%d files" % self.max_batch)
        prepared: List[Dict[str, Any]] = []
        seen = set()
        try:
            for item in items:
                if not isinstance(item, Mapping):
                    raise ArtifactStoreError("artifact entries must be objects")
                path = item.get("path")
                if not isinstance(path, str):
                    raise ArtifactStoreError("artifact paths must be distinct non-empty strings")
                path = path.replace("\\", "/")
                if path in seen:
                    raise ArtifactStoreError("artifact paths must be distinct non-empty strings")
                seen.add(path)
                prepared.append(self.register_scratch(session_id, path, step_id=step_id, display_name=item.get("display_name")))
            return prepared
        except BaseException:
            for row in prepared:
                artifact_id = row.get("id")
                if isinstance(artifact_id, str):
                    shutil.rmtree(self.output_dir(session_id) / artifact_id, ignore_errors=True)
            raise

    def artifact_path(self, session_id: str, artifact_id: str, filename: Optional[str] = None) -> Path:
        self._validate_session(session_id)
        if not artifact_id or any(char not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-" for char in artifact_id):
            raise ArtifactStoreError("invalid artifact id")
        root = self.output_dir(session_id).resolve()
        target_dir = root / artifact_id
        if target_dir.is_symlink() or not target_dir.is_dir():
            raise ArtifactStoreError("artifact is unavailable")
        files = [item for item in target_dir.iterdir() if item.is_file() and not item.is_symlink()]
        if filename is not None:
            candidate = target_dir / _safe_name(filename)
            files = [item for item in files if item == candidate]
        if len(files) != 1:
            raise ArtifactStoreError("artifact file is unavailable")
        return files[0]

    def verify(self, session_id: str, artifact: Mapping[str, Any]) -> Path:
        artifact_id = artifact.get("id")
        if not isinstance(artifact_id, str):
            raise ArtifactStoreError("artifact id is required")
        target = self.artifact_path(session_id, artifact_id, artifact.get("filename") if isinstance(artifact.get("filename"), str) else None)
        expected = artifact.get("checksum")
        if expected:
            digest_obj = hashlib.sha256()
            with target.open("rb") as handle:
                for chunk in iter(lambda: handle.read(8 * 1024 * 1024), b""):
                    digest_obj.update(chunk)
            if digest_obj.hexdigest().lower() != str(expected).lower():
                raise ArtifactStoreError("artifact content failed integrity validation")
        return target

    def list_artifacts(self, session_id: str) -> List[Dict[str, Any]]:
        """Return metadata discoverable from the local output tree."""
        root = self.output_dir(session_id)
        rows: List[Dict[str, Any]] = []
        for directory in sorted(root.iterdir(), key=lambda item: item.name) if root.exists() else []:
            if directory.is_symlink() or not directory.is_dir():
                continue
            files = [item for item in directory.iterdir() if item.is_file() and not item.is_symlink()]
            if len(files) != 1:
                continue
            target = files[0]
            digest_obj = hashlib.sha256()
            with target.open("rb") as handle:
                for chunk in iter(lambda: handle.read(8 * 1024 * 1024), b""):
                    digest_obj.update(chunk)
            rows.append({"id": directory.name, "source_ref": "artifact:%s" % directory.name, "filename": target.name, "size": target.stat().st_size, "checksum": digest_obj.hexdigest(), "content_type": mimetypes.guess_type(target.name)[0] or "application/octet-stream", "path": str(Path(session_id) / "outputs" / directory.name / target.name)})
        return rows

    @staticmethod
    def _validate_session(session_id: str) -> None:
        if not session_id or any(char not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-" for char in session_id):
            raise ArtifactStoreError("invalid session id")
