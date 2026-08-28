"""The tool broker: the only door between a model and real work.

Every tool call the model makes lands here.  The broker re-validates *everything*
against server-owned state — the plan it published, the catalog you registered,
the files this session actually owns — and only then touches the engine.  It is
the reason a plan step reading ``succeeded`` corresponds to a real run.

What it enforces, beyond per-tool schema validation:

**Plan authorisation.** ``run_capability`` requires a ``capability`` step in the
current plan whose ``capability`` field matches, whose dependencies have all
succeeded, and which has not already run.

**Kind ownership.** ``update_step`` refuses ``capability``/``workflow`` steps, so
the model cannot mark server-owned work complete.

**Input gating.** Once ``request_inputs`` fires, every mutating tool is refused
until the request is fulfilled or cancelled. Read-only lookups stay open.

**One execution at a time.** A second ``run_*`` while one is in flight is
refused rather than queued, so the plan's DAG stays the only concurrency model.

**Loop budgets.** A per-turn call budget and an identical-call repeat limit stop
a confused model from burning context in a no-progress loop.

**Bounded, value-free errors.** Failures come back as a stable ``error_code``
plus a short message. Rejected input values are never echoed — a model handed its
own bad payload tends to send it again.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from dataclasses import dataclass, field, replace
from typing import Any, Callable, Mapping

from . import inputs as inputs_module
from . import plan as plan_module
from . import tools as tools_module
from .engine import (
    Engine,
    ExecutionGraph,
    ExecutionNode,
    Run,
    graph_from_capability,
    graph_from_workflow,
)
from .plan_executor import PlanExecutor, resolve_retry
from .errors import (
    ActionBudgetError,
    AwaitingInputsError,
    ExecutionBusyError,
    InvalidArgumentError,
    LoomCraftError,
    RepeatedActionError,
    UnsupportedActionError,
)
from .registry import Registry
from .store import Session, public_artifact

logger = logging.getLogger("loomcraft.broker")

DEFAULT_ACTIONS_PER_TURN = 64
DEFAULT_IDENTICAL_ACTIONS = 3
DEFAULT_INSPECT_BYTES = 16 * 1024
DEFAULT_INSPECT_LINES = 40


@dataclass(frozen=True, slots=True)
class ToolResponse:
    """What the broker hands back for one tool call."""

    ok: bool
    result: dict[str, Any] | None = None
    error: str | None = None
    error_code: str | None = None

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"ok": self.ok}
        if self.result is not None:
            payload["result"] = self.result
        if self.error:
            payload["error"] = self.error[:4000]
        if not self.ok:
            payload["error_code"] = self.error_code or "BROKER_ACTION_FAILED"
        return payload

    def to_tool_result_text(self) -> str:
        """JSON text suitable for a ``tool_result`` content block."""
        return json.dumps(self.to_dict(), ensure_ascii=False)


@dataclass
class BrokerLimits:
    """Per-turn guardrails against runaway tool loops."""

    max_actions_per_turn: int = DEFAULT_ACTIONS_PER_TURN
    max_identical_actions: int = DEFAULT_IDENTICAL_ACTIONS
    max_inspect_bytes: int = DEFAULT_INSPECT_BYTES
    max_inspect_lines: int = DEFAULT_INSPECT_LINES
    search_limit: int = 10


@dataclass
class _TurnState:
    calls: int = 0
    repeats: dict[str, int] = field(default_factory=dict)


class ToolBroker:
    """Validates and dispatches one session's agent tool calls."""

    def __init__(
        self,
        session: Session,
        registry: Registry,
        *,
        engine: Engine | None = None,
        limits: BrokerLimits | None = None,
        on_event: Callable[[str, Mapping[str, Any]], Any] | None = None,
    ) -> None:
        self.session = session
        self.registry = registry
        self.limits = limits or BrokerLimits()
        self.engine = engine or Engine(registry, session, emit=self._emit)
        self._on_event = on_event
        self._turn = _TurnState()
        self._active_run: Run | None = None
        self._awaiting_inputs = bool(
            inputs_module.pending_requests(
                [event.to_dict() for event in session.events.read()]
            )
        )

    # ── Turn lifecycle ──────────────────────────────────────────────────────

    def begin_turn(self) -> None:
        """Reset per-turn budgets. Call once before handing tools to the model."""
        self._turn = _TurnState()
        self._awaiting_inputs = bool(
            inputs_module.pending_requests(
                [event.to_dict() for event in self.session.events.read()]
            )
        )

    @property
    def awaiting_inputs(self) -> bool:
        return self._awaiting_inputs

    @property
    def active_run(self) -> Run | None:
        return self._active_run

    async def close(self) -> None:
        """Cancel anything still running for this session."""
        if self._active_run is not None:
            with contextlib.suppress(Exception):
                await self._active_run.cancel()
            self._active_run = None

    async def dispatch_dynamic_tool(
        self, name: str, payload: Mapping[str, Any] | None = None
    ) -> dict[str, Any]:
        """:meth:`dispatch` in the plain-dict form an app-server host expects.

        Codex and similar runtimes hand back a JSON object per tool call rather
        than a typed result; this is the same validation path, serialized.
        """
        return (await self.dispatch(name, payload)).to_dict()

    # ── Event plumbing ──────────────────────────────────────────────────────

    def _emit(self, name: str, data: Mapping[str, Any]) -> Any:
        record = self.session.emit(name, data)
        if self._on_event is not None:
            with contextlib.suppress(Exception):
                self._on_event(name, record.data)
        return record

    # ── Dispatch ────────────────────────────────────────────────────────────

    async def dispatch(self, name: str, payload: Mapping[str, Any] | None = None) -> ToolResponse:
        """Validate and execute one tool call. Never raises for model errors."""
        arguments = dict(payload or {})

        self._turn.calls += 1
        if self._turn.calls > self.limits.max_actions_per_turn:
            return self._error(
                ActionBudgetError(
                    "tool-call budget exceeded: "
                    f"{self.limits.max_actions_per_turn} calls per turn"
                )
            )

        signature = json.dumps(
            {"name": name, "payload": arguments},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        repeats = self._turn.repeats.get(signature, 0) + 1
        self._turn.repeats[signature] = repeats
        if repeats > self.limits.max_identical_actions:
            return self._error(
                RepeatedActionError(
                    f"identical call repeated without progress: {name}"
                )
            )

        if name not in tools_module.READ_ONLY_TOOLS and self._awaiting_inputs:
            return self._error(
                AwaitingInputsError(
                    "this turn is waiting for user files; end the turn without "
                    "further plan or execution actions"
                )
            )

        try:
            return await self._route(name, arguments)
        except LoomCraftError as exc:
            logger.info("broker rejected %s: %s", name, exc)
            return self._error(exc)
        except Exception:  # noqa: BLE001 - stable model-facing boundary
            logger.exception("broker tool %s failed", name)
            return ToolResponse(
                ok=False,
                error="tool execution failed",
                error_code="BROKER_INTERNAL_ERROR",
            )

    async def _route(self, name: str, payload: dict[str, Any]) -> ToolResponse:
        if name == tools_module.SESSION_CONTEXT:
            return self._session_context()
        if name == tools_module.CAPABILITY_SEARCH:
            return self._capability_search(payload)
        if name == tools_module.CATALOG_SEARCH:
            return self._catalog_search(payload)
        if name == tools_module.INSPECT_SOURCE:
            return self._inspect_source(payload)
        if name == tools_module.PUBLISH_PLAN:
            return self._publish_plan(payload)
        if name == tools_module.UPDATE_STEP:
            return self._update_step(payload)
        if name == tools_module.REQUEST_INPUTS:
            return self._request_inputs(payload)
        if name == tools_module.REGISTER_ARTIFACTS:
            return self._register_artifacts(payload)
        if name in {
            tools_module.RUN_CAPABILITY,
            tools_module.RUN_WORKFLOW,
            tools_module.EXECUTE_PLAN,
        }:
            if self._busy:
                return self._error(
                    ExecutionBusyError(
                        "a previous execution is still active; wait for it to finish"
                    )
                )
            if name == tools_module.RUN_CAPABILITY:
                return await self._run_capability(payload)
            if name == tools_module.RUN_WORKFLOW:
                return await self._run_workflow(payload)
            return await self._execute_plan(payload)
        return self._error(UnsupportedActionError(f"unsupported tool {name!r}"))

    @property
    def _busy(self) -> bool:
        return self._active_run is not None and self._active_run.status not in {
            "succeeded",
            "failed",
            "cancelled",
        }

    def _error(self, exc: LoomCraftError) -> ToolResponse:
        return ToolResponse(
            ok=False,
            error=exc.public_message,
            error_code=exc.code,
        )

    # ── Read-only tools ─────────────────────────────────────────────────────

    def _session_context(self) -> ToolResponse:
        current = self.session.current_plan()
        uploads = [
            {
                "source_ref": row["source_ref"],
                "filename": row["filename"],
                "size": row["size"],
                "content_type": row.get("content_type"),
            }
            for row in self.session.list_uploads()
        ]
        artifacts = [
            {
                "source_ref": row["source_ref"],
                "filename": row["filename"],
                "size": row["size"],
                "step_id": row.get("step_id"),
            }
            for row in self.session.list_artifacts()
        ]
        executions = [
            {
                "id": row.get("id"),
                "capability": row.get("capability"),
                "status": row.get("status"),
                "step_id": row.get("step_id"),
                "error": row.get("error"),
            }
            for row in self.session.list_executions()[-10:]
        ]
        pending = inputs_module.pending_requests(
            [event.to_dict() for event in self.session.events.read()]
        )
        return ToolResponse(
            ok=True,
            result={
                "session_id": self.session.id,
                "uploads": uploads,
                "artifacts": artifacts,
                "executions": executions,
                "plan": {
                    "revision": current.get("revision") if current else None,
                    "goal": current.get("goal") if current else None,
                    "steps": [
                        {
                            "id": step["id"],
                            "title": step["title"],
                            "kind": step["kind"],
                            "status": step["status"],
                            "depends_on": step["depends_on"],
                            "capability": step.get("capability"),
                            "summary": step.get("summary"),
                        }
                        for step in (current or {}).get("steps", [])
                    ],
                }
                if current
                else None,
                "catalog": self.registry.catalog_summary(),
                "awaiting_inputs": [
                    {"request_id": row["request_id"], "title": row["title"]}
                    for row in pending
                ],
            },
        )

    def _capability_search(self, payload: dict[str, Any]) -> ToolResponse:
        query = _short_string(payload.get("query"), "query", 400)
        limit = _bounded_int(
            payload.get("limit", 5), "limit", 1, self.limits.search_limit
        )
        return ToolResponse(
            ok=True,
            result={
                "results": self.registry.search(query, scope="capabilities", limit=limit)
            },
        )

    def _catalog_search(self, payload: dict[str, Any]) -> ToolResponse:
        query = _short_string(payload.get("query"), "query", 400)
        scope = payload.get("scope", "all")
        if scope not in {"all", "capabilities", "workflows"}:
            raise LoomCraftError("catalog scope is invalid")
        limit = _bounded_int(
            payload.get("limit", 5), "limit", 1, self.limits.search_limit
        )
        return ToolResponse(
            ok=True,
            result={"results": self.registry.search(query, scope=scope, limit=limit)},
        )

    def _inspect_source(self, payload: dict[str, Any]) -> ToolResponse:
        source_ref = _short_string(payload.get("source_ref"), "source_ref", 500)
        max_bytes = _bounded_int(
            payload.get("max_bytes", self.limits.max_inspect_bytes),
            "max_bytes",
            1,
            self.limits.max_inspect_bytes,
        )
        max_lines = _bounded_int(
            payload.get("max_lines", self.limits.max_inspect_lines),
            "max_lines",
            1,
            self.limits.max_inspect_lines,
        )
        resolved = self.session.resolve_source(source_ref)
        with resolved.path.open("rb") as handle:
            head = handle.read(max_bytes)
        text = head.decode("utf-8", errors="replace")
        lines = text.splitlines()[:max_lines]
        printable = sum(1 for byte in head[:1024] if 9 <= byte <= 126 or byte >= 160)
        binary = bool(head) and printable / max(1, len(head[:1024])) < 0.75
        return ToolResponse(
            ok=True,
            result={
                "source_ref": source_ref,
                "filename": resolved.filename,
                "size": resolved.size,
                "checksum": resolved.checksum,
                "content_type": resolved.content_type,
                "binary": binary,
                "truncated": resolved.size > len(head),
                "preview_lines": [] if binary else lines,
            },
        )

    # ── Whole-plan execution ────────────────────────────────────────────────

    async def _execute_plan(self, payload: dict[str, Any]) -> ToolResponse:
        """Run the current published plan through the canonical engine."""
        current = self._require_plan()
        parsed = plan_module.parse_plan(current)

        bindings = payload.get("inputs", {})
        if bindings is not None and not isinstance(bindings, Mapping):
            raise InvalidArgumentError("inputs must be an object")
        timeout = payload.get("timeout_seconds")
        if timeout is not None and (
            isinstance(timeout, bool)
            or not isinstance(timeout, (int, float))
            or timeout <= 0
        ):
            raise InvalidArgumentError("timeout_seconds must be a positive number")

        started = time.monotonic()
        runner = PlanExecutor(self.registry, self.session, engine=self.engine)

        def submitted(active: Run) -> None:
            self._active_run = active
            self._emit(
                "execution_started",
                {
                    "execution_id": active.id,
                    "execution_kind": "plan",
                    "revision": parsed.revision,
                    "nodes": [node.id for node in active.graph.nodes],
                },
            )

        run = await runner.execute(
            parsed,
            inputs=dict(bindings or {}),
            timeout_seconds=float(timeout) if timeout is not None else None,
            on_submitted=submitted,
        )

        # The engine owns node state; the plan is what the user reads. Project
        # one onto the other so both views agree.
        self._project_run(run)

        paused = run.status == "paused_approval"
        ok = run.status == "succeeded"
        execution = {
            **run.to_dict(),
            "kind": "plan",
            "revision": parsed.revision,
            "duration_seconds": round(time.monotonic() - started, 3),
        }
        self.session.record_execution(execution)
        if run.status in {"succeeded", "failed", "cancelled"}:
            self._active_run = None
        self._emit("execution_finished", {"execution": execution})

        failure = run.error or "; ".join(
            f"{row['node_id']}: {row['error']}"
            for row in run.blocking_failures
            if row.get("error")
        )
        return ToolResponse(
            ok=ok or paused,
            result=execution,
            error=None if ok or paused else (failure or "plan execution failed"),
            error_code=None if ok or paused else "EXECUTION_FAILED",
        )

    def _project_run(self, run: Run) -> None:
        """Write engine node state back onto the published plan."""
        current = self.session.current_plan()
        if current is None:
            return
        revision = current.get("revision")
        updated = current
        for node_id, state in run.nodes.items():
            if state.status in {"pending", "ready"}:
                continue
            try:
                updated = plan_module.update_step(
                    updated,
                    node_id,
                    state.status,
                    summary=state.error or self._node_summary(state),
                    execution={
                        "run_id": run.id,
                        "status": state.status,
                        "attempts": state.attempts,
                        "error": state.error,
                        "artifacts": [
                            public_artifact(item) for item in state.artifacts
                        ],
                    },
                )
                for row in updated["steps"]:
                    if row["id"] == node_id:
                        row["attempts"] = state.attempts
                self.session.update_current_plan(updated)
                self._emit(
                    "step_updated",
                    {
                        "revision": revision,
                        "step": plan_module.get_step(updated, node_id),
                    },
                )
            except LoomCraftError:
                # A node id that is not a plan step, or a transition the plan
                # forbids. Neither should abort the projection of its siblings.
                logger.debug("could not project node %s", node_id, exc_info=True)

    @staticmethod
    def _node_summary(state: Any) -> str | None:
        detail = getattr(state, "detail", {}) or {}
        if isinstance(detail, Mapping) and detail.get("summary"):
            return str(detail["summary"])[:2000]
        return "completed" if state.status == "succeeded" else None

    # ── Run control ─────────────────────────────────────────────────────────

    async def cancel_run(self, run_id: str) -> bool:
        """Cancel one engine run by id. Host-facing; not a model tool."""
        cancelled = await self.engine.cancel(run_id)
        if cancelled and self._active_run is not None and self._active_run.id == run_id:
            self._active_run = None
        return cancelled

    async def approve_run(
        self, run_id: str, node_id: str, *, approved: bool = True, comment: str = ""
    ) -> dict[str, Any] | None:
        """Resolve one approval gate and let the run continue.

        Returns the run's state once it reaches a terminal status or its *next*
        gate — a graph may hold several, and waiting for all of them would hang
        the caller that is supposed to resolve them one at a time. Returns
        ``None`` when nothing was awaiting this decision.
        """
        run = self.engine.get(run_id)
        if run is None:
            return None
        target = self._approval_target(run, node_id)
        if target is None or not run.approve(target, approved):
            return None
        self._emit(
            "approval_resolved",
            {
                "execution_id": run_id,
                "node_id": target,
                "approved": approved,
                "comment": comment[:2000],
            },
        )
        while True:
            if run.status in {"succeeded", "failed", "cancelled"}:
                break
            if (
                run.status == "paused_approval"
                and run.pending_approvals
                and target not in run.pending_approvals
            ):
                break
            await asyncio.sleep(0.01)

        self._project_run(run)
        execution = run.to_dict()
        if run.plan_step_id:
            execution["step_id"] = run.plan_step_id
        self.session.record_execution(execution)
        if run.status in {"succeeded", "failed", "cancelled"}:
            self._emit("execution_finished", {"execution": execution})
            if self._active_run is run:
                self._active_run = None
                self.session.update_meta(status="idle", last_turn_status=run.status)
        return execution

    @staticmethod
    def _approval_target(run: Run, node_id: str) -> str | None:
        """Map a caller-supplied id onto the node actually awaiting approval.

        A UI that knows only about plan steps will send a step id. For a
        single-capability run the graph node is called ``execute``, so accept
        the step id and resolve it to the one gate that is open.
        """
        if node_id in run.nodes:
            return node_id
        if run.plan_step_id == node_id or not node_id:
            if len(run.pending_approvals) == 1:
                return run.pending_approvals[0]
            if len(run.nodes) == 1:
                return next(iter(run.nodes))
        return None

    # ── Plan tools ──────────────────────────────────────────────────────────

    def _publish_plan(self, payload: dict[str, Any]) -> ToolResponse:
        current = self.session.current_plan()
        validated = plan_module.validate_plan(
            payload.get("plan"), current, registry=self.registry
        )
        stored = self.session.publish_plan(validated)
        self._emit("plan_published", {"plan": stored})
        return ToolResponse(ok=True, result={"plan": stored})

    def _update_step(self, payload: dict[str, Any]) -> ToolResponse:
        current = self._require_plan()
        step_id = _short_string(payload.get("step_id"), "step_id", 64)
        step = plan_module.get_step(current, step_id)
        _require_agent_owned(
            step,
            step_id,
            "updated only by their execution tools",
            "use its execution tool instead of update_step",
        )
        status = _short_string(payload.get("status"), "status", 32)
        raw_summary = payload.get("summary")
        summary = (
            _short_string(raw_summary, "summary", 2000)
            if isinstance(raw_summary, str) and raw_summary.strip()
            else None
        )
        if status in {"running", "succeeded"}:
            plan_module.ensure_dependencies_succeeded(current, step_id)
        updated = plan_module.update_step(current, step_id, status, summary=summary)  # type: ignore[arg-type]
        if status in {"failed", "skipped"}:
            updated = plan_module.propagate_skips(updated)
        self.session.update_current_plan(updated)
        result_step = plan_module.get_step(updated, step_id)
        self._emit(
            "step_updated", {"revision": updated["revision"], "step": result_step}
        )
        return ToolResponse(ok=True, result={"step": result_step})

    def _request_inputs(self, payload: dict[str, Any]) -> ToolResponse:
        request = inputs_module.validate_input_request(payload.get("request"))
        self._emit("input_required", {"request": request})
        self._awaiting_inputs = True
        return ToolResponse(ok=True, result={"request": request})

    def _register_artifacts(self, payload: dict[str, Any]) -> ToolResponse:
        step_id = _short_string(payload.get("step_id"), "step_id", 64)
        current = self._require_plan()
        step = plan_module.get_step(current, step_id)
        _require_agent_owned(
            step,
            step_id,
            "artifacts may only be registered against a step you own",
            "its artifacts are registered by its execution tool",
        )
        raw = payload.get("artifacts")
        if not isinstance(raw, list):
            raise LoomCraftError("artifacts must be an array")
        registered = self.session.register_scratch_artifacts(raw, step_id=step_id)
        for artifact in registered:
            self._emit("artifact_registered", {"artifact": public_artifact(artifact)})
        return ToolResponse(
            ok=True,
            result={
                "artifacts": [
                    {
                        "id": item["id"],
                        "filename": item["filename"],
                        "size": item["size"],
                        "source_ref": item["source_ref"],
                    }
                    for item in registered
                ]
            },
        )

    # ── Execution tools ─────────────────────────────────────────────────────

    async def _run_capability(self, payload: dict[str, Any]) -> ToolResponse:
        capability_id = _short_string(payload.get("capability_id"), "capability_id", 160)
        step_id = _short_string(payload.get("step_id"), "step_id", 64)
        capability = self.registry.capability(capability_id)

        current = self._require_plan()
        # A review step may bind a review-scoped capability; publication already
        # verified that it really is one.
        planned = plan_module.get_step(current, step_id)
        expected_kind = "review" if planned["kind"] == "review" else "capability"
        plan_module.ensure_step_startable(
            current, step_id, kind=expected_kind, capability=capability_id
        )
        sources = capability.validate_inputs(payload.get("inputs"))
        parameters = capability.validate_parameters(payload.get("parameters"))
        # Resolve every source before touching the plan, so an unknown upload
        # fails the call rather than leaving a step stuck in `running`.
        for refs in sources.values():
            for ref in refs:
                self.session.resolve_source(ref)

        graph = self._apply_step_policy(
            graph_from_capability(capability, sources=sources, parameters=parameters),
            planned,
        )
        return await self._execute(
            graph=graph,
            step_id=step_id,
            kind=expected_kind,
            label=capability.name,
            target_id=capability_id,
            version=capability.version,
            parameters=parameters,
        )

    async def _run_workflow(self, payload: dict[str, Any]) -> ToolResponse:
        workflow_id = _short_string(payload.get("workflow_id"), "workflow_id", 160)
        step_id = _short_string(payload.get("step_id"), "step_id", 64)
        workflow = self.registry.workflow(workflow_id)

        current = self._require_plan()
        planned = plan_module.get_step(current, step_id)
        plan_module.ensure_step_startable(
            current, step_id, kind="workflow", capability=workflow_id
        )
        sources = workflow.validate_inputs(payload.get("inputs"))
        parameters = workflow.validate_parameters(payload.get("parameters"))
        for refs in sources.values():
            for ref in refs:
                self.session.resolve_source(ref)

        graph = self._apply_step_policy(
            graph_from_workflow(workflow, sources=sources, parameters=parameters),
            planned,
        )
        return await self._execute(
            graph=graph,
            step_id=step_id,
            kind="workflow",
            label=workflow.name,
            target_id=workflow_id,
            version=workflow.version,
            parameters=parameters,
        )

    async def _execute(
        self,
        *,
        graph: Any,
        step_id: str,
        kind: str,
        label: str,
        target_id: str,
        version: str,
        parameters: Mapping[str, Any],
    ) -> ToolResponse:
        started = time.monotonic()
        self._set_step(step_id, "running", summary=f"running {label}")

        run: Run | None = None
        try:
            run = self.engine.submit(graph)
            run.plan_step_id = step_id
            self._active_run = run
            self._emit(
                "execution_started",
                {
                    "step_id": step_id,
                    "execution_kind": kind,
                    "execution_id": run.id,
                    "capability": target_id,
                    "nodes": [node.id for node in graph.nodes],
                },
            )
            # ``settled`` rather than ``wait``: an approval gate is a question
            # for a human, and the agent turn must end so someone can answer it.
            await run.settled()
        except asyncio.CancelledError:
            if run is not None:
                with contextlib.suppress(Exception):
                    await run.cancel()
            self._set_step(
                step_id,
                "failed",
                summary=f"{label} was cancelled",
                execution={"kind": kind, "id": run.id if run else None, "status": "cancelled"},
            )
            raise
        finally:
            if run is not None and run.status in {"succeeded", "failed", "cancelled"}:
                self._active_run = None

        ok = run.status == "succeeded"
        paused = run.status == "paused_approval"
        # Surface the actual node error rather than a generic label — this text
        # goes straight into the model's context and is what it replans against.
        node_errors = [
            f"{row['node_id']}: {row['error']}"
            for row in run.blocking_failures
            if row.get("error")
        ]
        failure = run.error or "; ".join(node_errors) or f"{label} did not succeed"

        artifacts = [
            {
                "id": item["id"],
                "filename": item["filename"],
                "size": item["size"],
                "source_ref": item["source_ref"],
                "port_name": item.get("port_name"),
            }
            for item in run.artifacts
        ]
        attempts = max((state.attempts for state in run.nodes.values()), default=0)
        execution = {
            "id": run.id,
            "kind": kind,
            "capability": target_id,
            "capability_version": version,
            "status": run.status,
            "step_id": step_id,
            "duration_seconds": round(time.monotonic() - started, 3),
            "attempts": attempts,
            "parameters": dict(parameters),
            "failed_nodes": run.failed_nodes,
            "error": None if ok or paused else failure,
            "artifacts": artifacts,
        }
        self.session.record_execution(execution)

        summary = (
            f"{label} succeeded; produced {len(artifacts)} artifact(s)"
            if ok
            else f"{label} is waiting for approval"
            if paused
            else f"{label} failed — {failure}"
        )
        self._set_step(
            step_id,
            "succeeded" if ok else "waiting_approval" if paused else "failed",
            summary=summary,
            execution={
                "kind": kind,
                "id": run.id,
                "status": run.status,
                "attempts": attempts,
            },
        )
        self._emit("execution_finished", {"step_id": step_id, "execution": execution})
        return ToolResponse(
            ok=ok or paused,
            result=execution,
            error=None if ok or paused else summary,
            error_code=None if ok or paused else "EXECUTION_FAILED",
        )

    @staticmethod
    def _apply_step_policy(
        graph: ExecutionGraph, step: Mapping[str, Any]
    ) -> ExecutionGraph:
        """Overlay one plan step's retry/timeout/failure policy onto its graph.

        Keeps ``run_capability`` consistent with ``execute_plan``: the policy
        the reader sees on the step is the policy that runs, whichever tool
        dispatched it.
        """
        raw_retry = step.get("retry")
        policy = plan_module.RetryPolicy.model_validate(
            raw_retry if isinstance(raw_retry, Mapping) else {}
        )
        timeout = step.get("timeout_seconds")
        on_failure = str(step.get("on_failure", "stop"))

        def overlay(node: ExecutionNode) -> ExecutionNode:
            attempts, backoff, multiplier, max_backoff = resolve_retry(
                policy,
                attempts=node.max_attempts,
                backoff=node.retry_backoff_seconds,
                multiplier=node.retry_backoff_multiplier,
                max_backoff=node.retry_max_backoff_seconds,
            )
            return replace(
                node,
                max_attempts=max(1, attempts),
                retry_backoff_seconds=max(0.0, backoff),
                retry_backoff_multiplier=max(1.0, multiplier),
                retry_max_backoff_seconds=max_backoff,
                timeout_seconds=(
                    float(timeout)
                    if isinstance(timeout, (int, float)) and not isinstance(timeout, bool)
                    else node.timeout_seconds
                ),
                on_failure=(
                    on_failure
                    if on_failure in plan_module.FAILURE_POLICIES
                    else node.on_failure
                ),
            )

        return replace(graph, nodes=tuple(overlay(node) for node in graph.nodes))

    # ── Plan state helpers ──────────────────────────────────────────────────

    def _require_plan(self) -> dict[str, Any]:
        current = self.session.current_plan()
        if current is None:
            raise plan_module.PlanValidationError(
                "publish a task plan before executing anything",
                public_message="publish a task plan before executing anything",
            )
        return current

    def _set_step(
        self,
        step_id: str,
        status: str,
        *,
        summary: str | None = None,
        execution: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        current = self._require_plan()
        updated = plan_module.update_step(
            current, step_id, status, summary=summary, execution=execution  # type: ignore[arg-type]
        )
        if status in {"failed", "skipped"}:
            updated = plan_module.propagate_skips(updated)
        self.session.update_current_plan(updated)
        step = plan_module.get_step(updated, step_id)
        self._emit("step_updated", {"revision": updated["revision"], "step": step})
        return step

    # ── Host-side input request resolution ──────────────────────────────────

    def fulfill_input_request(self, request_id: str) -> dict[str, Any]:
        """Confirm that the user's uploads satisfy a pending request."""
        request = self._pending_request(request_id)
        allocated = inputs_module.validate_fulfillment(
            request, self.session.list_uploads()
        )
        record = self._emit(
            "input_fulfilled", {"request_id": request_id, "allocated": allocated}
        )
        self._awaiting_inputs = bool(
            inputs_module.pending_requests(
                [event.to_dict() for event in self.session.events.read()]
            )
        )
        return record.data

    def cancel_input_request(self, request_id: str) -> dict[str, Any]:
        """Give up on a request; the agent must continue without those files."""
        self._pending_request(request_id)
        record = self._emit("input_cancelled", {"request_id": request_id})
        self._awaiting_inputs = bool(
            inputs_module.pending_requests(
                [event.to_dict() for event in self.session.events.read()]
            )
        )
        return record.data

    def invalidate_requests_for_upload(self, upload_id: str) -> list[str]:
        """Re-open any request that had been satisfied by a now-deleted file."""
        events = [event.to_dict() for event in self.session.events.read()]
        affected = inputs_module.requests_using_upload(events, upload_id)
        for request_id in affected:
            self._emit(
                "input_invalidated",
                {"request_id": request_id, "upload_id": upload_id},
            )
        if affected:
            self._awaiting_inputs = True
        return affected

    def _pending_request(self, request_id: str) -> dict[str, Any]:
        events = [event.to_dict() for event in self.session.events.read()]
        for request in inputs_module.pending_requests(events):
            if request.get("request_id") == request_id:
                return request
        raise inputs_module.InputRequestError(
            f"no pending input request {request_id!r}",
            public_message="that input request is not pending",
        )


# ── Small validators ────────────────────────────────────────────────────────


def _require_agent_owned(
    step: Mapping[str, Any], step_id: str, reason: str, remedy: str
) -> None:
    """Refuse a self-report on a step whose result the server owns.

    ``capability`` and ``workflow`` steps are obvious. A ``review`` step bound
    to a capability counts too: binding it is exactly the choice to have the
    verification executed rather than asserted.
    """
    kind = step.get("kind")
    bound_review = kind == "review" and step.get("capability")
    if kind in plan_module.AGENT_REPORTABLE_KINDS and not bound_review:
        return
    label = "capability-bound review" if bound_review else f"{kind}"
    raise plan_module.PlanValidationError(
        reason,
        public_message=f"step {step_id!r} is a {label} step; {remedy}",
    )


def _short_string(value: object, name: str, maximum: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise LoomCraftError(f"{name} must be a non-empty string")
    text = value.strip()
    if len(text) > maximum:
        raise LoomCraftError(f"{name} exceeds {maximum} characters")
    return text


def _bounded_int(value: object, name: str, minimum: int, maximum: int) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise LoomCraftError(f"{name} must be an integer")
    if not minimum <= value <= maximum:
        raise LoomCraftError(f"{name} must be between {minimum} and {maximum}")
    return value


__all__ = ["BrokerLimits", "ToolBroker", "ToolResponse"]
