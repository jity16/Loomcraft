"""Session state: uploads, plans, executions, artifacts, and source resolution.

A :class:`Session` owns one task from the user's first message to the final
deliverable.  Its directory has four zones with different trust levels:

``uploads/``
    What the user gave you. Read-only to runners, checksummed on arrival.
``artifacts/``
    What execution produced. Written only by the engine, never by the agent
    directly.
``scratch/``
    The agent's own workspace. Anything may land here; nothing here is a
    deliverable until ``register_artifacts`` promotes it — and promotion
    re-validates the path and copies the bytes out.
``control/``
    Server-owned state the agent cannot reach: the event log, the current plan,
    plan history, and execution records.

The zone split is what makes ``source_ref`` safe. A capability input is never a
path — it is ``upload:<id>``, ``artifact:<id>``, or ``scratch:<relative-path>``,
and :meth:`Session.resolve_source` is the only thing that turns one into a real
file, with containment and integrity checks on every call.
"""

from __future__ import annotations

import hashlib
import json
import mimetypes
import os
import re
import secrets
import shutil
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, BinaryIO, Iterable, Mapping

from .errors import ArtifactError, SourceError, SourceIntegrityError
from .events import Event, EventLog, MemoryEventLog

CHUNK = 8 * 1024 * 1024
MAX_ARTIFACT_BATCH = 12
SAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def safe_filename(value: str, fallback: str = "file") -> str:
    """Reduce an arbitrary name to something safe to place on disk."""
    cleaned = SAFE_NAME.sub("_", (value or "").strip()).strip("._-")
    return cleaned[:180] or fallback


def _read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, UnicodeDecodeError, json.JSONDecodeError):
        return default


def _atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def _contained(root: Path, candidate: Path) -> Path:
    """Resolve ``candidate`` and prove it stays inside ``root``.

    Resolution follows symlinks first, so a link planted in ``scratch/`` pointing
    at ``/etc/passwd`` fails here rather than being read.
    """
    root_resolved = root.resolve()
    resolved = candidate.resolve()
    if resolved != root_resolved and root_resolved not in resolved.parents:
        raise SourceError("path escapes its session directory")
    return resolved


def _digest(path: Path) -> tuple[str, int]:
    sha = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            sha.update(chunk)
            size += len(chunk)
    return sha.hexdigest(), size


@dataclass(frozen=True, slots=True)
class ResolvedSource:
    """A source reference proven to exist, be contained, and match its manifest."""

    source_ref: str
    kind: str
    path: Path
    filename: str
    size: int
    checksum: str
    content_type: str


class Session:
    """One task workspace with server-owned state and agent-visible zones."""

    def __init__(
        self,
        session_id: str,
        root: str | Path,
        *,
        event_log: EventLog | None = None,
        max_upload_bytes: int = 2 * 1024**3,
        max_session_bytes: int = 8 * 1024**3,
    ) -> None:
        self.id = session_id
        self.root = Path(root)
        self.max_upload_bytes = max_upload_bytes
        self.max_session_bytes = max_session_bytes
        self._lock = threading.RLock()
        for directory in (self.uploads_dir, self.artifacts_dir, self.scratch_dir, self.control_dir):
            directory.mkdir(parents=True, exist_ok=True)
        self.events = event_log or EventLog(self.control_dir / "events.jsonl")

    # ── Layout ──────────────────────────────────────────────────────────────

    @property
    def uploads_dir(self) -> Path:
        return self.root / "uploads"

    @property
    def artifacts_dir(self) -> Path:
        return self.root / "artifacts"

    @property
    def scratch_dir(self) -> Path:
        return self.root / "scratch"

    @property
    def control_dir(self) -> Path:
        return self.root / "control"

    def run_dir(self, run_id: str) -> Path:
        path = self.artifacts_dir / safe_filename(run_id, "run")
        path.mkdir(parents=True, exist_ok=True)
        return path

    # ── Metadata ────────────────────────────────────────────────────────────

    @property
    def _meta_path(self) -> Path:
        return self.control_dir / "session.json"

    def meta(self) -> dict[str, Any]:
        return _read_json(
            self._meta_path,
            {"session_id": self.id, "created_at": _utcnow(), "status": "idle"},
        )

    def update_meta(self, **fields: Any) -> dict[str, Any]:
        with self._lock:
            meta = self.meta()
            meta.update(fields)
            meta["session_id"] = self.id
            meta["updated_at"] = _utcnow()
            _atomic_json(self._meta_path, meta)
            return meta

    # ── Uploads ─────────────────────────────────────────────────────────────

    @property
    def _uploads_manifest(self) -> Path:
        return self.control_dir / "uploads.json"

    def list_uploads(self) -> list[dict[str, Any]]:
        rows = _read_json(self._uploads_manifest, [])
        return rows if isinstance(rows, list) else []

    def total_upload_bytes(self) -> int:
        return sum(int(row.get("size", 0)) for row in self.list_uploads())

    def save_upload(
        self,
        filename: str,
        data: bytes | BinaryIO,
        *,
        content_type: str | None = None,
    ) -> dict[str, Any]:
        """Stream a user file into ``uploads/``, checksumming as it lands.

        Bytes are hashed during the single write pass rather than by re-reading,
        so a multi-gigabyte upload never has to be held in memory or read twice.
        """
        with self._lock:
            upload_id = f"up-{secrets.token_hex(8)}"
            name = safe_filename(filename, "upload")
            target_dir = self.uploads_dir / upload_id
            target_dir.mkdir(parents=True, exist_ok=True)
            target = target_dir / name

            sha = hashlib.sha256()
            size = 0
            try:
                with target.open("wb") as handle:
                    if isinstance(data, (bytes, bytearray, memoryview)):
                        chunks: Iterable[bytes] = (bytes(data),)
                    else:
                        chunks = iter(lambda: data.read(CHUNK), b"")
                    for chunk in chunks:
                        if not chunk:
                            continue
                        size += len(chunk)
                        if size > self.max_upload_bytes:
                            raise SourceError(
                                f"upload exceeds the {self.max_upload_bytes} byte limit"
                            )
                        sha.update(chunk)
                        handle.write(chunk)
                if size == 0:
                    raise SourceError("refusing to store an empty upload")
                if self.total_upload_bytes() + size > self.max_session_bytes:
                    raise SourceError("session upload quota exceeded")
            except BaseException:
                shutil.rmtree(target_dir, ignore_errors=True)
                raise

            record = {
                "id": upload_id,
                "filename": name,
                "size": size,
                "checksum": sha.hexdigest(),
                "content_type": content_type
                or mimetypes.guess_type(name)[0]
                or "application/octet-stream",
                "created_at": _utcnow(),
                "source_ref": f"upload:{upload_id}",
            }
            manifest = self.list_uploads()
            manifest.append(record)
            _atomic_json(self._uploads_manifest, manifest)
            return record

    def delete_upload(self, upload_id: str) -> dict[str, Any] | None:
        with self._lock:
            manifest = self.list_uploads()
            row = next((item for item in manifest if item.get("id") == upload_id), None)
            if row is None:
                return None
            shutil.rmtree(self.uploads_dir / safe_filename(upload_id), ignore_errors=True)
            _atomic_json(
                self._uploads_manifest,
                [item for item in manifest if item.get("id") != upload_id],
            )
            return row

    # ── Plans ───────────────────────────────────────────────────────────────

    @property
    def _plan_path(self) -> Path:
        return self.control_dir / "plan.json"

    @property
    def _plan_history_path(self) -> Path:
        return self.control_dir / "plan-history.json"

    def current_plan(self) -> dict[str, Any] | None:
        plan = _read_json(self._plan_path, None)
        return plan if isinstance(plan, dict) else None

    def plan_history(self) -> list[dict[str, Any]]:
        rows = _read_json(self._plan_history_path, [])
        return rows if isinstance(rows, list) else []

    def publish_plan(self, plan: Mapping[str, Any]) -> dict[str, Any]:
        """Persist a new revision, retaining the previous one for audit."""
        with self._lock:
            payload = dict(plan)
            history = self.plan_history()
            history = [
                row for row in history if row.get("revision") != payload.get("revision")
            ]
            history.append(payload)
            history.sort(key=lambda row: int(row.get("revision", 0)))
            _atomic_json(self._plan_history_path, history)
            _atomic_json(self._plan_path, payload)
            return payload

    def update_current_plan(self, plan: Mapping[str, Any]) -> dict[str, Any]:
        """Write execution state back onto the current revision."""
        with self._lock:
            payload = dict(plan)
            _atomic_json(self._plan_path, payload)
            history = self.plan_history()
            replaced = False
            for index, row in enumerate(history):
                if row.get("revision") == payload.get("revision"):
                    history[index] = payload
                    replaced = True
                    break
            if not replaced:
                history.append(payload)
            _atomic_json(self._plan_history_path, history)
            return payload

    # ── Executions ──────────────────────────────────────────────────────────

    @property
    def _executions_path(self) -> Path:
        return self.control_dir / "executions.json"

    def list_executions(self) -> list[dict[str, Any]]:
        rows = _read_json(self._executions_path, [])
        return rows if isinstance(rows, list) else []

    def record_execution(self, execution: Mapping[str, Any]) -> dict[str, Any]:
        with self._lock:
            payload = dict(execution)
            rows = self.list_executions()
            for index, row in enumerate(rows):
                if row.get("id") and row.get("id") == payload.get("id"):
                    rows[index] = {**row, **payload}
                    _atomic_json(self._executions_path, rows)
                    return rows[index]
            rows.append(payload)
            _atomic_json(self._executions_path, rows)
            return payload

    # ── Artifacts ───────────────────────────────────────────────────────────

    @property
    def _artifacts_manifest(self) -> Path:
        return self.control_dir / "artifacts.json"

    def list_artifacts(self) -> list[dict[str, Any]]:
        rows = _read_json(self._artifacts_manifest, [])
        return rows if isinstance(rows, list) else []

    def get_artifact(self, artifact_id: str) -> tuple[dict[str, Any], Path] | None:
        row = next(
            (item for item in self.list_artifacts() if item.get("id") == artifact_id),
            None,
        )
        if row is None:
            return None
        path = self.root / str(row.get("relpath", ""))
        if not path.is_file():
            return None
        return row, path

    def add_artifact(
        self,
        source: str | Path,
        *,
        port_name: str = "output",
        display_name: str | None = None,
        step_id: str | None = None,
        run_id: str | None = None,
        node_id: str | None = None,
        move: bool = False,
    ) -> dict[str, Any]:
        """Copy (or move) a produced file into ``artifacts/`` and register it."""
        with self._lock:
            origin = Path(source)
            if not origin.is_file():
                raise ArtifactError(f"artifact source is not a file: {origin.name}")
            artifact_id = f"art-{secrets.token_hex(8)}"
            folder = self.artifacts_dir / artifact_id
            folder.mkdir(parents=True, exist_ok=True)
            name = safe_filename(display_name or origin.name, "artifact")
            target = folder / name
            if move:
                shutil.move(str(origin), target)
            else:
                shutil.copy2(origin, target)
            checksum, size = _digest(target)
            record = {
                "id": artifact_id,
                "filename": name,
                "display_name": display_name or name,
                "size": size,
                "checksum": checksum,
                "content_type": mimetypes.guess_type(name)[0]
                or "application/octet-stream",
                "relpath": str(target.relative_to(self.root)),
                "port_name": port_name,
                "step_id": step_id,
                "run_id": run_id,
                "node_id": node_id,
                "created_at": _utcnow(),
                "source_ref": f"artifact:{artifact_id}",
            }
            manifest = self.list_artifacts()
            manifest.append(record)
            _atomic_json(self._artifacts_manifest, manifest)
            return record

    def register_scratch_artifacts(
        self,
        entries: Iterable[Mapping[str, Any]],
        *,
        step_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """Promote 1..12 agent-written scratch files into session deliverables.

        This is the only path from the agent's own workspace to a downloadable
        result, and it is atomic: every path is validated first, so a batch with
        one bad entry registers nothing.
        """
        rows = [dict(entry) for entry in entries]
        if not 1 <= len(rows) <= MAX_ARTIFACT_BATCH:
            raise ArtifactError(
                f"register between 1 and {MAX_ARTIFACT_BATCH} artifacts per call"
            )
        staged: list[tuple[Path, str | None]] = []
        for entry in rows:
            raw = str(entry.get("path") or "").strip()
            if not raw:
                raise ArtifactError("artifact entry is missing a path")
            relative = raw[len("scratch/") :] if raw.startswith("scratch/") else raw
            if relative.startswith("/") or ".." in Path(relative).parts:
                raise ArtifactError(f"invalid scratch path {raw!r}")
            resolved = _contained(self.scratch_dir, self.scratch_dir / relative)
            if not resolved.is_file():
                raise ArtifactError(f"scratch file not found: {raw}")
            display = entry.get("display_name")
            staged.append((resolved, str(display) if display else None))

        registered: list[dict[str, Any]] = []
        for path, display in staged:
            registered.append(
                self.add_artifact(path, display_name=display, step_id=step_id)
            )
        return registered

    # ── Source resolution ───────────────────────────────────────────────────

    def resolve_source(self, source_ref: str) -> ResolvedSource:
        """Turn ``upload:``/``artifact:``/``scratch:`` into a verified file.

        Every resolution re-checks containment and, for manifest-backed sources,
        re-checks size and SHA-256 against what was recorded at ingest. A file
        swapped underneath the session fails here instead of silently feeding a
        different input into a run.
        """
        if not isinstance(source_ref, str) or ":" not in source_ref:
            raise SourceError(
                "source_ref must be upload:<id>, artifact:<id>, or scratch:<path>"
            )
        kind, _, identifier = source_ref.partition(":")
        identifier = identifier.strip()
        if not identifier:
            raise SourceError(f"empty source reference: {source_ref!r}")

        if kind == "upload":
            row = next(
                (item for item in self.list_uploads() if item.get("id") == identifier),
                None,
            )
            if row is None:
                raise SourceError(f"unknown upload {identifier!r}")
            path = _contained(
                self.uploads_dir,
                self.uploads_dir / identifier / str(row["filename"]),
            )
            return self._verified(source_ref, "upload", path, row)

        if kind == "artifact":
            found = self.get_artifact(identifier)
            if found is None:
                raise SourceError(f"unknown artifact {identifier!r}")
            row, path = found
            return self._verified(
                source_ref, "artifact", _contained(self.artifacts_dir, path), row
            )

        if kind == "scratch":
            if identifier.startswith("/") or ".." in Path(identifier).parts:
                raise SourceError(f"invalid scratch path {identifier!r}")
            path = _contained(self.scratch_dir, self.scratch_dir / identifier)
            if not path.is_file():
                raise SourceError(f"scratch file not found: {identifier}")
            checksum, size = _digest(path)
            return ResolvedSource(
                source_ref=source_ref,
                kind="scratch",
                path=path,
                filename=path.name,
                size=size,
                checksum=checksum,
                content_type=mimetypes.guess_type(path.name)[0]
                or "application/octet-stream",
            )

        raise SourceError(f"unsupported source kind {kind!r}")

    def _verified(
        self,
        source_ref: str,
        kind: str,
        path: Path,
        row: Mapping[str, Any],
    ) -> ResolvedSource:
        if not path.is_file():
            raise SourceError(f"{kind} content is missing for {source_ref}")
        checksum, size = _digest(path)
        if int(row.get("size", size)) != size or str(row.get("checksum", checksum)) != checksum:
            raise SourceIntegrityError(
                f"{kind} content no longer matches its trusted metadata",
                public_message=(
                    "input content no longer matches its recorded checksum; "
                    "provide the file again"
                ),
            )
        return ResolvedSource(
            source_ref=source_ref,
            kind=kind,
            path=path,
            filename=str(row.get("filename", path.name)),
            size=size,
            checksum=checksum,
            content_type=str(row.get("content_type", "application/octet-stream")),
        )

    # ── History for the UI ──────────────────────────────────────────────────

    def history(self, *, after_seq: int = 0) -> dict[str, Any]:
        """Everything the renderer needs to rebuild state after a reload."""
        return {
            "session": self.meta(),
            "current_plan": self.current_plan(),
            "plans": self.plan_history(),
            "events": [event.to_dict() for event in self.events.read(after_seq=after_seq)],
            "uploads": self.list_uploads(),
            "executions": self.list_executions(),
            "artifacts": self.list_artifacts(),
        }

    def emit(self, event: str, data: Mapping[str, Any] | None = None) -> Event:
        return self.events.append(event, data)

    def delete(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)


class SessionStore:
    """Creates and looks up sessions under one root directory."""

    def __init__(
        self,
        root: str | Path,
        *,
        in_memory_events: bool = False,
        max_sessions: int = 512,
    ) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.in_memory_events = in_memory_events
        self.max_sessions = max_sessions
        self._lock = threading.RLock()
        self._cache: dict[str, Session] = {}

    def create(self, session_id: str | None = None) -> Session:
        with self._lock:
            if len(self.list_ids()) >= self.max_sessions:
                raise SourceError("session limit reached for this store")
            sid = session_id or f"lc-{secrets.token_hex(8)}"
            session = self._build(sid)
            session.update_meta(created_at=_utcnow(), status="idle")
            return session

    def get(self, session_id: str) -> Session | None:
        with self._lock:
            if session_id in self._cache:
                return self._cache[session_id]
            path = self.root / safe_filename(session_id)
            if not path.is_dir():
                return None
            return self._build(session_id)

    def get_or_create(self, session_id: str) -> Session:
        return self.get(session_id) or self.create(session_id)

    def list_ids(self) -> list[str]:
        return sorted(item.name for item in self.root.iterdir() if item.is_dir())

    def delete(self, session_id: str) -> bool:
        with self._lock:
            session = self.get(session_id)
            if session is None:
                return False
            session.delete()
            self._cache.pop(session_id, None)
            return True

    def _build(self, session_id: str) -> Session:
        path = self.root / safe_filename(session_id)
        log = MemoryEventLog() if self.in_memory_events else None
        session = Session(session_id, path, event_log=log)
        self._cache[session_id] = session
        return session


__all__ = [
    "MAX_ARTIFACT_BATCH",
    "ResolvedSource",
    "Session",
    "SessionStore",
    "safe_filename",
]
