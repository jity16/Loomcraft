"""The event stream: an append-only, hash-chained record of everything observable.

Every state change the UI can render — a plan published, a step moved, an
execution started, an artifact registered — becomes an event.  Events are the
*only* channel between the engine and the outside world, which is what lets the
same reducer power a live SSE stream and a page reload from history.

Two properties matter:

**Append-only with a hash chain.** Each line commits to the previous one, and a
sidecar cursor pins the log's identity (device, inode, size, mtime) plus the
running chain digest.  A normal append is O(1): read the small cursor, verify the
file still matches, write one line.  If the sidecar is missing or disagrees with
the log, the log is re-scanned once from scratch; if the log itself is
inconsistent the writer **fails closed** rather than allocating a duplicate
sequence number over a corrupted tail.

**Sequence numbers are dense and monotonic.** Clients resume with
``after_seq`` and can prove they missed nothing.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Mapping

from .errors import EventLogError

CURSOR_SCHEMA = 1
CHAIN_DOMAIN = b"loomcraft.event-log.v1"
MAX_EVENT_LINE_BYTES = 512 * 1024
MAX_CURSOR_BYTES = 8 * 1024

# ── Event vocabulary ────────────────────────────────────────────────────────

#: Emitted by the broker/engine and consumed by the renderer's reducer. Hosts may
#: append their own names; unknown events are ignored by the reducer rather than
#: breaking it.
EVENT_TYPES: tuple[str, ...] = (
    "plan_published",
    "step_updated",
    "execution_started",
    "execution_progress",
    "execution_finished",
    "node_log",
    "artifact_registered",
    "input_required",
    "input_fulfilled",
    "input_cancelled",
    "input_invalidated",
    "approval_required",
    "approval_resolved",
    "tool_call",
    "tool_result",
    "message",
    "message_delta",
    "notice",
    "error",
    "done",
)


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


@dataclass(frozen=True, slots=True)
class Event:
    """One persisted, sequenced observation."""

    seq: int
    event: str
    data: dict[str, Any]
    ts: str

    def to_dict(self) -> dict[str, Any]:
        return {"seq": self.seq, "event": self.event, "data": self.data, "ts": self.ts}

    @staticmethod
    def from_dict(row: Mapping[str, Any]) -> "Event":
        return Event(
            seq=int(row.get("seq", 0)),
            event=str(row.get("event", "")),
            data=dict(row.get("data") or {}),
            ts=str(row.get("ts", "")),
        )

    def sse(self) -> str:
        """Render as a Server-Sent Events frame."""
        payload = json.dumps(self.to_dict(), ensure_ascii=False)
        return f"event: {self.event}\ndata: {payload}\n\n"


# ── Hash chain helpers ──────────────────────────────────────────────────────


def _initial_chain() -> str:
    return hashlib.sha256(CHAIN_DOMAIN).hexdigest()


def _next_chain(previous: str, line: bytes) -> str:
    try:
        previous_bytes = bytes.fromhex(previous)
    except ValueError as exc:
        raise EventLogError("event cursor chain is invalid") from exc
    if len(previous_bytes) != 32:
        raise EventLogError("event cursor chain is invalid")
    digest = hashlib.sha256(previous_bytes)
    digest.update(len(line).to_bytes(8, "big"))
    digest.update(line)
    return digest.hexdigest()


def _fingerprint(info: os.stat_result) -> list[int]:
    return [
        int(info.st_dev),
        int(info.st_ino),
        int(info.st_size),
        int(getattr(info, "st_mtime_ns", int(info.st_mtime * 1_000_000_000))),
    ]


def _empty_cursor() -> dict[str, Any]:
    return {
        "schema": CURSOR_SCHEMA,
        "seq": 0,
        "fingerprint": None,
        "chain": _initial_chain(),
    }


def _atomic_write_json(path: Path, value: Mapping[str, Any]) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def _open_log(path: Path, flags: int, mode: int = 0o600) -> int:
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags, mode)
    except OSError as exc:
        raise EventLogError("event log cannot be opened safely") from exc
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise EventLogError("event log must be a regular file")
        return fd
    except BaseException:
        os.close(fd)
        raise


# ── Event log ───────────────────────────────────────────────────────────────


class EventLog:
    """A durable, hash-chained, append-only JSONL event log for one session."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.cursor_path = self.path.with_name(self.path.name + ".cursor.json")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._subscribers: list[Callable[[Event], None]] = []

    # ── Subscriptions ───────────────────────────────────────────────────────

    def subscribe(self, callback: Callable[[Event], None]) -> Callable[[], None]:
        """Register a live listener; returns an unsubscribe callable."""
        with self._lock:
            self._subscribers.append(callback)

        def unsubscribe() -> None:
            with self._lock:
                if callback in self._subscribers:
                    self._subscribers.remove(callback)

        return unsubscribe

    # ── Reading ─────────────────────────────────────────────────────────────

    def read(self, *, after_seq: int = 0) -> list[Event]:
        """Return persisted events with ``seq > after_seq``."""
        return list(self.iter_events(after_seq=after_seq))

    def iter_events(self, *, after_seq: int = 0) -> Iterator[Event]:
        if not self.path.exists():
            return
        with self.path.open("r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(row, dict):
                    continue
                event = Event.from_dict(row)
                if event.seq > after_seq:
                    yield event

    @property
    def last_seq(self) -> int:
        with self._lock:
            return int(self._cursor()["seq"])

    # ── Writing ─────────────────────────────────────────────────────────────

    def append(self, event: str, data: Mapping[str, Any] | None = None) -> Event:
        """Append one event, then notify live subscribers.

        Notification happens after the durable write so a subscriber can never
        observe an event that is not in the log.
        """
        with self._lock:
            cursor = self._cursor()
            seq = int(cursor["seq"]) + 1
            row = {
                "seq": seq,
                "event": event,
                "data": dict(data or {}),
                "ts": utcnow_iso(),
            }
            line = (json.dumps(row, ensure_ascii=False) + "\n").encode("utf-8")
            if len(line) > MAX_EVENT_LINE_BYTES:
                raise EventLogError("event exceeds the maximum log line size")

            fd = _open_log(self.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT)
            try:
                before = os.fstat(fd)
                expected = (
                    int(cursor["fingerprint"][2])
                    if cursor["fingerprint"] is not None
                    else 0
                )
                if before.st_size != expected:
                    raise EventLogError("event log changed before append")
                written = 0
                while written < len(line):
                    chunk = os.write(fd, line[written:])
                    if chunk <= 0:
                        raise EventLogError("event log append made no progress")
                    written += chunk
                os.fsync(fd)
                after = os.fstat(fd)
            finally:
                os.close(fd)

            _atomic_write_json(
                self.cursor_path,
                {
                    "schema": CURSOR_SCHEMA,
                    "seq": seq,
                    "fingerprint": _fingerprint(after),
                    "chain": _next_chain(str(cursor["chain"]), line),
                },
            )
            record = Event.from_dict(row)
            subscribers = list(self._subscribers)

        for callback in subscribers:
            try:
                callback(record)
            except Exception:  # noqa: BLE001 - a bad listener must not break the log
                pass
        return record

    def extend(self, events: Iterable[tuple[str, Mapping[str, Any]]]) -> list[Event]:
        return [self.append(name, data) for name, data in events]

    # ── Integrity ───────────────────────────────────────────────────────────

    def verify(self) -> bool:
        """Recompute the chain from scratch and compare it with the cursor.

        Returns ``True`` when the log is intact. Use it in audits or on startup;
        the append path does not pay this cost.
        """
        with self._lock:
            try:
                cursor = self._load_cursor()
            except EventLogError:
                return False
            recomputed = self._scan()
            return (
                recomputed["seq"] == cursor["seq"]
                and recomputed["chain"] == cursor["chain"]
            )

    def _cursor(self) -> dict[str, Any]:
        try:
            return self._load_cursor()
        except EventLogError:
            # Sidecar missing or disagreeing with the log: rebuild it once from
            # the log itself. A log that is *itself* inconsistent still raises.
            cursor = self._scan()
            _atomic_write_json(self.cursor_path, cursor)
            return cursor

    def _load_cursor(self) -> dict[str, Any]:
        try:
            info = self.cursor_path.lstat()
            if not stat.S_ISREG(info.st_mode) or not 1 <= info.st_size <= MAX_CURSOR_BYTES:
                raise EventLogError("event cursor is not a bounded regular file")
            cursor = json.loads(self.cursor_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise EventLogError("event cursor is unavailable") from exc
        if not isinstance(cursor, dict) or cursor.get("schema") != CURSOR_SCHEMA:
            raise EventLogError("event cursor schema is invalid")
        if set(cursor) != {"schema", "seq", "fingerprint", "chain"}:
            raise EventLogError("event cursor fields are invalid")

        fingerprint = cursor.get("fingerprint")
        if fingerprint is None:
            if self.path.exists() and self.path.stat().st_size:
                raise EventLogError("event cursor claims an empty log")
            return cursor
        if not isinstance(fingerprint, list) or len(fingerprint) != 4:
            raise EventLogError("event cursor fingerprint is invalid")
        try:
            actual = _fingerprint(self.path.stat())
        except OSError as exc:
            raise EventLogError("event log is unavailable") from exc
        if actual != [int(item) for item in fingerprint]:
            raise EventLogError("event log does not match its cursor")
        return cursor

    def _scan(self) -> dict[str, Any]:
        """Full recovery pass: recompute seq + chain by reading every line."""
        cursor = _empty_cursor()
        if not self.path.exists():
            return cursor
        chain = _initial_chain()
        expected_seq = 0
        with self.path.open("rb") as handle:
            for raw in handle:
                if not raw.endswith(b"\n"):
                    raise EventLogError("event log ends with a partial line")
                try:
                    row = json.loads(raw.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise EventLogError("event log contains invalid JSON") from exc
                if not isinstance(row, dict):
                    raise EventLogError("event log line is not an object")
                expected_seq += 1
                if int(row.get("seq", -1)) != expected_seq:
                    raise EventLogError("event log sequence numbers are not contiguous")
                chain = _next_chain(chain, raw)
        cursor["seq"] = expected_seq
        cursor["chain"] = chain
        cursor["fingerprint"] = _fingerprint(self.path.stat())
        return cursor


class MemoryEventLog(EventLog):
    """An in-process event log with the same API and no filesystem writes.

    Used by tests, examples, and stateless deployments. It keeps the sequencing
    and subscription semantics but drops durability and the hash chain.
    """

    def __init__(self) -> None:  # noqa: D107 - deliberately does not call super()
        self.path = Path("<memory>")
        self.cursor_path = Path("<memory>.cursor.json")
        self._lock = threading.RLock()
        self._subscribers: list[Callable[[Event], None]] = []
        self._events: list[Event] = []

    def append(self, event: str, data: Mapping[str, Any] | None = None) -> Event:
        with self._lock:
            record = Event(
                seq=len(self._events) + 1,
                event=event,
                data=dict(data or {}),
                ts=utcnow_iso(),
            )
            self._events.append(record)
            subscribers = list(self._subscribers)
        for callback in subscribers:
            try:
                callback(record)
            except Exception:  # noqa: BLE001
                pass
        return record

    def iter_events(self, *, after_seq: int = 0) -> Iterator[Event]:
        with self._lock:
            snapshot = list(self._events)
        for event in snapshot:
            if event.seq > after_seq:
                yield event

    @property
    def last_seq(self) -> int:
        with self._lock:
            return len(self._events)

    def verify(self) -> bool:
        return True


__all__ = [
    "EVENT_TYPES",
    "Event",
    "EventLog",
    "MemoryEventLog",
    "utcnow_iso",
]
