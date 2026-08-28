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
- **Retry with backoff.** ``max_attempts``, ``retry_backoff_seconds``, a growth
  multiplier and an absolute cap are per node. A retry gets a fresh
  :class:`~loomcraft.context.NodeContext` with an incremented ``attempt``.
- **Timeouts.** A node that exceeds ``timeout_seconds`` is cancelled and treated
  as a (possibly retryable) failure.
- **Human approval.** ``requires_approval`` parks a node *before* its runner is
  invoked, so the gate sits in front of the side effect rather than behind it. A
  runner may also return ``waiting_approval`` to pause mid-work. Either way the
  run parks in ``paused_approval`` until :meth:`Run.approve` resolves it.
- **Artifacts are promoted, not streamed.** A node's emitted files are only
  registered once the attempt that produced them succeeded, so a partial write
  from a failed try never becomes a deliverable or a downstream input.
- **Failure policy.** ``on_failure="continue"`` lets independent dependents run
  even though this node failed — for the branch whose emptiness is itself a
  result.
- **Stall detection.** Nothing runnable, nothing in flight, and not everything
  terminal is treated as a bug and fails the run — never as success.
- **Cancellation is awaited.** :meth:`Run.cancel` does not return until every
  node task has actually stopped, so a cancelled run leaves nothing writing
  behind it.
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import json
import secrets
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Literal, Mapping, Sequence

from .context import EmittedArtifact, InputFile, LogLevel, NodeContext, NodeResult
from .errors import RegistryError, SourceError
from .graph import layers, validate as validate_graph
from .registry import Capability, Registry, Workflow
from .store import Session, public_artifact

NodeStatus = Literal[
    "pending", "running", "waiting_approval", "succeeded", "failed", "skipped", "cancelled"
]
RunStatus = Literal[
    "created", "running", "paused_approval", "succeeded", "failed", "cancelled"
]

TERMINAL_NODE: frozenset[str] = frozenset({"succeeded", "failed", "skipped", "cancelled"})
TERMINAL_RUN: frozenset[str] = frozenset({"succeeded", "failed", "cancelled"})

#: A node's ``detail`` is echoed into the model's context and persisted in the
#: event log. Both have budgets, so an accidental data dump fails the node
#: rather than the session.
MAX_RESULT_DETAIL_BYTES = 256 * 1024


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
    retry_backoff_multiplier: float = 2.0
    #: Absolute ceiling on one backoff interval. ``None`` means uncapped.
    retry_max_backoff_seconds: float | None = None
    #: Park this node at a human gate *before* invoking its runner.
    requires_approval: bool = False
    #: What this node's failure does to its dependents.
    on_failure: Literal["stop", "continue", "require_approval"] = "stop"
    #: A runner supplied by the graph builder instead of looked up in the
    #: registry. This is what lets the plan adapter execute dynamic/review/
    #: answer steps without registering throwaway runners globally.
    runner_fn: Callable[[NodeContext], Any] | None = None
    #: ``{upstream_port_name: local_input_key}``. Without it an upstream
    #: artifact binds under its own port name, which only works when producer
    #: and consumer happened to agree on a word.
    input_ports: Mapping[str, str] = field(default_factory=dict)
    #: ``{input_key: (".csv", ...)}`` enforced at bind time, so a capability's
    #: declared file types hold even for files arriving from upstream.
    input_extensions: Mapping[str, tuple[str, ...]] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ExecutionGraph:
    """A validated, ready-to-run DAG."""

    id: str
    name: str
    nodes: tuple[ExecutionNode, ...]
    kind: Literal["capability", "workflow", "plan"] = "capability"
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
        input_ports={item.effective_port: item.key for item in capability.inputs},
        input_extensions={
            item.key: item.allowed_extensions for item in capability.inputs
        },
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
                input_extensions={
                    item.key: item.allowed_extensions
                    for item in workflow.inputs
                    if item.key in spec.inputs
                },
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
            "artifacts": [public_artifact(item) for item in self.artifacts],
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
        #: Set when this graph was launched to satisfy one step of a published
        #: plan. The engine stays graph-oriented; the broker uses this to
        #: project state back onto the plan and to route approvals.
        self.plan_step_id: str | None = None

        self._engine = engine
        self._wake = asyncio.Event()
        self._done = asyncio.Event()
        #: Set when the run is terminal *or* parked at an approval gate — the
        #: two states in which it will not progress without outside input.
        self._settled = asyncio.Event()
        self._cancel = asyncio.Event()
        self._driver: asyncio.Task[None] | None = None
        self._tasks: set[asyncio.Task[None]] = set()
        self._approvals: dict[str, bool] = {}
        #: Nodes whose pre-execution approval gate a human has already cleared.
        self._preapproved: set[str] = set()

    # ── Public control surface ──────────────────────────────────────────────

    async def wait(self) -> "Run":
        """Await a terminal status.

        A run parked at an approval gate is *not* terminal, so this will block
        until someone approves it. Use :meth:`settled` when you want to hand
        control back at the gate instead.
        """
        await self._done.wait()
        return self

    async def settled(self) -> "Run":
        """Await either a terminal status or an approval pause.

        This is what a caller almost always wants: an approval gate is a
        request for input, and blocking on it from the same task that would
        deliver that input is a deadlock.
        """
        await self._settled.wait()
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
        """Every artifact this run produced, each listed once.

        A composite node adopts its nested run's artifacts, so the same record
        can appear on two nodes; the reader should see one file, not two.
        """
        collected: list[dict[str, Any]] = []
        seen: set[str] = set()
        for state in self.nodes.values():
            for artifact in state.artifacts:
                identity = str(artifact.get("id") or artifact.get("source_ref") or "")
                if identity and identity in seen:
                    continue
                if identity:
                    seen.add(identity)
                collected.append(artifact)
        return collected

    @property
    def failed_nodes(self) -> list[dict[str, Any]]:
        """Every node that failed, tolerated or not.

        ``tolerated`` marks a node whose failure the graph explicitly allowed
        via ``on_failure="continue"``. Such a failure is still reported — it is
        just not treated as the run's verdict.
        """
        policies = {node.id: node.on_failure for node in self.graph.nodes}
        return [
            {
                "node_id": state.id,
                "error": state.error,
                "attempts": state.attempts,
                "tolerated": policies.get(state.id) == "continue",
            }
            for state in self.nodes.values()
            if state.status == "failed"
        ]

    @property
    def blocking_failures(self) -> list[dict[str, Any]]:
        """Failures that should make the whole run count as failed."""
        return [row for row in self.failed_nodes if not row["tolerated"]]

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
            "artifacts": [public_artifact(item) for item in self.artifacts],
        }

    # ── Internals ───────────────────────────────────────────────────────────

    def _finish(self, status: RunStatus) -> None:
        if self.status in TERMINAL_RUN:
            return
        self.status = status
        self.finished_at = time.monotonic()
        self._done.set()
        self._settled.set()


class Engine:
    """Drives execution graphs against a registry and a session."""

    def __init__(
        self,
        registry: Registry,
        session: Session,
        *,
        max_parallel: int = 8,
        max_retained_runs: int = 128,
        emit: Callable[[str, Mapping[str, Any]], Any] | None = None,
        stream_logs: bool = False,
    ) -> None:
        self.registry = registry
        self.session = session
        self.max_parallel = max(1, max_parallel)
        #: Finished runs are kept so a late approval or a UI refresh can still
        #: read them, but a long-lived server must not accumulate them forever.
        self.max_retained_runs = max(1, int(max_retained_runs))
        self._emit = emit or (lambda name, data: session.emit(name, data))
        self.stream_logs = stream_logs
        self._runs: dict[str, Run] = {}
        self._semaphore = asyncio.Semaphore(self.max_parallel)

    # ── Public API ──────────────────────────────────────────────────────────

    def submit(self, graph: ExecutionGraph, *, run_id: str | None = None) -> Run:
        """Start a run in the background and return its handle immediately."""
        self._prune_runs()
        missing = [
            node.runner
            for node in graph.nodes
            if node.runner_fn is None and not self.registry.has_runner(node.runner)
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

    def _prune_runs(self) -> None:
        """Drop the oldest terminal runs once the retention budget is reached.

        Only terminal runs are eligible — a paused or running handle is still
        someone's live state.
        """
        excess = len(self._runs) - self.max_retained_runs + 1
        if excess <= 0:
            return
        for identifier, run in list(self._runs.items()):
            if excess <= 0:
                break
            if run.status in TERMINAL_RUN:
                self._runs.pop(identifier, None)
                excess -= 1

    async def execute(self, graph: ExecutionGraph, *, run_id: str | None = None) -> Run:
        """Submit a graph and return once it settles.

        Returns at a terminal status, or at an approval pause — check
        ``run.status``. Blocking through a gate would deadlock the caller that
        is supposed to resolve it.
        """
        run = self.submit(graph, run_id=run_id)
        try:
            await run.settled()
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
                self._gate_ready_approvals(run)
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
                    run._settled.set()
                elif (runnable or in_flight) and run.status == "paused_approval":
                    run.status = "running"
                    run._settled.clear()

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
        policies = {node.id: node.on_failure for node in run.graph.nodes}
        ready: list[str] = []
        for node in run.graph.nodes:
            if statuses[node.id] != "pending":
                continue
            if self._dependencies_ready(node, statuses, policies):
                ready.append(node.id)
        return ready

    @staticmethod
    def _dependencies_ready(
        node: ExecutionNode,
        statuses: Mapping[str, str],
        policies: Mapping[str, str],
    ) -> bool:
        """Success clears a dependency; so does a failure marked ``continue``."""
        return all(
            statuses.get(dep) == "succeeded"
            or (
                statuses.get(dep) in {"failed", "skipped"}
                and policies.get(dep) == "continue"
            )
            for dep in node.depends_on
        )

    def _gate_ready_approvals(self, run: Run) -> None:
        """Park approval-gated nodes before their runner can do anything.

        This is the difference between "a human confirmed the result" and "a
        human authorised the action". A node declaring ``requires_approval``
        wants the second, so it must not be dispatched until someone says yes.
        """
        statuses = {key: state.status for key, state in run.nodes.items()}
        policies = {node.id: node.on_failure for node in run.graph.nodes}
        for node in run.graph.nodes:
            state = run.nodes[node.id]
            if (
                state.status == "pending"
                and node.requires_approval
                and node.id not in run._preapproved
                and self._dependencies_ready(node, statuses, policies)
            ):
                state.status = "waiting_approval"
                state.error = f"approval required before executing {node.name}"
                self._emit(
                    "execution_progress",
                    {
                        "execution_id": run.id,
                        "node_id": node.id,
                        "status": "waiting_approval",
                        "attempt": 0,
                    },
                )

    def _propagate_skips(self, run: Run) -> None:
        policies = {node.id: node.on_failure for node in run.graph.nodes}
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
                    and policies.get(dep) != "continue"
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
            node = run.graph.node(node_id)
            if approved and node.requires_approval and state.attempts == 0:
                # A pre-execution gate: approval means "now go and do it",
                # not "call it done". Release the node back to the scheduler.
                run._preapproved.add(node_id)
                state.status = "pending"
                state.error = None
                state.finished_at = None
            else:
                # A runner asked mid-flight, or the reviewer said no.
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
        elif run.blocking_failures:
            final = "failed"
        elif any(status == "cancelled" for status in statuses):
            final = "cancelled"
        else:
            # A node that failed under ``on_failure="continue"`` does not fail
            # the run: the plan author declared that outcome acceptable, and
            # returning "failed" anyway would make the policy pointless — the
            # agent would replan exactly as if it had said "stop". The failure
            # is still on the node and in ``failed_nodes``.
            final = "succeeded"
        run._finish(final)

    # ── Node execution ──────────────────────────────────────────────────────

    async def _run_node(self, run: Run, node: ExecutionNode) -> None:
        state = run.nodes[node.id]
        runner = node.runner_fn or self.registry.runner(node.runner)
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

                # Buffered, not registered: a retryable failure must not leave
                # a half-written file behind as a deliverable.
                emitted: list[EmittedArtifact] = []
                adopted: list[dict[str, Any]] = []
                config = dict(node.config)
                if node.id in run._preapproved:
                    config["approved"] = True

                ctx = NodeContext(
                    run_id=run.id,
                    node_id=node.id,
                    attempt=attempt,
                    inputs=inputs,
                    dependencies={
                        dependency: run.nodes[dependency].detail
                        for dependency in node.depends_on
                        if dependency in run.nodes
                    },
                    parameters=dict(node.parameters),
                    config=config,
                    workdir=self.session.run_dir(run.id) / node.id / f"attempt-{attempt}",
                    outputs=node.outputs,
                    on_log=self._node_logger(run),
                    on_progress=self._node_progress(run),
                    on_artifact=emitted.append,
                    on_adopted_artifact=adopted.append,
                    cancel_event=run._cancel,
                )

                try:
                    async with self._semaphore:

                        async def invoke(
                            call: Any = runner, context: NodeContext = ctx
                        ) -> NodeResult:
                            # A graph-supplied runner_fn may be a plain
                            # function; registry runners are always async.
                            value = call(context)
                            return await value if inspect.isawaitable(value) else value

                        if node.timeout_seconds:
                            result = await asyncio.wait_for(
                                invoke(), timeout=node.timeout_seconds
                            )
                        else:
                            result = await invoke()
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

                result = self._checked_detail(result)

                if result.status in {"succeeded", "waiting_approval"}:
                    # The attempt stands, so its outputs become real.
                    try:
                        for record in adopted:
                            self._adopt_artifact(state, record)
                        self._register_node_artifacts(run, node, state, emitted)
                    except Exception as exc:  # noqa: BLE001 - artifact boundary
                        result = NodeResult.fail(
                            f"artifact registration failed: {type(exc).__name__}: {exc}"
                        )

                state.detail = dict(result.detail)
                if result.status == "succeeded":
                    state.status = "succeeded"
                    state.error = None
                    break
                if result.status == "waiting_approval":
                    state.status = "waiting_approval"
                    state.error = result.error
                    break
                if result.status == "skipped":
                    state.status = "skipped"
                    state.error = result.error
                    break

                state.error = result.error or "node failed"
                should_retry = result.retryable and attempt < attempts and not run.cancelled
                if not should_retry:
                    # ``require_approval`` turns a terminal failure into a
                    # decision rather than an outcome: a human sees the error
                    # and chooses whether the graph carries on.
                    state.status = (
                        "waiting_approval"
                        if node.on_failure == "require_approval"
                        else "failed"
                    )
                    break
                delay = max(0.0, node.retry_backoff_seconds) * (
                    max(1.0, node.retry_backoff_multiplier) ** (attempt - 1)
                )
                if node.retry_max_backoff_seconds is not None:
                    delay = min(max(0.0, node.retry_max_backoff_seconds), delay)
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

        Upstream artifacts arrive keyed by their emitting port name, remapped
        through ``input_ports`` when producer and consumer use different words
        for the same file. Everything goes through ``resolve_source`` so a file
        is re-verified immediately before the node that will read it.
        """
        bound: dict[str, list[InputFile]] = {}
        for key, refs in node.inputs.items():
            files: list[InputFile] = []
            for ref in refs:
                resolved = self.session.resolve_source(ref)
                self._validate_input_extension(node, key, resolved.filename)
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
                key = str(node.input_ports.get(port, port))
                source_ref = str(artifact.get("source_ref") or "")
                if not source_ref:
                    raise SourceError("upstream artifact is missing its source reference")
                resolved = self.session.resolve_source(source_ref)
                self._validate_input_extension(node, key, resolved.filename)
                bound.setdefault(key, []).append(
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
        return bound

    @staticmethod
    def _validate_input_extension(node: ExecutionNode, key: str, filename: str) -> None:
        """Hold a capability's declared file types for upstream files too."""
        allowed = tuple(node.input_extensions.get(key, ()))
        if allowed and not filename.casefold().endswith(
            tuple(extension.casefold() for extension in allowed)
        ):
            raise SourceError(
                f"input {key!r} requires one of: " + ", ".join(allowed)
            )

    @staticmethod
    def _checked_detail(result: NodeResult) -> NodeResult:
        """Fail the node when its detail cannot be persisted or streamed."""
        try:
            encoded = json.dumps(
                result.detail, ensure_ascii=False, allow_nan=False
            ).encode("utf-8")
        except (TypeError, ValueError):
            return NodeResult.fail("node result detail is not JSON-serializable")
        if len(encoded) > MAX_RESULT_DETAIL_BYTES:
            return NodeResult.fail(
                "node result detail exceeds the "
                f"{MAX_RESULT_DETAIL_BYTES // 1024} KiB limit; "
                "emit an artifact instead"
            )
        return result

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

    def _register_node_artifacts(
        self,
        run: Run,
        node: ExecutionNode,
        state: NodeState,
        artifacts: Sequence[EmittedArtifact],
    ) -> None:
        """Promote a successful attempt's emitted files into session artifacts.

        Called only once the attempt has succeeded or parked for approval. A
        retry may write a partial file before discovering a transient failure;
        deferring registration keeps those bytes out of downstream bindings and
        out of the user's deliverables.
        """
        for artifact in artifacts:
            record = self.session.add_artifact(
                artifact.path,
                port_name=artifact.port_name,
                display_name=artifact.filename,
                content_type=artifact.content_type,
                step_id=(
                    run.plan_step_id
                    or (node.id if run.graph.kind == "plan" else None)
                ),
                run_id=run.id,
                node_id=node.id,
            )
            state.artifacts.append(record)
            self._emit("artifact_registered", {"artifact": public_artifact(record)})

    def _adopt_artifact(self, state: NodeState, record: Mapping[str, Any]) -> None:
        """Attach an artifact a nested run already registered to this node."""
        source_ref = str(record.get("source_ref") or "")
        if not source_ref:
            raise SourceError("adopted artifact is missing its source reference")
        self.session.resolve_source(source_ref)
        identity = str(record.get("id") or source_ref)
        if any(
            str(item.get("id") or item.get("source_ref")) == identity
            for item in state.artifacts
        ):
            return
        state.artifacts.append(dict(record))


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
    "MAX_RESULT_DETAIL_BYTES",
    "graph_from_capability",
    "graph_from_workflow",
    "scratch_workdir",
]
