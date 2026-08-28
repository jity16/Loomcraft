"""Pluggable session persistence for Loomcraft.

``InMemoryStore`` is ideal for tests and embedded applications.  ``JsonStore``
provides a dependency-free durable implementation for a single process.  A
production deployment can implement the small ``SessionStore`` protocol with
PostgreSQL, Redis, S3, or an existing application's stores without changing
the engine.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import secrets
import shutil
import threading
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Protocol

from .events import Event, EventLog, EventLogError, MemoryEventLog, utcnow_iso as utc_now


def _copy(value: Any) -> Any:
    return copy.deepcopy(value)


class SessionStore(Protocol):
    def get_current_plan(self, session_id: str) -> Optional[Dict[str, Any]]: ...
    def publish_plan(self, session_id: str, plan: Mapping[str, Any]) -> Dict[str, Any]: ...
    def update_current_plan(self, session_id: str, plan: Mapping[str, Any]) -> Dict[str, Any]: ...
    def append_event(self, session_id: str, event: str, data: Mapping[str, Any]) -> Event: ...
    def read_events(self, session_id: str, after: int = 0) -> List[Event]: ...
    def record_execution(self, session_id: str, execution: Mapping[str, Any]) -> Dict[str, Any]: ...
    def list_executions(self, session_id: str) -> List[Dict[str, Any]]: ...


class InMemoryStore:
    """A deterministic, thread-safe store with event replay."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._sessions: Dict[str, Dict[str, Any]] = {}
        self._events: Dict[str, EventLog] = {}
        self._plans: Dict[str, Optional[Dict[str, Any]]] = {}
        self._history: Dict[str, List[Dict[str, Any]]] = {}
        self._executions: Dict[str, List[Dict[str, Any]]] = {}
        self._artifacts: Dict[str, List[Dict[str, Any]]] = {}
        self._uploads: Dict[str, List[Dict[str, Any]]] = {}
        self._messages: Dict[str, List[Dict[str, Any]]] = {}

    def create_session(self, session_id: Optional[str] = None, **metadata: Any) -> Dict[str, Any]:
        with self._lock:
            sid = session_id or "sess-%s" % secrets.token_hex(8)
            if sid in self._sessions:
                return _copy(self._sessions[sid])
            row = {
                "session_id": sid,
                "status": "idle",
                "turns": 0,
                "created_at": utc_now(),
                "updated_at": utc_now(),
                **_copy(metadata),
            }
            self._sessions[sid] = row
            self._events[sid] = MemoryEventLog()
            self._plans[sid] = None
            self._history[sid] = []
            self._executions[sid] = []
            self._artifacts[sid] = []
            self._uploads[sid] = []
            self._messages[sid] = []
            return _copy(row)

    def ensure_session(self, session_id: str) -> Dict[str, Any]:
        return self.create_session(session_id)

    def get_session(self, session_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self._sessions.get(session_id)
            return _copy(row) if row is not None else None

    def update_session(self, session_id: str, **fields: Any) -> Dict[str, Any]:
        with self._lock:
            self.ensure_session(session_id)
            self._sessions[session_id].update(_copy(fields))
            self._sessions[session_id]["updated_at"] = utc_now()
            return _copy(self._sessions[session_id])

    def event_log(self, session_id: str) -> EventLog:
        with self._lock:
            self.ensure_session(session_id)
            return self._events[session_id]

    def append_event(self, session_id: str, event: str, data: Mapping[str, Any]) -> Event:
        with self._lock:
            self.ensure_session(session_id)
            row = self._events[session_id].append(event, _copy(dict(data)))
            self._sessions[session_id]["updated_at"] = utc_now()
            return row

    def read_events(self, session_id: str, after: int = 0) -> List[Event]:
        with self._lock:
            self.ensure_session(session_id)
            return self._events[session_id].read(after)

    def get_current_plan(self, session_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            self.ensure_session(session_id)
            return _copy(self._plans[session_id])

    def publish_plan(self, session_id: str, plan: Mapping[str, Any]) -> Dict[str, Any]:
        with self._lock:
            self.ensure_session(session_id)
            value = _copy(plan.to_dict() if callable(getattr(plan, "to_dict", None)) else dict(plan))
            self._plans[session_id] = value
            self._history[session_id].append({**_copy(value), "published_at": utc_now()})
            self._sessions[session_id]["updated_at"] = utc_now()
            return _copy(value)

    def update_current_plan(self, session_id: str, plan: Mapping[str, Any]) -> Dict[str, Any]:
        with self._lock:
            self.ensure_session(session_id)
            value = _copy(plan.to_dict() if callable(getattr(plan, "to_dict", None)) else dict(plan))
            self._plans[session_id] = value
            self._sessions[session_id]["updated_at"] = utc_now()
            return _copy(value)

    def list_plan_history(self, session_id: str) -> List[Dict[str, Any]]:
        with self._lock:
            self.ensure_session(session_id)
            return _copy(self._history[session_id])

    def record_execution(self, session_id: str, execution: Mapping[str, Any]) -> Dict[str, Any]:
        with self._lock:
            self.ensure_session(session_id)
            value = _copy(dict(execution))
            self._executions[session_id].append(value)
            self._sessions[session_id]["updated_at"] = utc_now()
            return _copy(value)

    def list_executions(self, session_id: str) -> List[Dict[str, Any]]:
        with self._lock:
            self.ensure_session(session_id)
            return _copy(self._executions[session_id])

    def register_artifact(self, session_id: str, artifact: Mapping[str, Any]) -> Dict[str, Any]:
        with self._lock:
            self.ensure_session(session_id)
            value = _copy(dict(artifact))
            if not value.get("id"):
                value["id"] = "art-%s" % secrets.token_hex(8)
            value.setdefault("source_ref", "artifact:%s" % value["id"])
            value.setdefault("created_at", utc_now())
            self._artifacts[session_id].append(value)
            return _copy(value)

    def list_artifacts(self, session_id: str) -> List[Dict[str, Any]]:
        with self._lock:
            self.ensure_session(session_id)
            return _copy(self._artifacts[session_id])

    def add_upload(self, session_id: str, upload: Mapping[str, Any]) -> Dict[str, Any]:
        with self._lock:
            self.ensure_session(session_id)
            value = _copy(dict(upload))
            value.setdefault("id", "upl-%s" % secrets.token_hex(8))
            value.setdefault("created_at", utc_now())
            if "checksum" not in value and isinstance(value.get("data"), (bytes, bytearray)):
                value["checksum"] = hashlib.sha256(bytes(value["data"])).hexdigest()
            self._uploads[session_id].append(value)
            return _copy(value)

    def list_uploads(self, session_id: str) -> List[Dict[str, Any]]:
        with self._lock:
            self.ensure_session(session_id)
            # Binary payloads are intentionally omitted from context snapshots.
            rows = []
            for item in self._uploads[session_id]:
                row = _copy(item)
                row.pop("data", None)
                rows.append(row)
            return rows

    def pending_input_requests(self, session_id: str) -> List[Dict[str, Any]]:
        requests: Dict[str, Dict[str, Any]] = {}
        pending: Dict[str, Dict[str, Any]] = {}
        for event in self.read_events(session_id):
            data = event.data
            if event.event == "input_required":
                request = data.get("request")
                if isinstance(request, Mapping) and isinstance(request.get("request_id"), str):
                    request_id = str(request["request_id"])
                    requests[request_id] = _copy(dict(request))
                    pending[request_id] = _copy(dict(request))
            elif event.event in ("input_fulfilled", "input_cancelled"):
                request_id = data.get("request_id")
                if isinstance(request_id, str):
                    pending.pop(request_id, None)
            elif event.event == "input_invalidated":
                request_id = data.get("request_id")
                if isinstance(request_id, str) and request_id in requests:
                    pending[request_id] = _copy(requests[request_id])
        return list(pending.values())

    def fulfilled_input_requests_using_upload(self, session_id: str, upload_id: str) -> List[str]:
        allocations: Dict[str, Dict[str, Any]] = {}
        for event in self.read_events(session_id):
            request_id = event.data.get("request_id")
            if not isinstance(request_id, str):
                continue
            if event.event == "input_fulfilled":
                allocation = event.data.get("allocation")
                allocations[request_id] = dict(allocation) if isinstance(allocation, Mapping) else {}
            elif event.event in {"input_cancelled", "input_invalidated"}:
                allocations.pop(request_id, None)
        return [
            request_id
            for request_id, allocation in allocations.items()
            if any(upload_id in values for values in allocation.values() if isinstance(values, list))
        ]

    def append_message(self, session_id: str, role: str, text: str, **metadata: Any) -> Dict[str, Any]:
        if role not in {"user", "assistant", "system", "notice"}:
            raise ValueError("unsupported message role")
        if not isinstance(text, str) or not text.strip():
            raise ValueError("message text is required")
        with self._lock:
            self.ensure_session(session_id)
            row = {"role": role, "text": text[:20000], "ts": utc_now(), **_copy(metadata)}
            self._messages[session_id].append(row)
            return _copy(row)

    def list_messages(self, session_id: str) -> List[Dict[str, Any]]:
        with self._lock:
            self.ensure_session(session_id)
            return _copy(self._messages[session_id])

    def history(self, session_id: str) -> Dict[str, Any]:
        return {
            "session": self.get_session(session_id),
            "messages": self.list_messages(session_id),
            "events": [event.as_dict() for event in self.read_events(session_id)],
            "plans": self.list_plan_history(session_id),
            "current_plan": self.get_current_plan(session_id),
            "executions": self.list_executions(session_id),
            "artifacts": self.list_artifacts(session_id),
            "uploads": self.list_uploads(session_id),
        }

    def delete_session(self, session_id: str) -> bool:
        with self._lock:
            if session_id not in self._sessions:
                return False
            for collection in (self._sessions, self._events, self._plans, self._history, self._executions, self._artifacts, self._uploads, self._messages):
                collection.pop(session_id, None)
            return True


class JsonStore(InMemoryStore):
    """Single-process durable store backed by one directory per session.

    It intentionally uses the same public methods as ``InMemoryStore``.  The
    JSON files are human-readable and easy to migrate; applications needing
    multi-process locking should provide a database-backed implementation.
    """

    def __init__(self, root: os.PathLike) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        super().__init__()

    def _session_root(self, session_id: str) -> Path:
        # Session ids are generated by the store, but validate caller-provided
        # ids before using them as paths.
        if not session_id or any(char not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-" for char in session_id):
            raise ValueError("invalid session id")
        return self.root / session_id

    def create_session(self, session_id: Optional[str] = None, **metadata: Any) -> Dict[str, Any]:
        sid = session_id or "sess-%s" % secrets.token_hex(8)
        session_root = self._session_root(sid)
        with self._lock:
            if sid in self._sessions:
                return _copy(self._sessions[sid])
            if session_root.is_symlink() or (session_root.exists() and not session_root.is_dir()):
                raise EventLogError("session root is not a private directory")
            session_root.mkdir(parents=True, exist_ok=True)
            try:
                session_root.chmod(0o700)
            except OSError:
                pass
            meta_path = session_root / "session.json"
            if meta_path.exists():
                try:
                    row = json.loads(meta_path.read_text(encoding="utf-8"))
                except (OSError, ValueError) as exc:
                    raise EventLogError("session metadata is invalid") from exc
                if not isinstance(row, dict):
                    raise EventLogError("session metadata is invalid")
            else:
                row = {
                    "session_id": sid,
                    "status": "idle",
                    "turns": 0,
                    "created_at": utc_now(),
                    "updated_at": utc_now(),
                    **_copy(metadata),
                }
                self._write_json(meta_path, row)
            self._sessions[sid] = row
            self._events[sid] = EventLog(session_root / "events.jsonl")
            self._plans[sid] = self._read_json(session_root / "current-plan.json", None)
            history = self._read_json(session_root / "plans.json", []) or []
            executions = self._read_json(session_root / "executions.json", []) or []
            artifacts = self._read_json(session_root / "artifacts.json", []) or []
            uploads = self._read_json(session_root / "uploads.json", []) or []
            messages = self._read_json(session_root / "messages.json", []) or []
            if not all(isinstance(item, list) for item in (history, executions, artifacts, uploads, messages)):
                raise EventLogError("stored session collections are invalid")
            self._history[sid] = history
            self._executions[sid] = executions
            self._artifacts[sid] = artifacts
            self._uploads[sid] = uploads
            self._messages[sid] = messages
            return _copy(row)

    def _write_json(self, path: Path, value: Any) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str, allow_nan=False), encoding="utf-8")
        try:
            temporary.chmod(0o600)
        except OSError:
            pass
        os.replace(str(temporary), str(path))

    @staticmethod
    def _read_json(path: Path, default: Any) -> Any:
        if not path.exists():
            return default
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise EventLogError("stored JSON is invalid: %s" % path.name) from exc

    def publish_plan(self, session_id: str, plan: Mapping[str, Any]) -> Dict[str, Any]:
        value = super().publish_plan(session_id, plan)
        root = self._session_root(session_id)
        self._write_json(root / "current-plan.json", value)
        self._write_json(root / "plans.json", self._history[session_id])
        return value

    def append_event(self, session_id: str, event: str, data: Mapping[str, Any]) -> Event:
        with self._lock:
            row = super().append_event(session_id, event, data)
            self._write_json(self._session_root(session_id) / "session.json", self._sessions[session_id])
            return row

    def update_session(self, session_id: str, **fields: Any) -> Dict[str, Any]:
        value = super().update_session(session_id, **fields)
        self._write_json(self._session_root(session_id) / "session.json", self._sessions[session_id])
        return value

    def update_current_plan(self, session_id: str, plan: Mapping[str, Any]) -> Dict[str, Any]:
        value = super().update_current_plan(session_id, plan)
        self._write_json(self._session_root(session_id) / "current-plan.json", value)
        return value

    def record_execution(self, session_id: str, execution: Mapping[str, Any]) -> Dict[str, Any]:
        value = super().record_execution(session_id, execution)
        self._write_json(self._session_root(session_id) / "executions.json", self._executions[session_id])
        return value

    def register_artifact(self, session_id: str, artifact: Mapping[str, Any]) -> Dict[str, Any]:
        value = super().register_artifact(session_id, artifact)
        self._write_json(self._session_root(session_id) / "artifacts.json", self._artifacts[session_id])
        return value

    def add_upload(self, session_id: str, upload: Mapping[str, Any]) -> Dict[str, Any]:
        value = super().add_upload(session_id, upload)
        self._write_json(self._session_root(session_id) / "uploads.json", self._uploads[session_id])
        return value

    def append_message(self, session_id: str, role: str, text: str, **metadata: Any) -> Dict[str, Any]:
        value = super().append_message(session_id, role, text, **metadata)
        self._write_json(self._session_root(session_id) / "messages.json", self._messages[session_id])
        return value

    def delete_session(self, session_id: str) -> bool:
        session_root = self._session_root(session_id)
        if not session_root.exists():
            return False
        if session_root.is_symlink() or not session_root.is_dir():
            raise EventLogError("session root is invalid")
        # Keep deletion scoped to the validated session directory.
        shutil.rmtree(session_root)
        super().delete_session(session_id)
        return True


SessionStoreProtocol = SessionStore
