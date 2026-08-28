"""Optional session-scoped upload storage with checksum and size guards."""

from __future__ import annotations

import hashlib
import json
import mimetypes
import os
import secrets
import shutil
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, BinaryIO, Dict, List, Optional


class UploadError(ValueError):
    pass


def _safe_filename(value: str) -> str:
    name = value.replace("\\", "/").split("/")[-1] or "upload"
    return "".join(char if char.isalnum() or char in ".-_+" else "_" for char in name)[:200] or "upload"


class LocalUploadStore:
    """Persist uploads in private directories and return allocation-friendly rows."""

    def __init__(self, root: os.PathLike, *, max_file_bytes: int = 2 * 1024 * 1024 * 1024, max_session_bytes: int = 2 * 1024 * 1024 * 1024) -> None:
        self.root = Path(root).resolve()
        self.max_file_bytes = max(1, int(max_file_bytes))
        self.max_session_bytes = max(1, int(max_session_bytes))
        self._lock = threading.RLock()

    def save(self, session_id: str, filename: str, data: bytes, content_type: Optional[str] = None) -> Dict[str, Any]:
        import io
        return self.save_stream(session_id, filename, io.BytesIO(data), content_type)

    def save_stream(self, session_id: str, filename: str, source: BinaryIO, content_type: Optional[str] = None, chunk_size: int = 8 * 1024 * 1024) -> Dict[str, Any]:
        self._validate_session(session_id)
        if not isinstance(filename, str) or not filename.strip():
            raise UploadError("filename is required")
        upload_id = "upl-%s" % secrets.token_hex(8)
        safe = _safe_filename(filename)
        session_root = self.root / session_id
        if session_root.is_symlink() or (session_root.exists() and not session_root.is_dir()):
            raise UploadError("session upload root is invalid")
        upload_root = session_root / "uploads"
        if upload_root.is_symlink() or (upload_root.exists() and not upload_root.is_dir()):
            raise UploadError("upload root is invalid")
        target_dir = upload_root / upload_id
        target_dir.mkdir(parents=True, exist_ok=False)
        target = target_dir / safe
        size = 0
        digest = hashlib.sha256()
        try:
            with target.open("xb") as handle:
                while True:
                    chunk = source.read(chunk_size)
                    if not chunk:
                        break
                    if not isinstance(chunk, (bytes, bytearray)):
                        raise UploadError("upload stream must yield bytes")
                    size += len(chunk)
                    if size > self.max_file_bytes:
                        raise UploadError("upload exceeds the file size limit")
                    digest.update(bytes(chunk))
                    handle.write(bytes(chunk))
            if size == 0:
                raise UploadError("upload is empty")
            target.chmod(0o600)
            row = {
                "id": upload_id,
                "filename": safe,
                "size": size,
                "checksum": digest.hexdigest(),
                "content_type": content_type or mimetypes.guess_type(safe)[0] or "application/octet-stream",
                "created_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                "source_ref": "upload:%s" % upload_id,
                "path": str(Path(session_id) / "uploads" / upload_id / safe),
            }
            with self._lock:
                rows = self.list_uploads(session_id)
                used = sum(int(item.get("size", 0)) for item in rows if isinstance(item.get("size"), int))
                if used + size > self.max_session_bytes:
                    raise UploadError("session upload quota exceeded")
                self._write_manifest(session_id, [*rows, row])
            return row
        except BaseException:
            shutil.rmtree(target_dir, ignore_errors=True)
            raise

    def path(self, session_id: str, upload_id: str) -> Path:
        self._validate_session(session_id)
        if not upload_id or any(char not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-" for char in upload_id):
            raise UploadError("invalid upload id")
        root_path = self.root / session_id / "uploads"
        if root_path.is_symlink() or not root_path.is_dir():
            raise UploadError("upload root is unavailable")
        root = root_path.resolve()
        upload_root = root / upload_id
        if upload_root.is_symlink() or not upload_root.is_dir():
            raise UploadError("upload file is unavailable")
        candidates = list(upload_root.glob("*"))
        files = [item for item in candidates if item.is_file() and not item.is_symlink()]
        if len(files) != 1:
            raise UploadError("upload file is unavailable")
        return files[0]

    def delete(self, session_id: str, upload_id: str) -> Optional[Dict[str, Any]]:
        """Delete one manifest-owned upload and roll back metadata on failure."""
        self._validate_session(session_id)
        with self._lock:
            rows = self.list_uploads(session_id)
            row = next((item for item in rows if item.get("id") == upload_id), None)
            if row is None:
                return None
            upload_root = self.root / session_id / "uploads" / upload_id
            if upload_root.is_symlink() or (upload_root.exists() and not upload_root.is_dir()):
                raise UploadError("upload root is invalid")
            remaining = [item for item in rows if item.get("id") != upload_id]
            self._write_manifest(session_id, remaining)
            try:
                if upload_root.exists():
                    shutil.rmtree(upload_root)
            except OSError as exc:
                self._write_manifest(session_id, rows)
                raise UploadError("upload deletion failed") from exc
            return row

    def verify(self, session_id: str, upload_id: str) -> Dict[str, Any]:
        """Re-hash an upload before execution and fail on content drift."""
        row = next((item for item in self.list_uploads(session_id) if item.get("id") == upload_id), None)
        if row is None:
            raise UploadError("upload not found")
        target = self.path(session_id, upload_id)
        digest_obj = hashlib.sha256()
        with target.open("rb") as handle:
            while True:
                chunk = handle.read(8 * 1024 * 1024)
                if not chunk:
                    break
                digest_obj.update(chunk)
        digest = digest_obj.hexdigest()
        if row.get("checksum") and digest.lower() != str(row.get("checksum")).lower():
            raise UploadError("upload content failed integrity validation")
        return row

    def list_uploads(self, session_id: str) -> List[Dict[str, Any]]:
        self._validate_session(session_id)
        path = self.root / session_id / "uploads" / "manifest.json"
        if not path.exists():
            return []
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise UploadError("upload manifest is invalid") from exc
        rows = value.get("uploads") if isinstance(value, dict) else None
        return [dict(item) for item in rows if isinstance(item, dict)] if isinstance(rows, list) else []

    def _write_manifest(self, session_id: str, rows: List[Dict[str, Any]]) -> None:
        path = self.root / session_id / "uploads" / "manifest.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps({"uploads": rows}, ensure_ascii=False, indent=2), encoding="utf-8")
        try:
            temporary.chmod(0o600)
        except OSError:
            pass
        os.replace(str(temporary), str(path))

    @staticmethod
    def _validate_session(session_id: str) -> None:
        if not session_id or any(char not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-" for char in session_id):
            raise UploadError("invalid session id")
