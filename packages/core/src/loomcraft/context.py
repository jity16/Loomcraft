"""The runner contract: what a unit of work receives and returns.

A *runner* is any async callable ``run(ctx: NodeContext) -> NodeResult``.  That's
the whole extension point — LoomCraft never imports your domain code, you
register a function and the engine calls it.

The context is deliberately narrow. A runner can read its declared inputs, write
logs, emit artifacts, report progress, and check for cancellation.  It cannot
reach the plan, mutate other nodes, or write status for itself: status is the
engine's to assign based on what the runner returns.
"""

from __future__ import annotations

import asyncio
import hashlib
import mimetypes
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Literal, Mapping, Protocol, Sequence

LogLevel = Literal["debug", "info", "warn", "error", "success"]
NodeOutcome = Literal["succeeded", "failed", "waiting_approval", "skipped"]


@dataclass(frozen=True, slots=True)
class NodeResult:
    """What a runner reports back to the engine."""

    status: NodeOutcome
    error: str | None = None
    #: Free-form structured detail surfaced in the execution record and to the
    #: agent. Keep it small — it is echoed into the model's context.
    detail: dict[str, Any] = field(default_factory=dict)
    #: Set ``True`` to tell the engine this failure is worth another attempt.
    #: Ignored when the node has no retry budget left.
    retryable: bool = False

    @staticmethod
    def ok(**detail: Any) -> "NodeResult":
        return NodeResult(status="succeeded", detail=detail)

    @staticmethod
    def fail(message: str, *, retryable: bool = False, **detail: Any) -> "NodeResult":
        return NodeResult(
            status="failed", error=message, retryable=retryable, detail=detail
        )

    @staticmethod
    def retry(message: str, **detail: Any) -> "NodeResult":
        """Fail in a way the engine should re-attempt if budget remains."""
        return NodeResult(status="failed", error=message, retryable=True, detail=detail)

    @staticmethod
    def needs_approval(message: str = "", **detail: Any) -> "NodeResult":
        """Pause here until a human approves or rejects the node."""
        return NodeResult(
            status="waiting_approval", error=message or None, detail=detail
        )

    @staticmethod
    def skip(message: str = "", **detail: Any) -> "NodeResult":
        """Decline to do the work, without that counting as a failure.

        A runner that discovers its work is unnecessary — the input is already
        clean, the branch does not apply to this dataset — should say so rather
        than fabricate a success or raise.
        """
        return NodeResult(status="skipped", error=message or None, detail=detail)


@dataclass(frozen=True, slots=True)
class InputFile:
    """One resolved, integrity-checked input handed to a runner."""

    key: str
    path: Path
    filename: str
    size: int
    checksum: str
    source_ref: str
    content_type: str = "application/octet-stream"

    def read_bytes(self, limit: int | None = None) -> bytes:
        with self.path.open("rb") as handle:
            return handle.read() if limit is None else handle.read(limit)

    def read_text(self, encoding: str = "utf-8", limit: int | None = None) -> str:
        return self.read_bytes(limit).decode(encoding, errors="replace")


@dataclass(frozen=True, slots=True)
class EmittedArtifact:
    """A file a runner produced, registered as a session-owned deliverable."""

    id: str
    port_name: str
    filename: str
    path: Path
    size: int
    checksum: str
    content_type: str
    node_id: str


class NodeContext:
    """Everything a runner is allowed to touch.

    Instances are created by the engine per node attempt; a retry gets a fresh
    context with ``attempt`` incremented so runners can vary behaviour (longer
    timeouts, a different mirror) without tracking state themselves.
    """

    def __init__(
        self,
        *,
        run_id: str,
        node_id: str,
        attempt: int,
        inputs: Mapping[str, list[InputFile]],
        parameters: Mapping[str, Any],
        config: Mapping[str, Any],
        workdir: Path,
        dependencies: Mapping[str, Any] | None = None,
        outputs: Sequence[str] = (),
        on_log: Callable[[str, LogLevel, str], None] | None = None,
        on_progress: Callable[[str, float, str], None] | None = None,
        on_artifact: Callable[[EmittedArtifact], None] | None = None,
        on_adopted_artifact: Callable[[Mapping[str, Any]], None] | None = None,
        cancel_event: asyncio.Event | None = None,
    ) -> None:
        self.run_id = run_id
        self.node_id = node_id
        #: 1 on the first try, 2 on the first retry, and so on.
        self.attempt = attempt
        self.inputs = dict(inputs)
        #: Structured ``detail`` returned by each upstream node, keyed by node
        #: id. Files arrive through ``inputs``; this is for the small facts a
        #: step wants to pass on — a chosen threshold, a computed λ.
        self.dependencies = dict(dependencies or {})
        self.parameters = dict(parameters)
        self.config = dict(config)
        #: A private scratch directory for this node. Deleted with the run.
        self.workdir = workdir
        self.output_ports = list(outputs)
        self._on_log = on_log
        self._on_progress = on_progress
        self._on_artifact = on_artifact
        self._on_adopted_artifact = on_adopted_artifact
        self._cancel = cancel_event or asyncio.Event()
        self.workdir.mkdir(parents=True, exist_ok=True)

    # ── Reading inputs ──────────────────────────────────────────────────────

    def input(self, key: str) -> InputFile:
        """The single file bound to ``key``. Raises when absent or multi-valued."""
        files = self.inputs.get(key) or []
        if len(files) != 1:
            raise KeyError(
                f"input {key!r} resolved to {len(files)} files; use input_list()"
            )
        return files[0]

    def input_list(self, key: str) -> list[InputFile]:
        return list(self.inputs.get(key) or [])

    def optional_input(self, key: str) -> InputFile | None:
        files = self.inputs.get(key) or []
        return files[0] if len(files) == 1 else None

    def has_input(self, key: str) -> bool:
        return bool(self.inputs.get(key))

    # ── Reporting ───────────────────────────────────────────────────────────

    def log(self, message: str, level: LogLevel = "info") -> None:
        if self._on_log is not None:
            self._on_log(self.node_id, level, message)

    def progress(self, fraction: float, message: str = "") -> None:
        """Report 0.0–1.0 completion. Streamed to the UI as ``execution_progress``."""
        if self._on_progress is not None:
            self._on_progress(self.node_id, max(0.0, min(1.0, fraction)), message)

    def emit(
        self,
        port_name: str,
        filename: str,
        data: bytes | str,
        *,
        content_type: str | None = None,
    ) -> EmittedArtifact:
        """Write bytes as a durable artifact bound to one declared output port."""
        self._validate_port(port_name)
        relative = Path(filename)
        if relative.is_absolute() or ".." in relative.parts or not relative.name:
            # Runners routinely build filenames from data — a table name, a
            # chromosome id. Anything derived from input must not be able to
            # steer the write out of this node's private workdir.
            raise ValueError("artifact filename must stay inside the node workdir")
        payload = data.encode("utf-8") if isinstance(data, str) else data
        target = self.workdir / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload)
        return self.emit_path(port_name, target, content_type=content_type)

    def emit_path(
        self,
        port_name: str,
        path: str | Path,
        *,
        content_type: str | None = None,
    ) -> EmittedArtifact:
        """Register an already-written file as an artifact."""
        self._validate_port(port_name)
        source = Path(path)
        if not source.is_file():
            raise FileNotFoundError(f"artifact source is not a file: {source}")
        digest = hashlib.sha256()
        with source.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        artifact = EmittedArtifact(
            id=f"art-{digest.hexdigest()[:16]}",
            port_name=port_name,
            filename=source.name,
            path=source,
            size=source.stat().st_size,
            checksum=digest.hexdigest(),
            content_type=content_type
            or mimetypes.guess_type(source.name)[0]
            or "application/octet-stream",
            node_id=self.node_id,
        )
        if self._on_artifact is not None:
            self._on_artifact(artifact)
        return artifact

    def _validate_port(self, port_name: str) -> None:
        """Keep artifacts inside the contract the capability advertised.

        Downstream nodes bind inputs by port name, so an artifact on an
        undeclared port is an edge nobody agreed to.
        """
        if not isinstance(port_name, str) or not port_name:
            raise ValueError("artifact port_name must be a non-empty string")
        if self.output_ports and port_name not in self.output_ports:
            raise ValueError(
                f"artifact port {port_name!r} is not declared by this node; "
                f"declared ports are: {', '.join(self.output_ports)}"
            )

    def adopt_artifact(self, record: Mapping[str, Any]) -> None:
        """Claim an already-registered artifact as this node's output.

        Used by composite runners that delegate to a nested run: the inner run
        already registered the files, and re-registering would duplicate them.
        """
        if self._on_adopted_artifact is None:
            raise RuntimeError("this node context cannot adopt registered artifacts")
        self._on_adopted_artifact(dict(record))

    # ── Cancellation ────────────────────────────────────────────────────────

    @property
    def cancelled(self) -> bool:
        """Poll this inside long CPU-bound loops that never hit an ``await``."""
        return self._cancel.is_set()

    def raise_if_cancelled(self) -> None:
        if self._cancel.is_set():
            raise asyncio.CancelledError(f"node {self.node_id} cancelled")

    async def wait_cancelled(self) -> None:
        await self._cancel.wait()


class Runner(Protocol):
    """Structural type for a unit of work."""

    def __call__(self, ctx: NodeContext) -> Awaitable[NodeResult]: ...


RunnerFn = Callable[[NodeContext], Awaitable[NodeResult]]


__all__ = [
    "EmittedArtifact",
    "InputFile",
    "LogLevel",
    "NodeContext",
    "NodeOutcome",
    "NodeResult",
    "Runner",
    "RunnerFn",
]
