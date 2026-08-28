"""Asynchronous DAG scheduler with bounded parallelism and retries."""

from __future__ import annotations

import asyncio
import copy
import inspect
import json
import math
import secrets
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Mapping, Optional, Set

from .events import Event, utc_now
from .errors import ExecutionError as CoreExecutionError
from .models import (
    Plan,
    PlanStep,
    PlanValidationError,
    update_step,
    validate_plan,
)
from .registry import CapabilitySpec, Registry, StepResult, invoke_handler
from .legacy_storage import InMemoryStore, SessionStore


class ExecutionError(CoreExecutionError):
    """A run could not be completed."""


class ApprovalRequired(Exception):
    """A handler can raise this to pause a run at an approval boundary."""

    def __init__(self, message: str = "approval required", payload: Optional[Mapping[str, Any]] = None) -> None:
        super().__init__(message)
        self.payload = dict(payload or {})


@dataclass
class StepContext:
    session_id: str
    run_id: str
    plan: Dict[str, Any]
    step: Dict[str, Any]
    inputs: Dict[str, Any]
    dependencies: Dict[str, Any]
    attempt: int
    cancel_event: asyncio.Event
    emit: Callable[[str, Dict[str, Any]], Event]
    parameters: Dict[str, Any] = field(default_factory=dict)

    def cancelled(self) -> bool:
        return self.cancel_event.is_set()

    def log(self, message: str, *, level: str = "info", **data: Any) -> None:
        """Emit a structured, renderer-friendly handler log event."""
        self.emit("step_log", {"level": level, "message": str(message)[:4000], **data})

    def progress(self, fraction: Optional[float] = None, message: Optional[str] = None, **data: Any) -> None:
        payload: Dict[str, Any] = dict(data)
        if fraction is not None:
            payload["fraction"] = max(0.0, min(1.0, float(fraction)))
        if message is not None:
            payload["message"] = str(message)[:1000]
        self.emit("step_progress", payload)


@dataclass
class StepOutcome:
    step_id: str
    status: str
    result: Optional[StepResult] = None
    attempts: int = 0
    error: Optional[str] = None
    error_type: Optional[str] = None
    started_at: float = field(default_factory=time.monotonic)
    ended_at: float = field(default_factory=time.monotonic)
    approval: Optional[Dict[str, Any]] = None

    def as_dict(self) -> Dict[str, Any]:
        return {
            "step_id": self.step_id,
            "status": self.status,
            "attempts": self.attempts,
            "error": self.error,
            "error_type": self.error_type,
            "output": self.result.output if self.result is not None else None,
            "duration_seconds": round(max(0.0, self.ended_at - self.started_at), 3),
            "artifacts": list(self.result.artifacts if self.result else []),
        }


@dataclass
class ExecutionResult:
    run_id: str
    session_id: str
    status: str
    steps: Dict[str, StepOutcome]
    started_at: str
    finished_at: Optional[str] = None
    error: Optional[str] = None
    revision: Optional[int] = None

    def as_dict(self) -> Dict[str, Any]:
        failed_nodes = [
            {"node_id": outcome.step_id, "error": outcome.error, "error_type": outcome.error_type}
            for outcome in self.steps.values()
            if outcome.status == "failed"
        ]
        return {
            "kind": "dag",
            "id": self.run_id,
            "session_id": self.session_id,
            "status": self.status,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "error": self.error,
            "revision": self.revision,
            "failed_nodes": failed_nodes,
            "steps": {key: value.as_dict() for key, value in self.steps.items()},
            "artifacts": [
                artifact
                for outcome in self.steps.values()
                if outcome.result is not None
                for artifact in outcome.result.artifacts
            ],
        }

    def to_json(self) -> str:
        return json.dumps(self.as_dict(), ensure_ascii=False, allow_nan=False)


@dataclass
class _RunControl:
    cancel_event: asyncio.Event
    task: Optional[asyncio.Task] = None
    result: Optional[ExecutionResult] = None
    waiting_step: Optional[str] = None
    wakeup: Optional[asyncio.Event] = None
    active: bool = True
    scheduler: bool = True


class DAGExecutor:
    """Execute a validated plan and stream every state transition.

    Handlers are looked up from :class:`~loomcraft.registry.Registry`.  They
    may be synchronous or asynchronous and receive a ``StepContext``.  The
    scheduler never knows anything about a domain's files, database, or tools.
    """

    def __init__(
        self,
        registry: Registry,
        *,
        store: Optional[SessionStore] = None,
        on_event: Optional[Callable[[Dict[str, Any]], None]] = None,
        max_concurrency: int = 4,
        run_sync_in_thread: bool = False,
    ) -> None:
        if isinstance(max_concurrency, bool) or not isinstance(max_concurrency, int) or max_concurrency < 1:
            raise ValueError("max_concurrency must be positive")
        self.registry = registry
        self.store = store or InMemoryStore()
        self.on_event = on_event
        self.max_concurrency = max_concurrency
        self.run_sync_in_thread = bool(run_sync_in_thread)
        self._runs: Dict[str, _RunControl] = {}
        self._run_sessions: Dict[str, str] = {}
        self._session_locks: Dict[str, asyncio.Lock] = {}
        self._plan_locks: Dict[str, asyncio.Lock] = {}

    def _lock_for(self, collection: Dict[str, asyncio.Lock], key: str) -> asyncio.Lock:
        lock = collection.get(key)
        if lock is None:
            lock = asyncio.Lock()
            collection[key] = lock
        return lock

    def _ensure_session(self, session_id: str) -> None:
        ensure = getattr(self.store, "ensure_session", None)
        if callable(ensure):
            ensure(session_id)

    def _update_session(self, session_id: str, **fields: Any) -> None:
        update = getattr(self.store, "update_session", None)
        if callable(update):
            update(session_id, **fields)

    def _emit(self, session_id: str, event: str, data: Mapping[str, Any]) -> Event:
        self._ensure_session(session_id)
        row = self.store.append_event(session_id, event, dict(data))
        if isinstance(row, Event):
            outgoing = row.as_dict()
        elif isinstance(row, Mapping):
            outgoing = dict(row)
            if "data" not in outgoing:
                outgoing["data"] = dict(data)
        else:
            outgoing = {"event": event, "data": dict(data)}
        if self.on_event is not None:
            try:
                self.on_event(outgoing)
            except Exception:
                # Observers are not part of execution correctness.
                pass
        return row if isinstance(row, Event) else Event(int(outgoing.get("seq", 1)), str(outgoing.get("event", event)), dict(outgoing.get("data", data)), outgoing.get("ts"))

    async def _set_step(
        self,
        session_id: str,
        step_id: str,
        status: str,
        *,
        summary: Optional[str] = None,
        execution: Optional[Mapping[str, Any]] = None,
    ) -> Dict[str, Any]:
        lock = self._lock_for(self._plan_locks, session_id)
        async with lock:
            current = self.store.get_current_plan(session_id)
            if current is None:
                raise PlanValidationError("publish a task plan before execution")
            updated = update_step(current, step_id, status, summary=summary, execution=execution)
            if isinstance(execution, Mapping) and isinstance(execution.get("attempts"), int):
                for row in updated.get("steps", []):
                    if row.get("id") == step_id:
                        row["attempts"] = int(execution["attempts"])
            self.store.update_current_plan(session_id, updated)
            step = next(item for item in updated["steps"] if item["id"] == step_id)
        self._emit(session_id, "step_updated", {"revision": updated["revision"], "step": step})
        return step

    def _handler_for(self, step: PlanStep) -> Optional[Callable[[StepContext], Any]]:
        if step.kind == "capability":
            spec = self.registry.capability(step.capability or "")
            return spec.handler if spec is not None else None
        if step.kind == "workflow":
            spec = self.registry.workflow(step.capability or "")
            return spec.handler if spec is not None else None
        if step.kind == "review" and step.capability:
            spec = self.registry.capability(step.capability)
            return spec.handler if isinstance(spec, CapabilitySpec) else None
        handler = self.registry.handler_for(step.kind)
        if handler is not None:
            return handler
        if step.kind == "answer":
            return lambda context: StepResult(
                output={"message": context.step.get("description") or context.step.get("title")},
                summary=context.step.get("description") or context.step.get("title"),
            )
        if step.kind == "review":
            def default_review(context: StepContext) -> StepResult:
                raise ApprovalRequired(
                    "manual approval is required for %s" % context.step.get("title", context.step.get("id", "review")),
                    {"step_id": context.step.get("id")},
                )
            return default_review
        return None

    def _validate_registered_steps(self, plan: Plan) -> None:
        for step in plan.steps:
            if step.kind == "capability" and self.registry.capability(step.capability or "") is None:
                raise PlanValidationError("capability step must reference a registered capability")
            if step.kind == "workflow" and self.registry.workflow(step.capability or "") is None:
                raise PlanValidationError("workflow step must reference a registered workflow")
            if step.kind == "review" and step.capability:
                spec = self.registry.capability(step.capability)
                if not isinstance(spec, CapabilitySpec) or spec.handler is None:
                    raise PlanValidationError("review step capability must have a registered handler")

    def _historical_outputs(self, session_id: str, revision: int) -> Dict[str, Any]:
        outputs: Dict[str, Any] = {}
        if not hasattr(self.store, "list_executions"):
            return outputs
        for execution in self.store.list_executions(session_id)[::-1]:
            if not isinstance(execution, Mapping):
                continue
            recorded_revision = execution.get("revision")
            if recorded_revision is not None and recorded_revision != revision:
                continue
            direct_step = execution.get("step_id")
            if isinstance(direct_step, str) and direct_step not in outputs and execution.get("status") == "succeeded":
                outputs[direct_step] = execution.get("output")
            recorded_steps = execution.get("steps")
            if not isinstance(recorded_steps, Mapping):
                continue
            for step_id, recorded in recorded_steps.items():
                if step_id in outputs or not isinstance(recorded, Mapping):
                    continue
                if recorded.get("status") == "succeeded":
                    outputs[step_id] = recorded.get("output")
        return outputs

    async def _prepare_artifacts(
        self,
        session_id: str,
        step_id: str,
        artifacts: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        prepared: List[Dict[str, Any]] = []
        for raw in artifacts:
            if not isinstance(raw, Mapping):
                continue
            artifact = copy.deepcopy(dict(raw))
            try:
                if len(json.dumps(artifact, ensure_ascii=False, default=str).encode("utf-8")) > 256 * 1024:
                    raise ValueError("artifact metadata exceeds the size limit")
            except (TypeError, ValueError) as exc:
                raise ExecutionError("artifact metadata is not serializable or is too large") from exc
            artifact.setdefault("id", "art-%s" % secrets.token_hex(8))
            artifact.setdefault("step_id", step_id)
            artifact.setdefault("created_at", utc_now())
            register = getattr(self.store, "register_artifact", None)
            if callable(register):
                registered = register(session_id, artifact)
                if isinstance(registered, Mapping):
                    artifact = dict(registered)
            prepared.append(artifact)
        return prepared

    async def _run_one(
        self,
        session_id: str,
        run_id: str,
        plan: Dict[str, Any],
        step: PlanStep,
        inputs: Dict[str, Any],
        dependencies: Dict[str, Any],
        cancel_event: asyncio.Event,
        parameters: Optional[Dict[str, Any]] = None,
    ) -> StepOutcome:
        started = time.monotonic()
        handler = self._handler_for(step)
        if handler is None:
            error = "no handler registered for %s step %s" % (step.kind, step.id)
            await self._set_step(
                session_id,
                step.id,
                "failed",
                summary=error,
                execution={"run_id": run_id, "attempts": 0, "status": "failed", "error": error},
            )
            return StepOutcome(
                step_id=step.id,
                status="failed",
                attempts=0,
                error=error,
                error_type="HandlerNotFound",
                started_at=started,
                ended_at=time.monotonic(),
            )
        attempts = 0
        last_error: Optional[BaseException] = None
        for attempt in range(1, step.retry.max_attempts + 1):
            attempts = attempt
            if cancel_event.is_set():
                error = "execution cancelled"
                await self._set_step(
                    session_id,
                    step.id,
                    "cancelled",
                    summary=error,
                    execution={"run_id": run_id, "attempts": attempt - 1, "status": "cancelled"},
                )
                return StepOutcome(
                    step_id=step.id,
                    status="cancelled",
                    attempts=attempt - 1,
                    error="execution cancelled",
                    error_type="CancelledError",
                    started_at=started,
                    ended_at=time.monotonic(),
                )
            await self._set_step(session_id, step.id, "running", summary="running (attempt %d/%d)" % (attempt, step.retry.max_attempts))
            self._emit(
                session_id,
                "step_attempt",
                {"run_id": run_id, "step_id": step.id, "attempt": attempt, "max_attempts": step.retry.max_attempts},
            )
            context = StepContext(
                session_id=session_id,
                run_id=run_id,
                plan=copy.deepcopy(plan),
                step=step.to_dict(),
                inputs=copy.deepcopy(inputs),
                dependencies=copy.deepcopy(dependencies),
                attempt=attempt,
                cancel_event=cancel_event,
                emit=lambda name, data: self._emit(session_id, name, {**dict(data), "run_id": run_id, "step_id": step.id}),
                parameters=copy.deepcopy(parameters or {}),
            )
            try:
                if self.run_sync_in_thread and not inspect.iscoroutinefunction(handler):
                    async def threaded_invocation(
                        current_handler=handler,
                        current_context=context,
                    ):
                        value = await asyncio.to_thread(current_handler, current_context)
                        if inspect.isawaitable(value):
                            value = await value
                        return StepResult.from_value(value)
                    invocation = threaded_invocation()
                else:
                    invocation = invoke_handler(handler, context)
                if step.timeout_seconds is not None:
                    result = await asyncio.wait_for(invocation, timeout=step.timeout_seconds)
                else:
                    result = await invocation
                if result.status == "failed":
                    raise ExecutionError(result.error or result.summary or "handler returned failed")
                if result.status == "skipped":
                    await self._set_step(
                        session_id,
                        step.id,
                        "skipped",
                        summary=result.summary or "handler skipped this step",
                        execution={"run_id": run_id, "attempts": attempt, "status": "skipped"},
                    )
                    return StepOutcome(
                        step_id=step.id,
                        status="skipped",
                        result=result,
                        attempts=attempt,
                        started_at=started,
                        ended_at=time.monotonic(),
                    )
                try:
                    json.dumps(result.output, ensure_ascii=False, allow_nan=False)
                    json.dumps(result.metadata, ensure_ascii=False, allow_nan=False)
                except (TypeError, ValueError) as exc:
                    raise ExecutionError("step result must be JSON serializable") from exc
                result.artifacts = await self._prepare_artifacts(session_id, step.id, result.artifacts)
                for artifact in result.artifacts:
                    self._emit(session_id, "artifact_registered", {"run_id": run_id, "step_id": step.id, "artifact": artifact})
                result_summary = result.summary or "%s completed" % step.title
                await self._set_step(
                    session_id,
                    step.id,
                    "succeeded",
                    summary=result_summary,
                    execution={"run_id": run_id, "attempts": attempt, "status": "succeeded"},
                )
                return StepOutcome(
                    step_id=step.id,
                    status="succeeded",
                    result=result,
                    attempts=attempt,
                    started_at=started,
                    ended_at=time.monotonic(),
                )
            except ApprovalRequired as exc:
                await self._set_step(
                    session_id,
                    step.id,
                    "waiting_approval",
                    summary=str(exc),
                    execution={"run_id": run_id, "attempts": attempt, "status": "waiting_approval", **exc.payload},
                )
                return StepOutcome(
                    step_id=step.id,
                    status="waiting_approval",
                    attempts=attempt,
                    error=str(exc),
                    error_type="ApprovalRequired",
                    approval=dict(exc.payload),
                    started_at=started,
                    ended_at=time.monotonic(),
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - handler boundary
                last_error = exc
                if attempt < step.retry.max_attempts:
                    delay = step.retry.delay_for(attempt)
                    self._emit(
                        session_id,
                        "step_retry",
                        {
                            "run_id": run_id,
                            "step_id": step.id,
                            "attempt": attempt,
                            "next_attempt": attempt + 1,
                            "delay_seconds": delay,
                            "error": str(exc)[:1000],
                        },
                    )
                    if delay:
                        try:
                            await asyncio.wait_for(cancel_event.wait(), timeout=delay)
                            await self._set_step(
                                session_id,
                                step.id,
                                "cancelled",
                                summary="execution cancelled during retry backoff",
                                execution={"run_id": run_id, "attempts": attempt, "status": "cancelled"},
                            )
                            return StepOutcome(
                                step_id=step.id,
                                status="cancelled",
                                attempts=attempt,
                                error="execution cancelled during retry backoff",
                                error_type="CancelledError",
                                started_at=started,
                                ended_at=time.monotonic(),
                            )
                        except asyncio.TimeoutError:
                            pass
                    continue
                break
        error = str(last_error)[:2000] if last_error is not None else "step failed"
        if step.on_failure == "require_approval":
            await self._set_step(
                session_id,
                step.id,
                "waiting_approval",
                summary=error,
                execution={"run_id": run_id, "attempts": attempts, "status": "waiting_approval", "error": error},
            )
            return StepOutcome(
                step_id=step.id,
                status="waiting_approval",
                attempts=attempts,
                error=error,
                error_type=type(last_error).__name__ if last_error is not None else None,
                approval={"error": error},
                started_at=started,
                ended_at=time.monotonic(),
            )
        await self._set_step(
            session_id,
            step.id,
            "failed",
            summary=error,
            execution={"run_id": run_id, "attempts": attempts, "status": "failed", "error": error},
        )
        return StepOutcome(
            step_id=step.id,
            status="failed",
            attempts=attempts,
            error=error,
            error_type=type(last_error).__name__ if last_error is not None else None,
            started_at=started,
            ended_at=time.monotonic(),
        )

    @staticmethod
    def _dependencies_allow(step: PlanStep, steps: Mapping[str, PlanStep]) -> bool:
        for dependency_id in step.depends_on:
            dependency = steps[dependency_id]
            if dependency.status == "succeeded":
                continue
            if dependency.status in ("failed", "skipped") and dependency.on_failure == "continue":
                continue
            return False
        return True

    @staticmethod
    def _dependency_blocks(step: PlanStep, steps: Mapping[str, PlanStep]) -> bool:
        for dependency_id in step.depends_on:
            dependency = steps[dependency_id]
            if dependency.status in ("failed", "cancelled", "skipped") and dependency.on_failure != "continue":
                return True
        return False

    async def execute(
        self,
        plan: Mapping[str, Any],
        *,
        session_id: str = "default",
        inputs: Optional[Mapping[str, Any]] = None,
        run_id: Optional[str] = None,
        reset: bool = False,
        timeout_seconds: Optional[float] = None,
    ) -> ExecutionResult:
        """Run all ready nodes, scheduling independent branches concurrently."""
        if timeout_seconds is not None and (isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float)) or not math.isfinite(float(timeout_seconds)) or timeout_seconds <= 0):
            raise ValueError("timeout_seconds must be a positive finite number")
        if isinstance(plan, Plan):
            plan = plan.to_dict()
        self._ensure_session(session_id)
        current = self.store.get_current_plan(session_id)
        if current is not None and plan is not current:
            # A broker-published plan is authoritative when the caller passes
            # an equivalent stale object.
            if current.get("revision") == plan.get("revision"):
                plan = current
        if reset:
            normalized = validate_plan(plan, registry=self.registry)
        else:
            parsed = Plan.from_raw(plan)
            normalized = parsed.to_dict()
            # A newly supplied plan may not have been published yet. Persist it
            # so step updates and events have one source of truth.
        parsed = Plan.from_raw(normalized)
        self._validate_registered_steps(parsed)
        run_id = run_id or "run-%s" % secrets.token_hex(8)
        if run_id in self._runs and self._runs[run_id].active:
            raise ExecutionError("run %s is already active" % run_id)
        if any(
            session == session_id
            and control.active
            for existing_id, control in self._runs.items()
            for session in [self._run_sessions.get(existing_id)]
        ):
            raise ExecutionError("session %s already has an active execution" % session_id)
        persist_plan = reset or current is None or current.get("revision") != normalized.get("revision")
        if persist_plan:
            self.store.update_current_plan(session_id, normalized)
            if current is None:
                self._emit(session_id, "plan_published", {"plan": normalized})
        control = _RunControl(cancel_event=asyncio.Event())
        control.task = asyncio.current_task()
        control.wakeup = asyncio.Event()
        self._runs[run_id] = control
        self._run_sessions[run_id] = session_id
        started_at = utc_now()
        outcome_map: Dict[str, StepOutcome] = {}
        self._update_session(session_id, status="running", active_run_id=run_id)
        self._emit(session_id, "execution_started", {"run_id": run_id, "execution_id": run_id, "execution_kind": "dag", "revision": parsed.revision})

        steps: Dict[str, PlanStep] = {step.id: step for step in parsed.steps}
        # A resumed run keeps completed nodes; a fresh plan starts pending.
        if reset:
            for step in steps.values():
                step.status = "pending"
        stale_running = [step for step in steps.values() if step.status == "running"]
        if stale_running:
            if any(existing_id != run_id and session == session_id and control.active for existing_id, control in self._runs.items() for session in [self._run_sessions.get(existing_id)]):
                raise ExecutionError("session %s already has an active execution" % session_id)
            for stale in stale_running:
                stale.status = "failed"
                try:
                    await self._set_step(session_id, stale.id, "failed", summary="previous execution was interrupted", execution={"run_id": run_id, "status": "interrupted"})
                except Exception:
                    pass
        pending: Set[str] = {step.id for step in steps.values() if step.status in ("pending", "ready")}
        running: Dict[asyncio.Task, str] = {}
        outputs: Dict[str, Any] = self._historical_outputs(session_id, parsed.revision)
        # Preserve terminal states when a host resumes a partially completed
        # run (for example after an approval decision). This also prevents a
        # second execute_plan call from disguising a prior failure as success.
        for existing in steps.values():
            if existing.status in ("succeeded", "failed", "skipped", "cancelled"):
                outcome_map[existing.id] = StepOutcome(existing.id, existing.status, error=(existing.summary if existing.status != "succeeded" else None))
        stop_requested = False
        waiting_step: Optional[str] = None
        input_map = dict(inputs or {})
        deadline = time.monotonic() + float(timeout_seconds) if timeout_seconds is not None else None

        async def mark_skipped(step: PlanStep, reason: str) -> None:
            step.status = "skipped"
            pending.discard(step.id)
            await self._set_step(session_id, step.id, "skipped", summary=reason, execution={"run_id": run_id, "status": "skipped"})
            outcome_map[step.id] = StepOutcome(step.id, "skipped", attempts=0, error=reason)

        try:
            while pending or running:
                if deadline is not None and time.monotonic() >= deadline:
                    control.cancel_event.set()
                    self._emit(session_id, "run_timeout", {"run_id": run_id, "timeout_seconds": timeout_seconds})
                if control.cancel_event.is_set():
                    for task in list(running):
                        task.cancel()
                    if running:
                        results = await asyncio.gather(*running, return_exceptions=True)
                        for task, result in zip(list(running), results):
                            step_id = running[task]
                            if isinstance(result, StepOutcome):
                                outcome_map[step_id] = result
                            if steps[step_id].status not in ("succeeded", "failed", "skipped", "cancelled"):
                                steps[step_id].status = "cancelled"
                                try:
                                    await self._set_step(
                                        session_id,
                                        step_id,
                                        "cancelled",
                                        summary="execution cancelled",
                                        execution={"run_id": run_id, "status": "cancelled"},
                                    )
                                except Exception:
                                    pass
                    for step_id in list(pending):
                        step = steps[step_id]
                        step.status = "cancelled"
                        await self._set_step(session_id, step_id, "cancelled", summary="execution cancelled", execution={"run_id": run_id, "status": "cancelled"})
                        outcome_map[step_id] = StepOutcome(step_id, "cancelled", error="execution cancelled")
                        pending.discard(step_id)
                    running.clear()
                    break

                # Mark nodes whose dependency failed under a blocking policy.
                for step_id in list(pending):
                    step = steps[step_id]
                    if self._dependency_blocks(step, steps):
                        await mark_skipped(step, "blocked by a failed dependency")

                if stop_requested and waiting_step is None:
                    for step_id in list(pending):
                        await mark_skipped(steps[step_id], "stopped after a step failure")
                elif waiting_step is None:
                    capacity = max(0, self.max_concurrency - len(running))
                    ready = [
                        steps[step_id]
                        for step_id in pending
                        if self._dependencies_allow(steps[step_id], steps)
                    ]
                    ready.sort(key=lambda item: item.id)
                    for step in ready[:capacity]:
                        pending.discard(step.id)
                        step.status = "running"
                        dependencies = {dependency: outputs.get(dependency) for dependency in step.depends_on}
                        step_inputs = input_map.get(step.id, input_map) if isinstance(input_map.get(step.id, input_map), Mapping) else input_map
                        raw_parameters = step.metadata.get("parameters", {}) if isinstance(step.metadata, Mapping) else {}
                        step_parameters = dict(raw_parameters) if isinstance(raw_parameters, Mapping) else {}
                        task = asyncio.create_task(
                            self._run_one(
                                session_id,
                                run_id,
                                normalized,
                                step,
                                dict(step_inputs),
                                dependencies,
                                control.cancel_event,
                                step_parameters,
                            )
                        )
                        running[task] = step.id

                if not running:
                    if pending:
                        waiting = [step for step in steps.values() if step.status == "waiting_approval"]
                        if waiting:
                            waiting_step = waiting[0].id
                            control.waiting_step = waiting_step
                            break
                        # A valid DAG should not deadlock; this guard protects
                        # custom status stores and produces an actionable event.
                        for step_id in list(pending):
                            await mark_skipped(steps[step_id], "no runnable dependencies")
                    continue

                # A cancellation request sets ``wakeup``. A short timeout keeps
                # cancellation responsive even when a third-party handler does
                # not expose its own cancellation primitive.
                done, _ = await asyncio.wait(
                    list(running),
                    timeout=0.25,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if not done:
                    continue
                for task in done:
                    step_id = running.pop(task)
                    try:
                        result = task.result()
                    except asyncio.CancelledError:
                        result = StepOutcome(step_id, "cancelled", error="execution cancelled")
                    except Exception as exc:  # noqa: BLE001 - scheduler boundary
                        result = StepOutcome(step_id, "failed", error=str(exc)[:2000], error_type=type(exc).__name__)
                    outcome_map[step_id] = result
                    steps[step_id].status = result.status
                    if result.result is not None:
                        outputs[step_id] = result.result.output
                    if result.status == "failed" and steps[step_id].on_failure == "stop":
                        stop_requested = True
                    if result.status == "waiting_approval":
                        waiting_step = step_id
                        control.waiting_step = step_id
                        # Pause immediately; leave downstream nodes pending so
                        # an approval decision can resume the same run.
                        stop_requested = False
                    progress = {identifier: item.status for identifier, item in steps.items()}
                    self._emit(session_id, "execution_progress", {"run_id": run_id, "execution_id": run_id, "execution_kind": "dag", "nodes": progress})

            if waiting_step is not None:
                status = "waiting_approval"
                error = outcome_map.get(waiting_step).error if waiting_step in outcome_map else "approval required"
            elif control.cancel_event.is_set() or any(item.status == "cancelled" for item in outcome_map.values()):
                status = "cancelled"
                error = "execution cancelled"
            elif any(item.status == "failed" for item in outcome_map.values()):
                status = "failed"
                error = "one or more DAG steps failed"
            else:
                status = "succeeded"
                error = None
        except asyncio.CancelledError:
            control.cancel_event.set()
            for task in list(running):
                task.cancel()
            await asyncio.gather(*running, return_exceptions=True)
            control.active = False
            self._runs.pop(run_id, None)
            self._run_sessions.pop(run_id, None)
            raise
        except Exception as exc:  # noqa: BLE001 - durable scheduler boundary
            status = "failed"
            error = str(exc)[:2000]
            for task in list(running):
                task.cancel()
            if running:
                await asyncio.gather(*running, return_exceptions=True)
                for task, step_id in list((task, running[task]) for task in running):
                    if steps[step_id].status not in ("succeeded", "failed", "skipped", "cancelled"):
                        try:
                            await self._set_step(session_id, step_id, "cancelled", summary="scheduler failed", execution={"run_id": run_id, "status": "cancelled"})
                        except Exception:
                            pass
            for step_id in list(pending):
                try:
                    await mark_skipped(steps[step_id], "scheduler error")
                except Exception:
                    pass
        finally:
            finished_at = utc_now()

        result = ExecutionResult(run_id, session_id, status, outcome_map, started_at, finished_at, error, parsed.revision)
        control.result = result
        control.active = False
        self.store.record_execution(session_id, result.as_dict())
        self._update_session(session_id, status=status, active_run_id=None)
        self._emit(session_id, "execution_finished", {"run_id": run_id, "execution": result.as_dict()})
        if status in ("succeeded", "failed", "cancelled"):
            self._runs.pop(run_id, None)
            self._run_sessions.pop(run_id, None)
        return result

    async def execute_step(
        self,
        session_id: str,
        step_id: str,
        *,
        inputs: Optional[Mapping[str, Any]] = None,
        expected_kind: Optional[str] = None,
        expected_capability: Optional[str] = None,
        parameters: Optional[Mapping[str, Any]] = None,
        run_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Run one server-authorized step (used by tool brokers)."""
        self._ensure_session(session_id)
        if any(
            session == session_id
            and control.active
            for existing_id, control in self._runs.items()
            for session in [self._run_sessions.get(existing_id)]
        ):
            raise ExecutionError("session %s already has an active execution" % session_id)
        current = self.store.get_current_plan(session_id)
        if current is None:
            raise PlanValidationError("publish a task plan before execution")
        parsed = Plan.from_raw(current)
        step = next((item for item in parsed.steps if item.id == step_id), None)
        if step is None:
            raise PlanValidationError("unknown plan step %r" % step_id)
        if expected_kind is not None and step.kind != expected_kind:
            raise PlanValidationError("step %r is %r, expected %r" % (step_id, step.kind, expected_kind))
        if expected_capability is not None and step.capability != expected_capability:
            raise PlanValidationError("step %r does not authorize %r" % (step_id, expected_capability))
        if step.status != "pending":
            raise PlanValidationError("step %r cannot start from %r" % (step_id, step.status))
        by_id = {item.id: item for item in parsed.steps}
        incomplete = [dependency for dependency in step.depends_on if by_id[dependency].status != "succeeded"]
        if incomplete:
            raise PlanValidationError("step %r has incomplete dependencies: %s" % (step_id, ", ".join(incomplete)))
        run_id = run_id or "run-%s" % secrets.token_hex(8)
        if run_id in self._runs and self._runs[run_id].active:
            raise ExecutionError("run %s is already active" % run_id)
        cancel_event = asyncio.Event()
        control = _RunControl(cancel_event=cancel_event, task=asyncio.current_task(), scheduler=False)
        self._runs[run_id] = control
        self._run_sessions[run_id] = session_id
        self._update_session(session_id, status="running", active_run_id=run_id)
        self._emit(session_id, "execution_started", {"run_id": run_id, "execution_kind": step.kind, "execution_id": run_id, "step_id": step_id, "capability": step.capability})
        historical_outputs = self._historical_outputs(session_id, parsed.revision)
        dependencies = {dependency: historical_outputs.get(dependency) for dependency in step.depends_on}
        try:
            outcome = await self._run_one(session_id, run_id, current, step, dict(inputs or {}), dependencies, cancel_event, dict(parameters or {}))
        except asyncio.CancelledError:
            cancel_event.set()
            try:
                await self._set_step(session_id, step_id, "cancelled", summary="execution cancelled", execution={"run_id": run_id, "status": "cancelled"})
            except Exception:
                pass
            control.active = False
            self._runs.pop(run_id, None)
            self._run_sessions.pop(run_id, None)
            raise
        except Exception as exc:  # noqa: BLE001 - single-step boundary
            error = str(exc)[:2000]
            try:
                await self._set_step(session_id, step_id, "failed", summary=error, execution={"run_id": run_id, "status": "failed", "error": error})
            except Exception:
                pass
            outcome = StepOutcome(step_id, "failed", attempts=0, error=error, error_type=type(exc).__name__)
        execution = {
            "kind": step.kind,
            "id": run_id,
            "revision": parsed.revision,
            "capability": step.capability,
            "status": outcome.status,
            "attempts": outcome.attempts,
            "duration_seconds": round(max(0.0, outcome.ended_at - outcome.started_at), 3),
            "error": outcome.error,
            "error_code": None if outcome.status == "succeeded" else "EXECUTION_STEP_FAILED",
            "artifacts": list(outcome.result.artifacts if outcome.result else []),
            "output": outcome.result.output if outcome.result else None,
        }
        self.store.record_execution(session_id, execution)
        self._update_session(session_id, status="idle", active_run_id=None)
        self._emit(session_id, "execution_finished", {"step_id": step_id, "execution": execution})
        self._runs.pop(run_id, None)
        self._run_sessions.pop(run_id, None)
        return execution

    async def execute_dag(
        self,
        dag: Mapping[str, Any],
        *,
        session_id: str = "default",
        inputs: Optional[Mapping[str, Any]] = None,
        revision: int = 1,
    ) -> ExecutionResult:
        """Validate a node/edge DAG and execute its loss-aware Plan conversion."""
        from .dag import plan_from_dag

        return await self.execute(
            plan_from_dag(dag, revision=revision),
            session_id=session_id,
            inputs=inputs,
            reset=True,
        )

    async def run_plan(self, plan: Mapping[str, Any], **kwargs: Any) -> ExecutionResult:
        """Semantic alias for execute, useful in host service code."""
        return await self.execute(plan, **kwargs)

    async def cancel(self, run_id: str) -> bool:
        control = self._runs.get(run_id)
        if control is None:
            return False
        if not control.active and control.result is not None and control.result.status == "waiting_approval":
            session_id = control.result.session_id
            if control.waiting_step:
                try:
                    await self._set_step(session_id, control.waiting_step, "cancelled", summary="approval cancelled", execution={"run_id": run_id, "status": "cancelled"})
                except Exception:
                    pass
            self._update_session(session_id, status="cancelled", active_run_id=None)
            execution = {"kind": "dag", "id": run_id, "session_id": session_id, "status": "cancelled", "error": "approval cancelled", "artifacts": []}
            self.store.record_execution(session_id, execution)
            self._emit(session_id, "execution_finished", {"run_id": run_id, "execution": execution})
            self._runs.pop(run_id, None)
            self._run_sessions.pop(run_id, None)
            return True
        control.cancel_event.set()
        if control.wakeup is not None:
            control.wakeup.set()
        if not control.scheduler and control.task is not None and not control.task.done():
            control.task.cancel()
        return True

    def active_runs(self, session_id: Optional[str] = None) -> List[str]:
        return [
            run_id
            for run_id, control in self._runs.items()
            if control.active and (session_id is None or self._run_sessions.get(run_id) == session_id)
        ]

    def known_runs(self, session_id: Optional[str] = None) -> List[str]:
        return [
            run_id
            for run_id, control in self._runs.items()
            if session_id is None or self._run_sessions.get(run_id) == session_id
        ]

    async def wait(self, run_id: str, timeout: Optional[float] = None) -> Optional[ExecutionResult]:
        """Wait for a scheduler-owned run after requesting cancellation."""
        control = self._runs.get(run_id)
        if control is None:
            return None
        task = control.task
        if task is not None and task is not asyncio.current_task() and not task.done():
            if timeout is None:
                await task
            else:
                await asyncio.wait_for(asyncio.shield(task), timeout=max(0.01, float(timeout)))
        return control.result

    async def approve(self, run_id: str, step_id: str, *, approved: bool = True, comment: str = "") -> Optional[ExecutionResult]:
        """Resolve an ``ApprovalRequired`` pause and resume the remaining DAG."""
        control = self._runs.get(run_id)
        if control is None or control.waiting_step != step_id or control.result is None or control.result.status != "waiting_approval":
            raise ExecutionError("run is not waiting for approval")
        current = self.store.get_current_plan(control.result.session_id if control.result else "")
        session_id = control.result.session_id if control.result else None
        if session_id is None:
            raise ExecutionError("run session is unavailable")
        if not approved:
            await self._set_step(session_id, step_id, "failed", summary=comment or "rejected", execution={"run_id": run_id, "status": "rejected"})
            control.waiting_step = None
            return await self.execute(current or {}, session_id=session_id, run_id=run_id, reset=False) if current is not None else None
        await self._set_step(session_id, step_id, "succeeded", summary=comment or "approved", execution={"run_id": run_id, "status": "approved"})
        control.waiting_step = None
        if current is None:
            return None
        return await self.execute(current, session_id=session_id, run_id=run_id, reset=False)
