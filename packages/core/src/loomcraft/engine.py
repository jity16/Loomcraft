"""The DAG execution engine.

The engine takes an :class:`ExecutionGraph` — a set of nodes with dependencies —
and drives it to a terminal state.  It is deliberately small and boring, because
this is the part that must never lie about what happened.

Scheduling model: a single driver coroutine per run repeatedly scans for nodes
whose dependencies have all succeeded, spawns **every one of them at once**
(bounded by a semaphore), and then sleeps on an event that node tasks set when
they finish.  Parallelism is therefore a property of the graph shape, not
something the plan author asks for.

What the driver guarantees:

- **Skip propagation.** A node whose upstream failed, was skipped, or was
  cancelled is marked ``skipped``; it never runs on stale inputs.
- **Retry with backoff.** ``max_attempts`` and ``retry_backoff_seconds`` are per
  node. A retry gets a fresh :class:`~loomcraft.context.NodeContext` with an
  incremented ``attempt``.
- **Timeouts.** A node that exceeds ``timeout_seconds`` is cancelled and treated
  as a (possibly retryable) failure.
- **Human approval.** A node may return ``waiting_approval``; the run parks in
  ``paused_approval`` until :meth:`Run.approve` resolves it.
- **Stall detection.** Nothing runnable, nothing in flight, and not everything
  terminal is treated as a bug and fails the run — never as success.
- **Cancellation is awaited.** :meth:`Run.cancel` does not return until every
  node task has actually stopped, so a cancelled run leaves nothing writing
  behind it.
"""

from __future__ import annotations

import asyncio
import contextlib
import secrets
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Literal, Mapping, Sequence

from .context import EmittedArtifact, InputFile, LogLevel, NodeContext, NodeResult
from .errors import RegistryError
from .graph import layers, validate as validate_graph
from .registry import Capability, Registry, Workflow
from .store import Session

NodeStatus = Literal[
    "pending", "running", "waiting_approval", "succeeded", "failed", "skipped", "cancelled"
]
RunStatus = Literal[
    "created", "running", "paused_approval", "succeeded", "failed", "cancelled"
]

TERMINAL_NODE: frozenset[str] = frozenset({"succeeded", "failed", "skipped", "cancelled"})
TERMINAL_RUN: frozenset[str] = frozenset({"succeeded", "failed", "cancelled"})


# ── Graph model ─────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class ExecutionNode:
    """One schedulable unit inside an :class:`ExecutionGraph`."""

    id: str
    name: str
    runner: str
    depends_on: tuple[str, ...] = ()
    #: ``{input_key: [source_ref, ...]}`` bound before the run starts.
    inputs: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    parameters: Mapping[str, Any] = field(default_factory=dict)
    config: Mapping[str, Any] = field(default_factory=dict)
    outputs: tuple[str, ...] = ()
    timeout_seconds: float | None = None
    max_attempts: int = 1
    retry_backoff_seconds: float = 1.0
    requires_approval: bool = False


@dataclass(frozen=True, slots=True)
class ExecutionGraph:
    """A validated, ready-to-run DAG."""

    id: str
    name: str
    nodes: tuple[ExecutionNode, ...]
    kind: Literal["capability", "workflow"] = "capability"
    source_id: str | None = None

    def __post_init__(self) -> None:
        ids = [node.id for node in self.nodes]
        if len(ids) != len(set(ids)):
            raise RegistryError("execution graph node ids must be unique")
        issues = validate_graph({node.id: list(node.depends_on) for node in self.nodes})
        if issues:
            raise RegistryError("; ".join(str(issue) for issue in issues))

    @property
    def adjacency(self) -> dict[str, list[str]]:
        return {node.id: list(node.depends_on) for node in self.nodes}

    @property
    def layers(self) -> list[list[str]]:
        return layers(self.adjacency)

    def node(self, node_id: str) -> ExecutionNode:
        for node in self.nodes:
            if node.id == node_id:
                return node
        raise KeyError(node_id)


def graph_from_capability(
    capability: Capability,
    *,
    sources: Mapping[str, Sequence[str]],
    parameters: Mapping[str, Any],
    graph_id: str | None = None,
) -> ExecutionGraph:
    """Wrap one capability as a single-node graph.

    A capability run goes through the exact same driver as a workflow run — same
    retry, cancellation, artifact, and event semantics — rather than a separate
    "simple" path that could drift.
    """
    node = ExecutionNode(
        id="execute",
        name=capability.name,
        runner=capability.runner,
        inputs={key: tuple(value) for key, value in sources.items()},
        parameters=dict(parameters),
        config=dict(capability.config),
        outputs=tuple(port.name for port in capability.outputs),
        timeout_seconds=capability.timeout_seconds,
        max_attempts=capability.max_attempts,
        retry_backoff_seconds=capability.retry_backoff_seconds,
        requires_approval=capability.requires_approval,
    )
    return ExecutionGraph(
        id=graph_id or f"cap-{secrets.token_hex(6)}",
        name=capability.name,
        nodes=(node,),
        kind="capability",
        source_id=capability.id,
    )


def graph_from_workflow(
    workflow: Workflow,
    *,
    sources: Mapping[str, Sequence[str]],
    parameters: Mapping[str, Any],
    graph_id: str | None = None,
) -> ExecutionGraph:
    """Expand a registered workflow into its runnable node graph."""
    nodes: list[ExecutionNode] = []
    for spec in workflow.nodes:
        bound = {
            key: tuple(sources.get(key, ()))
            for key in spec.inputs
            if sources.get(key)
        }
        nodes.append(
            ExecutionNode(
                id=spec.id,
                name=spec.name,
                runner=spec.runner,
                depends_on=tuple(spec.depends_on),
                inputs=bound,
                parameters=dict(parameters),
                config=dict(spec.config),
                outputs=tuple(port.name for port in spec.outputs),
                timeout_seconds=spec.timeout_seconds,
                max_attempts=spec.max_attempts,
                retry_backoff_seconds=spec.retry_backoff_seconds,
                requires_approval=spec.requires_approval,
            )
        )
    return ExecutionGraph(
        id=graph_id or f"wf-{secrets.token_hex(6)}",
        name=workflow.name,
        nodes=tuple(nodes),
        kind="workflow",
        source_id=workflow.id,
    )


# ── Run state ───────────────────────────────────────────────────────────────


@dataclass
class NodeState:
    id: str
    status: NodeStatus = "pending"
    attempts: int = 0
    error: str | None = None
    detail: dict[str, Any] = field(default_factory=dict)
    started_at: float | None = None
    finished_at: float | None = None
    artifacts: list[dict[str, Any]] = field(default_factory=list)

    @property
    def duration_seconds(self) -> float | None:
        if self.started_at is None:
            return None
        return round((self.finished_at or time.monotonic()) - self.started_at, 3)

    def to_dict(self) -> dict[str, Any]:
        return {
            "node_id": self.id,
            "status": self.status,
            "attempts": self.attempts,
            "error": self.error,
            "detail": self.detail,
            "duration_seconds": self.duration_seconds,
            "artifacts": list(self.artifacts),
        }


class Run:
    """A live execution of one :class:`ExecutionGraph`."""

    def __init__(self, run_id: str, graph: ExecutionGraph, engine: "Engine") -> None:
        self.id = run_id
        self.graph = graph
        self.status: RunStatus = "created"
        self.nodes: dict[str, NodeState] = {
            node.id: NodeState(id=node.id) for node in graph.nodes
        }
        self.started_at = time.monotonic()
        self.finished_at: float | None = None
        self.error: str | None = None

        self._engine = engine
        self._wake = asyncio.Event()
        self._done = asyncio.Event()
        self._cancel = asyncio.Event()
        self._driver: asyncio.Task[None] | None = None
        self._tasks: set[asyncio.Task[None]] = set()
        self._approvals: dict[str, bool] = {}

    # ── Public control surface ──────────────────────────────────────────────

    async def wait(self) -> "Run":
        await self._done.wait()
        return self

    async def cancel(self) -> bool:
        """Stop the run and await every node task before returning.

        Returning early would let a node keep writing artifacts after the caller
        believed the run was over, so this deliberately blocks until quiet.
        """
        if self.status in TERMINAL_RUN:
            return False
        self._cancel.set()
        self._wake.set()

        tasks = [task for task in self._tasks if not task.done()]
        for task in tasks:
            task.cancel()
        if self._driver is not None and not self._driver.done():
            self._driver.cancel()
        pending = [*tasks, *( [self._driver] if self._driver is not None else [] )]
        for task in pending:
            if task is None or task is asyncio.current_task():
                continue
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task

        for state in self.nodes.values():
            if state.status not in TERMINAL_NODE:
                state.status = "cancelled"
                state.error = state.error or "cancelled"
                state.finished_at = time.monotonic()
        self._finish("cancelled")
        return True

    def approve(self, node_id: str, approved: bool = True) -> bool:
        """Resolve a node parked in ``waiting_approval``."""
        state = self.nodes.get(node_id)
        if state is None or state.status != "waiting_approval":
            return False
        self._approvals[node_id] = approved
        self._wake.set()
        return True

    @property
    def cancelled(self) -> bool:
        return self._cancel.is_set()

    @property
    def pending_approvals(self) -> list[str]:
        return sorted(
            node_id
            for node_id, state in self.nodes.items()
            if state.status == "waiting_approval"
        )

    @property
    def duration_seconds(self) -> float:
        return round((self.finished_at or time.monotonic()) - self.started_at, 3)

    @property
    def artifacts(self) -> list[dict[str, Any]]:
        collected: list[dict[str, Any]] = []
        for state in self.nodes.values():
            collected.extend(state.artifacts)
        return collected

    @property
    def failed_nodes(self) -> list[dict[str, Any]]:
        return [
            {"node_id": state.id, "error": state.error, "attempts": state.attempts}
            for state in self.nodes.values()
            if state.status == "failed"
        ]

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "graph_id": self.graph.id,
            "kind": self.graph.kind,
            "capability": self.graph.source_id,
            "name": self.graph.name,
            "status": self.status,
            "duration_seconds": self.duration_seconds,
            "error": self.error,
            "nodes": {key: state.to_dict() for key, state in self.nodes.items()},
            "failed_nodes": self.failed_nodes,
            "artifacts": self.artifacts,
        }

    # ── Internals ───────────────────────────────────────────────────────────

    def _finish(self, status: RunStatus) -> None:
        if self.status in TERMINAL_RUN:
            return
        self.status = status
        self.finished_at = time.monotonic()
        self._done.set()


class Engine:
    """Drives execution graphs against a registry and a session."""

    def __init__(
        self,
        registry: Registry,
        session: Session,
        *,
        max_parallel: int = 8,
        emit: Callable[[str, Mapping[str, Any]], Any] | None = None,
        stream_logs: bool = False,
    ) -> None:
        self.registry = registry
        self.session = session
        self.max_parallel = max(1, max_parallel)
        self._emit = emit or (lambda name, data: session.emit(name, data))
        self.stream_logs = stream_logs
        self._runs: dict[str, Run] = {}
        self._semaphore = asyncio.Semaphore(self.max_parallel)

    # ── Public API ──────────────────────────────────────────────────────────

    def submit(self, graph: ExecutionGraph, *, run_id: str | None = None) -> Run:
        """Start a run in the background and return its handle immediately."""
        missing = [
            node.runner for node in graph.nodes if not self.registry.has_runner(node.runner)
        ]
        if missing:
            raise RegistryError(
                "graph references unregistered runners: " + ", ".join(sorted(set(missing)))
            )
        identifier = run_id or f"run-{secrets.token_hex(8)}"
        run = Run(identifier, graph, self)
        self._runs[identifier] = run
        run.status = "running"
        run._driver = asyncio.create_task(self._drive(run), name=f"loomcraft-drive-{identifier}")
        return run

    async def execute(self, graph: ExecutionGraph, *, run_id: str | None = None) -> Run:
        """Submit and await a graph in one call."""
        run = self.submit(graph, run_id=run_id)
        try:
            await run.wait()
        except asyncio.CancelledError:
            await run.cancel()
            raise
        return run

    def get(self, run_id: str) -> Run | None:
        return self._runs.get(run_id)

    async def cancel(self, run_id: str) -> bool:
        run = self._runs.get(run_id)
        return await run.cancel() if run is not None else False

    async def cancel_all(self) -> None:
        for run in list(self._runs.values()):
            with contextlib.suppress(Exception):
                await run.cancel()

    # ── Driver ──────────────────────────────────────────────────────────────

    async def _drive(self, run: Run) -> None:
        try:
            while True:
                if run.cancelled:
                    return

                self._apply_approvals(run)
                self._propagate_skips(run)

                runnable = self._runnable(run)
                for node_id in runnable:
                    run.nodes[node_id].status = "running"
                    run.nodes[node_id].started_at = time.monotonic()
                    task = asyncio.create_task(
                        self._run_node(run, run.graph.node(node_id)),
                        name=f"loomcraft-node-{run.id}-{node_id}",
                    )
                    run._tasks.add(task)
                    task.add_done_callback(run._tasks.discard)

                statuses = {key: state.status for key, state in run.nodes.items()}
                in_flight = sum(1 for value in statuses.values() if value == "running")
                waiting = sum(1 for value in statuses.values() if value == "waiting_approval")

                if all(value in TERMINAL_NODE for value in statuses.values()):
                    self._finalize(run)
                    return

                if not runnable and in_flight == 0 and waiting > 0:
                    if run.status != "paused_approval":
                        run.status = "paused_approval"
                        self._emit(
                            "approval_required",
                            {
                                "execution_id": run.id,
                                "nodes": run.pending_approvals,
                            },
                        )
                elif (runnable or in_flight) and run.status == "paused_approval":
                    run.status = "running"

                if not runnable and in_flight == 0 and waiting == 0:
                    # Not everything is terminal, yet nothing can move. A cyclic
                    # or inconsistent graph reaches here; it is never success.
                    run.error = "graph stalled with no runnable nodes"
                    for state in run.nodes.values():
                        if state.status not in TERMINAL_NODE:
                            state.status = "failed"
                            state.error = state.error or run.error
                            state.finished_at = time.monotonic()
                    self._finalize(run, forced="failed")
                    return

                run._wake.clear()
                await run._wake.wait()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - the driver must never hang a run
            run.error = f"engine driver crashed: {type(exc).__name__}: {exc}"
            for state in run.nodes.values():
                if state.status not in TERMINAL_NODE:
                    state.status = "failed"
                    state.error = state.error or run.error
                    state.finished_at = time.monotonic()
            self._finalize(run, forced="failed")

    def _runnable(self, run: Run) -> list[str]:
        statuses = {key: state.status for key, state in run.nodes.items()}
        ready: list[str] = []
        for node in run.graph.nodes:
            if statuses[node.id] != "pending":
                continue
            if all(statuses.get(dep) == "succeeded" for dep in node.depends_on):
                ready.append(node.id)
        return ready

    def _propagate_skips(self, run: Run) -> None:
        changed = True
        while changed:
            changed = False
            statuses = {key: state.status for key, state in run.nodes.items()}
            for node in run.graph.nodes:
                state = run.nodes[node.id]
                if state.status != "pending":
                    continue
                if any(
                    statuses.get(dep) in {"failed", "skipped", "cancelled"}
                    for dep in node.depends_on
                ):
                    state.status = "skipped"
                    state.error = "upstream node did not succeed"
                    state.finished_at = time.monotonic()
                    changed = True
                    self._emit(
                        "execution_progress",
                        {
                            "execution_id": run.id,
                            "node_id": node.id,
                            "status": "skipped",
                        },
                    )

    def _apply_approvals(self, run: Run) -> None:
        for node_id, approved in list(run._approvals.items()):
            state = run.nodes.get(node_id)
            if state is None or state.status != "waiting_approval":
                continue
            state.status = "succeeded" if approved else "failed"
            state.error = None if approved else "rejected by reviewer"
            state.finished_at = time.monotonic()
            run._approvals.pop(node_id, None)
            self._emit(
                "approval_resolved",
                {
                    "execution_id": run.id,
                    "node_id": node_id,
                    "approved": approved,
                },
            )

    def _finalize(self, run: Run, *, forced: RunStatus | None = None) -> None:
        statuses = [state.status for state in run.nodes.values()]
        if forced is not None:
            final: RunStatus = forced
        elif any(status == "failed" for status in statuses):
            final = "failed"
        elif any(status == "cancelled" for status in statuses):
            final = "cancelled"
        else:
            final = "succeeded"
        run._finish(final)

    # ── Node execution ──────────────────────────────────────────────────────

    async def _run_node(self, run: Run, node: ExecutionNode) -> None:
        state = run.nodes[node.id]
        runner = self.registry.runner(node.runner)
        attempts = max(1, node.max_attempts)

        try:
            for attempt in range(1, attempts + 1):
                if run.cancelled:
                    state.status = "cancelled"
                    state.error = "cancelled"
                    break
                state.attempts = attempt
                self._emit(
                    "execution_progress",
                    {
                        "execution_id": run.id,
                        "node_id": node.id,
                        "status": "running",
                        "attempt": attempt,
                        "max_attempts": attempts,
                    },
                )

                try:
                    inputs = self._bind_inputs(run, node)
                except Exception as exc:  # noqa: BLE001 - input binding is a node failure
                    state.status = "failed"
                    state.error = f"input binding failed: {exc}"
                    break

                ctx = NodeContext(
                    run_id=run.id,
                    node_id=node.id,
                    attempt=attempt,
                    inputs=inputs,
                    parameters=dict(node.parameters),
                    config=dict(node.config),
                    workdir=self.session.run_dir(run.id) / node.id / f"attempt-{attempt}",
                    outputs=node.outputs,
                    on_log=self._node_logger(run),
                    on_progress=self._node_progress(run),
                    on_artifact=self._node_artifact(run, node, state),
                    cancel_event=run._cancel,
                )

                try:
                    async with self._semaphore:
                        if node.timeout_seconds:
                            result = await asyncio.wait_for(
                                runner(ctx), timeout=node.timeout_seconds
                            )
                        else:
                            result = await runner(ctx)
                except asyncio.CancelledError:
                    state.status = "cancelled"
                    state.error = "cancelled"
                    raise
                except asyncio.TimeoutError:
                    result = NodeResult.retry(
                        f"node timed out after {node.timeout_seconds}s"
                    )
                except Exception as exc:  # noqa: BLE001 - stable runner boundary
                    result = NodeResult.fail(f"{type(exc).__name__}: {exc}")

                state.detail = dict(result.detail)
                if result.status == "succeeded":
                    state.status = "succeeded"
                    state.error = None
                    break
                if result.status == "waiting_approval":
                    state.status = "waiting_approval"
                    state.error = result.error
                    break

                state.error = result.error or "node failed"
                should_retry = result.retryable and attempt < attempts and not run.cancelled
                if not should_retry:
                    state.status = "failed"
                    break
                delay = node.retry_backoff_seconds * (2 ** (attempt - 1))
                self._emit(
                    "execution_progress",
                    {
                        "execution_id": run.id,
                        "node_id": node.id,
                        "status": "retrying",
                        "attempt": attempt,
                        "max_attempts": attempts,
                        "retry_in_seconds": round(delay, 3),
                        "error": state.error,
                    },
                )
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(run._cancel.wait(), timeout=delay)
        except asyncio.CancelledError:
            if state.status not in TERMINAL_NODE:
                state.status = "cancelled"
                state.error = state.error or "cancelled"
            state.finished_at = time.monotonic()
            run._wake.set()
            raise
        finally:
            if state.status == "running":
                # Defensive: a runner path that fell through without deciding.
                state.status = "failed"
                state.error = state.error or "node produced no result"
            if state.status in TERMINAL_NODE or state.status == "waiting_approval":
                state.finished_at = state.finished_at or time.monotonic()
            self._emit(
                "execution_progress",
                {
                    "execution_id": run.id,
                    "node_id": node.id,
                    "status": state.status,
                    "attempt": state.attempts,
                    "error": state.error,
                    "duration_seconds": state.duration_seconds,
                },
            )
            run._wake.set()

    def _bind_inputs(self, run: Run, node: ExecutionNode) -> dict[str, list[InputFile]]:
        """Resolve declared source refs plus artifacts produced upstream.

        Upstream artifacts are keyed by their emitting port name, so a workflow
        node reads ``ctx.input("cleaned_table")`` regardless of which upstream
        node produced it.
        """
        bound: dict[str, list[InputFile]] = {}
        for key, refs in node.inputs.items():
            files: list[InputFile] = []
            for ref in refs:
                resolved = self.session.resolve_source(ref)
                files.append(
                    InputFile(
                        key=key,
                        path=resolved.path,
                        filename=resolved.filename,
                        size=resolved.size,
                        checksum=resolved.checksum,
                        source_ref=resolved.source_ref,
                        content_type=resolved.content_type,
                    )
                )
            bound[key] = files

        for dependency in node.depends_on:
            for artifact in run.nodes[dependency].artifacts:
                port = str(artifact.get("port_name") or "output")
                bound.setdefault(port, []).append(
                    InputFile(
                        key=port,
                        path=self.session.root / str(artifact["relpath"]),
                        filename=str(artifact["filename"]),
                        size=int(artifact["size"]),
                        checksum=str(artifact["checksum"]),
                        source_ref=str(artifact.get("source_ref", "")),
                        content_type=str(
                            artifact.get("content_type", "application/octet-stream")
                        ),
                    )
                )
        return bound

    def _node_logger(self, run: Run) -> Callable[[str, LogLevel, str], None]:
        def log(node_id: str, level: LogLevel, message: str) -> None:
            if not self.stream_logs:
                return
            self._emit(
                "node_log",
                {
                    "execution_id": run.id,
                    "node_id": node_id,
                    "level": level,
                    "message": message[:4000],
                },
            )

        return log

    def _node_progress(self, run: Run) -> Callable[[str, float, str], None]:
        def progress(node_id: str, fraction: float, message: str) -> None:
            self._emit(
                "execution_progress",
                {
                    "execution_id": run.id,
                    "node_id": node_id,
                    "status": "running",
                    "fraction": round(fraction, 4),
                    "message": message[:500],
                },
            )

        return progress

    def _node_artifact(
        self, run: Run, node: ExecutionNode, state: NodeState
    ) -> Callable[[EmittedArtifact], None]:
        def register(artifact: EmittedArtifact) -> None:
            record = self.session.add_artifact(
                artifact.path,
                port_name=artifact.port_name,
                display_name=artifact.filename,
                run_id=run.id,
                node_id=node.id,
            )
            state.artifacts.append(record)
            self._emit("artifact_registered", {"artifact": record})

        return register


def scratch_workdir(session: Session, run_id: str, node_id: str) -> Path:
    """Where a node's per-attempt files live. Exposed for tests and tooling."""
    return session.run_dir(run_id) / node_id


__all__ = [
    "Engine",
    "ExecutionGraph",
    "ExecutionNode",
    "NodeState",
    "NodeStatus",
    "Run",
    "RunStatus",
    "TERMINAL_NODE",
    "TERMINAL_RUN",
    "graph_from_capability",
    "graph_from_workflow",
    "scratch_workdir",
]
