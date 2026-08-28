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
import inspect
import json
import logging
import secrets
import time
from dataclasses import dataclass, field, replace
from typing import Any, Callable, Mapping

from . import inputs as inputs_module
from . import plan as plan_module
from . import tools as tools_module
from .engine import Engine, ExecutionGraph, ExecutionNode, Run, graph_from_capability, graph_from_workflow
from .errors import (
    ActionBudgetError,
    AwaitingInputsError,
    ContractError,
    ExecutionBusyError,
    KnowledgeUnavailableError,
    InvalidArgumentError,
    LoomCraftError,
    RepeatedActionError,
    UnsupportedActionError,
)
from .registry import Capability, CapabilitySpec, Registry, Workflow, WorkflowSpec
from .store import Session, public_artifact

logger = logging.getLogger("loomcraft.broker")

# Module-level compatibility export; schemas themselves live in ``tools.py``.
dynamic_tool_specs = tools_module.dynamic_tool_specs

DEFAULT_ACTIONS_PER_TURN = 64
DEFAULT_IDENTICAL_ACTIONS = 3
DEFAULT_INSPECT_BYTES = 16 * 1024
DEFAULT_INSPECT_LINES = 40

# Stable names retained for hosts that imported the extracted broker constants.
ERROR_ACTION_FAILED = "BROKER_ACTION_FAILED"
ERROR_ACTION_LIMIT = "BROKER_ACTION_LIMIT_EXCEEDED"
ERROR_ACTION_REPEATED = "BROKER_ACTION_REPEATED"
ERROR_AWAITING_INPUTS = "BROKER_AWAITING_INPUTS"
ERROR_EXECUTION_BUSY = "BROKER_EXECUTION_BUSY"
ERROR_INPUT_REQUEST_INVALID = "BROKER_INPUT_REQUEST_INVALID"
ERROR_INVALID_ARGUMENT = "BROKER_INVALID_ARGUMENT"
ERROR_PLAN_INVALID = "BROKER_PLAN_INVALID"
ERROR_UNSUPPORTED_ACTION = "BROKER_UNSUPPORTED_ACTION"
ERROR_INTERNAL = "BROKER_INTERNAL_ERROR"
ERROR_KNOWLEDGE_UNAVAILABLE = "BROKER_KNOWLEDGE_UNAVAILABLE"
ERROR_CAPABILITY_FAILED = "BROKER_CAPABILITY_EXECUTION_FAILED"
ERROR_WORKFLOW_FAILED = "BROKER_WORKFLOW_EXECUTION_FAILED"
ERROR_INPUT_INTEGRITY = "BROKER_INPUT_INTEGRITY_FAILED"
ERROR_EXECUTION_CLEANUP_PENDING = "BROKER_EXECUTION_CLEANUP_PENDING"
ERROR_STOPPING = "BROKER_STOPPING"
BROKER_MAX_ACTIONS_PER_TURN = DEFAULT_ACTIONS_PER_TURN
BROKER_MAX_IDENTICAL_ACTIONS = DEFAULT_IDENTICAL_ACTIONS


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
        try:
            json.dumps(payload, ensure_ascii=False, allow_nan=False)
        except (TypeError, ValueError):
            return {
                "ok": False,
                "error": "tool result was not JSON-serializable",
                "error_code": "BROKER_INTERNAL_ERROR",
            }
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

    def __post_init__(self) -> None:
        self.max_actions_per_turn = max(1, int(self.max_actions_per_turn))
        self.max_identical_actions = max(1, int(self.max_identical_actions))
        self.max_inspect_bytes = max(1, int(self.max_inspect_bytes))
        self.max_inspect_lines = max(1, int(self.max_inspect_lines))
        self.search_limit = max(1, int(self.search_limit))


@dataclass
class _TurnState:
    calls: int = 0
    repeats: dict[str, int] = field(default_factory=dict)


class ToolBroker:
    """Validates and dispatches one session's agent tool calls."""

    def __init__(
        self,
        session: Session | str,
        registry: Registry,
        *,
        engine: Engine | None = None,
        executor: Any | None = None,
        limits: BrokerLimits | None = None,
        on_event: Callable[[str, Mapping[str, Any]], Any] | None = None,
        table_inspector: Callable[[str, Mapping[str, Any]], Any] | None = None,
        catalog_provider: Callable[[str, str, int], Any] | None = None,
        knowledge_provider: Any | None = None,
        extra_tool_handlers: Mapping[str, Callable[[Mapping[str, Any]], Any]] | None = None,
        store: Any | None = None,
        artifact_store: Any | None = None,
        upload_store: Any | None = None,
        source_resolver: Any | None = None,
        max_actions: int | None = None,
        max_identical_actions: int | None = None,
    ) -> None:
        if engine is None and executor is not None:
            engine = executor
        # The extracted package originally accepted ``(session_id, registry,
        # store=...)``. Route that form through a namespaced compatibility
        # backend while keeping the typed Session API as the canonical path.
        self._legacy_backend: Any | None = None
        if isinstance(session, str):
            from .legacy_broker import ToolBroker as LegacyToolBroker

            self._legacy_backend = LegacyToolBroker(
                session,
                registry,
                store=store,
                executor=engine,
                on_event=on_event,
                table_inspector=table_inspector,
                catalog_provider=catalog_provider,
                knowledge_provider=knowledge_provider,
                artifact_store=artifact_store,
                source_resolver=source_resolver,
                extra_tool_handlers=extra_tool_handlers,
                max_actions=max_actions,
                max_identical_actions=max_identical_actions,
            )
            return
        self.session = session
        self.registry = registry
        if limits is not None and (
            max_actions is not None or max_identical_actions is not None
        ):
            raise ValueError(
                "pass either limits or max_actions/max_identical_actions, not both"
            )
        self.limits = limits or BrokerLimits(
            max_actions_per_turn=(
                DEFAULT_ACTIONS_PER_TURN if max_actions is None else max_actions
            ),
            max_identical_actions=(
                DEFAULT_IDENTICAL_ACTIONS
                if max_identical_actions is None
                else max_identical_actions
            ),
        )
        self._tool_schemas = {
            spec.name: spec.parameters
            for spec in tools_module.extended_tool_specs(
                max_search_results=self.limits.search_limit
            )
        }
        self.engine = engine or Engine(registry, session, emit=self._emit)
        self._on_event = on_event
        self.table_inspector = table_inspector
        self.catalog_provider = catalog_provider
        self.knowledge_provider = knowledge_provider
        self._extra_tool_handlers = dict(extra_tool_handlers or {})
        reserved = set(tools_module.READ_ONLY_TOOLS) | set(tools_module.MUTATING_TOOLS)
        collisions = sorted(reserved & set(self._extra_tool_handlers))
        if collisions:
            raise ValueError(
                "extra tool handlers cannot override LoomCraft tools: "
                + ", ".join(collisions)
            )
        self._turn = _TurnState()
        self._active_run: Run | None = None
        pinned_version = session.meta().get("knowledge_version")
        self._knowledge_version = (
            str(pinned_version) if isinstance(pinned_version, str) and pinned_version else None
        )
        self._awaiting_inputs = bool(
            inputs_module.pending_requests(
                [event.to_dict() for event in session.events.read()]
            )
        )

    # ── Turn lifecycle ──────────────────────────────────────────────────────

    def begin_turn(self) -> None:
        """Reset per-turn budgets. Call once before handing tools to the model."""
        if self._legacy_backend is not None:
            self._legacy_backend.begin_turn()
            return
        self._turn = _TurnState()
        self._awaiting_inputs = bool(
            inputs_module.pending_requests(
                [event.to_dict() for event in self.session.events.read()]
            )
        )

    @property
    def awaiting_inputs(self) -> bool:
        if self._legacy_backend is not None:
            return bool(self._legacy_backend.awaiting_inputs)
        return self._awaiting_inputs

    # Compatibility names used by the extracted runtime/provider layer.  The
    # canonical broker owns a Session object; exposing these read-only aliases
    # lets older adapters migrate without duplicating authorization logic.
    @property
    def session_id(self) -> str:
        if self._legacy_backend is not None:
            return self._legacy_backend.session_id
        return self.session.id

    @property
    def store(self) -> Session:
        if self._legacy_backend is not None:
            return self._legacy_backend.store
        return self.session

    @property
    def active_run(self) -> Run | None:
        if self._legacy_backend is not None:
            return self._legacy_backend.active_run
        return self._active_run

    async def close(self) -> None:
        """Cancel anything still running for this session."""
        if self._legacy_backend is not None:
            await self._legacy_backend.stop()
            return
        if self._active_run is not None:
            with contextlib.suppress(Exception):
                await self._active_run.cancel()
            self._active_run = None

    async def stop(self) -> None:
        """Compatibility alias for closing/cancelling the broker."""
        if self._legacy_backend is not None:
            await self._legacy_backend.stop()
            return
        await self.close()

    @property
    def stop_requested(self) -> bool:
        if self._legacy_backend is not None:
            return bool(self._legacy_backend.stop_requested)
        return False

    async def dispatch_dynamic_tool(
        self, name: str, payload: Mapping[str, Any] | None = None
    ) -> dict[str, Any]:
        """Compatibility alias returning the JSON form of :meth:`dispatch`."""
        if self._legacy_backend is not None:
            return await self._legacy_backend.dispatch_dynamic_tool(name, payload)
        return (await self.dispatch(name, payload)).to_dict()

    async def cancel_run(self, run_id: str) -> bool:
        """Cancel a scheduler-owned run by id."""
        if self._legacy_backend is not None:
            return await self._legacy_backend.cancel_run(run_id)
        cancelled = await self.engine.cancel(run_id)
        if cancelled and self._active_run is not None and self._active_run.id == run_id:
            self._active_run = None
        return cancelled

    async def approve_run(
        self, run_id: str, node_id: str, *, approved: bool = True, comment: str = ""
    ) -> dict[str, Any] | None:
        """Resolve a paused run and return its serialized state."""
        if self._legacy_backend is not None:
            return await self._legacy_backend.approve_run(
                run_id, node_id, approved=approved, comment=comment
            )
        run = self.engine.get(run_id)
        approval_node_id = node_id
        if run is not None and run.plan_step_id == node_id:
            if len(run.pending_approvals) == 1:
                approval_node_id = run.pending_approvals[0]
            elif len(run.nodes) == 1:
                approval_node_id = next(iter(run.nodes))
        if run is None or not run.approve(approval_node_id, approved):
            return None
        self._emit(
            "approval_resolved",
            {"execution_id": run_id, "node_id": node_id, "approved": approved, "comment": comment},
        )
        # A graph may contain more than one approval boundary. Wait for this
        # decision to be consumed, then return at either a terminal state or
        # the next pause instead of hanging until every future reviewer acts.
        while True:
            if run.status in {"succeeded", "failed", "cancelled"}:
                break
            if (
                run.status == "paused_approval"
                and run.pending_approvals
                and approval_node_id not in run.pending_approvals
            ):
                break
            await asyncio.sleep(0.01)
        self._project_run(run)
        execution = run.to_dict()
        if run.plan_step_id:
            execution["step_id"] = run.plan_step_id
        self.session.record_execution(execution)
        if run.status in {"succeeded", "failed", "cancelled"}:
            self._emit(
                "execution_finished",
                {"step_id": run.plan_step_id, "execution": execution},
            )
        if run.status in {"succeeded", "failed", "cancelled"} and self._active_run is run:
            self._active_run = None
            self.session.update_meta(status="idle", last_turn_status=run.status)
        return run.to_dict()

    # ── Event plumbing ──────────────────────────────────────────────────────

    def _emit(self, name: str, data: Mapping[str, Any]) -> Any:
        if self._legacy_backend is not None:
            return self._legacy_backend._emit(name, data)
        record = self.session.emit(name, data)
        if self._on_event is not None:
            with contextlib.suppress(Exception):
                self._on_event(name, record.data)
        return record

    # ── Dispatch ────────────────────────────────────────────────────────────

    async def dispatch(self, name: str, payload: Mapping[str, Any] | None = None) -> ToolResponse:
        """Validate and execute one tool call. Never raises for model errors."""
        if self._legacy_backend is not None:
            value = await self._legacy_backend.dispatch(name, payload)
            if isinstance(value, Mapping):
                return ToolResponse(
                    ok=bool(value.get("ok")),
                    result=dict(value.get("result")) if isinstance(value.get("result"), Mapping) else value.get("result"),
                    error=str(value.get("error")) if value.get("error") else None,
                    error_code=str(value.get("error_code")) if value.get("error_code") else None,
                )
            return value
        name = str(name or "").strip().replace("-", "_")
        if (
            not name
            or len(name) > 160
            or any(ord(character) < 32 for character in name)
        ):
            return self._error(UnsupportedActionError("tool name is invalid"))
        if payload is not None and not isinstance(payload, Mapping):
            return self._error(InvalidArgumentError("tool payload must be an object"))
        arguments = dict(payload or {})

        self._turn.calls += 1
        if self._turn.calls > self.limits.max_actions_per_turn:
            return self._error(
                ActionBudgetError(
                    "tool-call budget exceeded: "
                    f"{self.limits.max_actions_per_turn} calls per turn"
                )
            )

        try:
            signature = json.dumps(
                {"name": name, "payload": arguments},
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
        except (TypeError, ValueError):
            return self._error(
                InvalidArgumentError("tool payload must be JSON-serializable")
            )
        if len(signature.encode("utf-8")) > 512 * 1024:
            return self._error(
                InvalidArgumentError("tool payload exceeds the 512 KiB limit")
            )

        schema = self._tool_schemas.get(name)
        if schema is not None and name not in {
            tools_module.PUBLISH_PLAN,
            tools_module.REQUEST_INPUTS,
        }:
            from .schema import SchemaValidationError, validate as validate_schema

            try:
                validate_schema(arguments, schema, "payload")
            except SchemaValidationError as exc:
                message = str(exc)[:1000]
                if name == tools_module.PUBLISH_PLAN:
                    return self._error(
                        plan_module.PlanValidationError(
                            message, public_message=message
                        )
                    )
                if name == tools_module.REQUEST_INPUTS:
                    return self._error(
                        inputs_module.InputRequestError(
                            message, public_message=message
                        )
                    )
                return self._error(InvalidArgumentError(message))
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
        except Exception:
            logger.exception("broker tool %s failed", name)
            return ToolResponse(
                ok=False,
                error="tool execution failed",
                error_code="BROKER_INTERNAL_ERROR",
            )

    async def _route(self, name: str, payload: dict[str, Any]) -> ToolResponse:
        if name in self._extra_tool_handlers:
            return await self._extension_tool(name, payload)
        if name == tools_module.SESSION_CONTEXT:
            return self._session_context()
        if name == tools_module.CAPABILITY_SEARCH:
            return self._capability_search(payload)
        if name == tools_module.CATALOG_SEARCH:
            return await self._catalog_search(payload)
        if name == tools_module.INSPECT_SOURCE:
            return self._inspect_source(payload)
        if name in {
            tools_module.OPERATION_SEARCH,
            tools_module.INSPECT_TABLE,
            tools_module.KNOWLEDGE_LIST,
            tools_module.KNOWLEDGE_SEARCH,
            tools_module.KNOWLEDGE_READ,
            tools_module.REGISTER_ARTIFACT,
        }:
            return await self._extension_tool(name, payload)
        if name == tools_module.PUBLISH_PLAN:
            return self._publish_plan(payload)
        if name == tools_module.UPDATE_STEP:
            return self._update_step(payload)
        if name == tools_module.REQUEST_INPUTS:
            return self._request_inputs(payload)
        if name == tools_module.REGISTER_ARTIFACTS:
            return self._register_artifacts(payload)
        if name == tools_module.EXECUTE_PLAN:
            return await self._execute_plan(payload)
        if name in {tools_module.RUN_CAPABILITY, tools_module.RUN_WORKFLOW}:
            if self._active_run is not None and self._active_run.status not in {
                "succeeded",
                "failed",
                "cancelled",
            }:
                return self._error(
                    ExecutionBusyError(
                        "a previous execution is still active; wait for it to finish"
                    )
                )
            if name == tools_module.RUN_CAPABILITY:
                return await self._run_capability(payload)
            return await self._run_workflow(payload)
        return self._error(UnsupportedActionError(f"unsupported tool {name!r}"))

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
                    "summary": current.get("summary") if current else None,
                    "reason": current.get("reason") if current else None,
                    "analysis_profile": current.get("analysis_profile") if current else None,
                    "objectives": list((current or {}).get("objectives", [])),
                    "analysis_coverage": list(
                        (current or {}).get("analysis_coverage", [])
                    ),
                    "steps": [
                        {
                            "id": step["id"],
                            "title": step["title"],
                            "kind": step["kind"],
                            "status": step["status"],
                            "depends_on": step["depends_on"],
                            "capability": step.get("capability"),
                            "summary": step.get("summary"),
                            "retry": step.get("retry"),
                            "timeout_seconds": step.get("timeout_seconds"),
                            "on_failure": step.get("on_failure"),
                            "attempts": step.get("attempts", 0),
                        }
                        for step in (current or {}).get("steps", [])
                    ],
                }
                if current
                else None,
                "catalog": self.registry.catalog_summary(),
                "knowledge_version": self._knowledge_version,
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

    async def _catalog_search(self, payload: dict[str, Any]) -> ToolResponse:
        query = _short_string(payload.get("query"), "query", 400)
        scope = payload.get("scope", "all")
        allowed = {
            "all",
            "capabilities",
            "workflows",
            "operations",
            "tools",
            "skills",
            "runners",
        }
        if scope not in allowed:
            raise LoomCraftError("catalog scope is invalid")
        limit = _bounded_int(
            payload.get("limit", 5), "limit", 1, self.limits.search_limit
        )
        rows = self.registry.search(query, scope=scope, limit=limit)
        if self.catalog_provider is not None and scope not in {"capabilities", "workflows"}:
            provided = self.catalog_provider(query, str(scope), limit)
            if inspect.isawaitable(provided):
                provided = await provided
            if isinstance(provided, list):
                seen = {
                    (str(item.get("type", item.get("scope", ""))), str(item.get("id", "")))
                    for item in rows
                    if isinstance(item, Mapping)
                }
                for item in provided:
                    if not isinstance(item, Mapping):
                        continue
                    key = (
                        str(item.get("type", item.get("scope", ""))),
                        str(item.get("id", "")),
                    )
                    if key not in seen:
                        rows.append(dict(item))
                        seen.add(key)
        return ToolResponse(
            ok=True,
            result={"scope": scope, "query": query, "results": rows[:limit]},
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

    async def _extension_tool(self, name: str, payload: dict[str, Any]) -> ToolResponse:
        """Dispatch optional host extensions without widening the core boundary."""
        custom = self._extra_tool_handlers.get(name)
        if custom is not None:
            value = custom(dict(payload))
            if inspect.isawaitable(value):
                value = await value
            if isinstance(value, ToolResponse):
                return value
            if isinstance(value, Mapping):
                return ToolResponse(ok=True, result=dict(value))
            return ToolResponse(ok=True, result={"value": value})

        if name == tools_module.OPERATION_SEARCH:
            query = _short_string(payload.get("query"), "query", 400)
            limit = _bounded_int(
                payload.get("limit", 5), "limit", 1, self.limits.search_limit
            )
            if self.catalog_provider is not None:
                value = self.catalog_provider(query, "operations", limit)
                if inspect.isawaitable(value):
                    value = await value
                rows = (
                    [dict(item) for item in value if isinstance(item, Mapping)]
                    if isinstance(value, list)
                    else []
                )
                return ToolResponse(ok=True, result={"results": rows[:limit]})
            entries = getattr(self.registry, "_catalog_entries", {}).get("operations", [])
            terms = query.casefold().split()
            rows = [
                dict(item)
                for item in entries
                if all(term in json.dumps(item, ensure_ascii=False).casefold() for term in terms)
            ]
            return ToolResponse(ok=True, result={"results": rows[:limit]})

        if name == tools_module.INSPECT_TABLE:
            source_ref = _short_string(payload.get("source_ref"), "source_ref", 500)
            resolved = self.session.resolve_source(source_ref)
            if self.table_inspector is not None:
                options = dict(payload)
                options["resolved_path"] = resolved.path
                value = self.table_inspector(source_ref, options)
                if inspect.isawaitable(value):
                    value = await value
                if not isinstance(value, Mapping):
                    raise LoomCraftError("table inspector must return an object")
                return ToolResponse(ok=True, result=dict(value))
            from .inspection import inspect_table_file

            result = inspect_table_file(
                resolved.path,
                source_ref=source_ref,
                requested_format=str(payload.get("format", "auto")),
                max_rows=int(payload.get("max_rows", 100)),
                table=payload.get("table"),
            )
            return ToolResponse(ok=True, result=result)

        if name in {
            tools_module.KNOWLEDGE_LIST,
            tools_module.KNOWLEDGE_SEARCH,
            tools_module.KNOWLEDGE_READ,
        }:
            if self.knowledge_provider is None:
                raise LoomCraftError("knowledge provider is not configured")
            method_name = {
                tools_module.KNOWLEDGE_LIST: "list",
                tools_module.KNOWLEDGE_SEARCH: "search",
                tools_module.KNOWLEDGE_READ: "read",
            }[name]
            method = getattr(self.knowledge_provider, method_name, None)
            if not callable(method):
                raise LoomCraftError("knowledge provider does not implement the requested operation")
            value = method(dict(payload))
            if inspect.isawaitable(value):
                value = await value
            if isinstance(value, Mapping) and isinstance(value.get("version"), str):
                version = str(value["version"])
                if self._knowledge_version is None:
                    self._knowledge_version = version
                    self.session.update_meta(knowledge_version=version)
                elif self._knowledge_version != version:
                    raise KnowledgeUnavailableError(
                        "knowledge snapshot changed during this session"
                    )
            return ToolResponse(ok=True, result=dict(value) if isinstance(value, Mapping) else {"value": value})

        if name == tools_module.REGISTER_ARTIFACT:
            converted = {
                "step_id": payload.get("step_id"),
                "artifacts": [
                    {
                        "path": payload.get("path"),
                        "display_name": payload.get("display_name"),
                    }
                ],
            }
            return self._register_artifacts(converted)
        return self._error(UnsupportedActionError(f"unsupported extension tool {name!r}"))

    async def _execute_plan(self, payload: dict[str, Any]) -> ToolResponse:
        """Run the current published Plan through the canonical Engine driver."""
        current = self._require_plan()
        if self._active_run is not None and self._active_run.status not in {
            "succeeded",
            "failed",
            "cancelled",
        }:
            return self._error(
                ExecutionBusyError("a previous execution is still active; wait for it to finish")
            )
        from .plan_executor import PlanExecutor

        parsed = plan_module.parse_plan(current)
        inputs = payload.get("inputs", {})
        if inputs is not None and not isinstance(inputs, Mapping):
            raise LoomCraftError("inputs must be an object")
        runner = PlanExecutor(self.registry, self.session, engine=self.engine)
        timeout = payload.get("timeout_seconds")
        if timeout is not None and (isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or timeout <= 0):
            raise LoomCraftError("timeout_seconds must be a positive number")

        def submitted(active: Run) -> None:
            self._active_run = active
            self._emit(
                "execution_started",
                {
                    "run_id": active.id,
                    "execution_id": active.id,
                    "execution_kind": "plan",
                    "revision": parsed.revision,
                    "nodes": [node.id for node in active.graph.nodes],
                },
            )

        run = await runner.execute(
            parsed,
            inputs=inputs or {},
            timeout_seconds=float(timeout) if timeout is not None else None,
            on_submitted=submitted,
        )

        # Project the scheduler-owned node states back onto the published Plan
        # so the same event stream drives both the canonical and compatibility
        # renderers.
        self._project_run(run)

        execution = run.to_dict()
        execution["revision"] = parsed.revision
        execution["session_id"] = self.session.id
        self.session.record_execution(execution)
        if run.status in {"succeeded", "failed", "cancelled"}:
            self._active_run = None
        self._emit("execution_finished", {"run_id": run.id, "execution": execution})
        return ToolResponse(
            ok=run.status in {"succeeded", "paused_approval"},
            result=execution,
            error=None if run.status in {"succeeded", "paused_approval"} else (run.error or "plan execution failed"),
            error_code=None if run.status in {"succeeded", "paused_approval"} else "EXECUTION_FAILED",
        )

    def _project_run(self, run: Run) -> None:
        """Project canonical node state into the current Plan and emit deltas."""
        current = self.session.current_plan()
        if current is None:
            return
        revision = current.get("revision")
        if run.plan_step_id:
            status = (
                "waiting_approval"
                if run.status == "paused_approval"
                else run.status
            )
            if status not in {
                "running",
                "waiting_approval",
                "succeeded",
                "failed",
                "cancelled",
            }:
                return
            attempts = max(
                (state.attempts for state in run.nodes.values()), default=0
            )
            errors = [
                state.error
                for state in run.nodes.values()
                if state.status in {"failed", "waiting_approval"} and state.error
            ]
            try:
                updated = plan_module.update_step(
                    current,
                    run.plan_step_id,
                    status,
                    summary=(
                        "; ".join(errors)
                        if errors
                        else next(
                            (
                                str(state.detail["summary"])
                                for state in run.nodes.values()
                                if state.detail.get("summary")
                            ),
                            "completed" if status == "succeeded" else None,
                        )
                    ),
                    execution={
                        "run_id": run.id,
                        "status": run.status,
                        "attempts": attempts,
                        "artifacts": [public_artifact(item) for item in run.artifacts],
                    },
                )
                for row in updated.get("steps", []):
                    if row.get("id") == run.plan_step_id:
                        row["attempts"] = attempts
                self.session.update_current_plan(updated)
                self._emit(
                    "step_updated",
                    {
                        "revision": revision,
                        "step": plan_module.get_step(updated, run.plan_step_id),
                    },
                )
            except Exception:
                logger.debug(
                    "could not project direct run %s", run.id, exc_info=True
                )
            return
        updated = current
        for node_id, state in run.nodes.items():
            if state.status in {"pending", "ready"}:
                continue
            target_step_id = node_id
            try:
                updated = plan_module.update_step(
                    updated,
                    target_step_id,
                    state.status,
                    summary=state.error
                    or (
                        str(state.detail.get("summary"))
                        if state.detail.get("summary")
                        else "completed"
                        if state.status == "succeeded"
                        else None
                    ),
                    execution={
                        "run_id": run.id,
                        "status": state.status,
                        "attempts": state.attempts,
                        "error": state.error,
                        "artifacts": [public_artifact(item) for item in state.artifacts],
                    },
                )
                # attempts is server-owned metadata but useful to the renderer.
                for row in updated.get("steps", []):
                    if row.get("id") == target_step_id:
                        row["attempts"] = state.attempts
                self.session.update_current_plan(updated)
                self._emit(
                    "step_updated",
                    {"revision": revision, "step": plan_module.get_step(updated, target_step_id)},
                )
            except Exception:
                logger.debug("could not project plan node %s", node_id, exc_info=True)

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
        if (
            step["kind"] not in plan_module.AGENT_REPORTABLE_KINDS
            or (step["kind"] == "review" and step.get("capability"))
        ):
            raise plan_module.PlanValidationError(
                "capability and workflow steps are updated only by their execution tools",
                public_message=(
                    f"step {step_id!r} is a {step['kind']} step; use its execution "
                    "tool instead of update_step"
                ),
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
        if (
            step["kind"] not in plan_module.AGENT_REPORTABLE_KINDS
            or (step["kind"] == "review" and step.get("capability"))
        ):
            raise plan_module.PlanValidationError(
                "artifacts may only be registered against a step you own",
                public_message=(
                    f"step {step_id!r} is a {step['kind']} step; its artifacts are "
                    "registered by its execution tool"
                ),
            )
        if step["status"] not in {"pending", "running"}:
            raise plan_module.PlanValidationError(
                "artifact registration step must be pending or running",
                public_message=(
                    f"step {step_id!r} cannot register artifacts from "
                    f"status {step['status']!r}"
                ),
            )
        plan_module.ensure_dependencies_succeeded(current, step_id)
        raw = payload.get("artifacts")
        if not isinstance(raw, list):
            raise LoomCraftError("artifacts must be an array")
        registered = self.session.register_scratch_artifacts(raw, step_id=step_id)
        for artifact in registered:
            self._emit("artifact_registered", {"artifact": artifact})
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
        planned_step = plan_module.get_step(current, step_id)
        expected_kind = "review" if planned_step["kind"] == "review" else "capability"
        plan_module.ensure_step_startable(
            current, step_id, kind=expected_kind, capability=capability_id
        )
        sources = capability.validate_inputs(payload.get("inputs"))
        parameters = capability.validate_parameters(payload.get("parameters"))
        # Resolve every source before touching the plan, so an unknown upload
        # fails the call rather than leaving a step stuck in `running`.
        typed_inputs = (
            {item.key: item for item in capability.inputs}
            if isinstance(capability, Capability)
            else {}
        )
        for key, refs in sources.items():
            values = refs if isinstance(refs, (list, tuple)) else [refs]
            for ref in values:
                if isinstance(ref, str) and ":" in ref:
                    resolved = self.session.resolve_source(ref)
                    allowed = tuple(
                        getattr(typed_inputs.get(key), "allowed_extensions", ())
                    )
                    if allowed and not resolved.filename.casefold().endswith(
                        tuple(extension.casefold() for extension in allowed)
                    ):
                        raise ContractError(
                            f"capability input {key!r} requires one of: "
                            + ", ".join(allowed)
                        )
                elif isinstance(capability, Capability):
                    raise ContractError("typed capability inputs must be source references")

        if isinstance(capability, Capability):
            graph = graph_from_capability(
                capability, sources=sources, parameters=parameters
            )
        elif isinstance(capability, CapabilitySpec):
            # Legacy JSON-Schema registrations are executed by the same Engine
            # through an ephemeral runner, so they retain retries, artifacts,
            # cancellation, and event semantics.
            from .plan_executor import _call

            handler = capability.handler
            canonical_runner = False
            if handler is None and capability.runner and self.registry.has_runner(capability.runner):
                handler = self.registry.runner(capability.runner)
                canonical_runner = True
            if handler is None:
                raise ContractError(
                    f"capability {capability.id!r} has no registered handler"
                )

            refs = (
                {
                    key: tuple(value if isinstance(value, list) else [value])
                    for key, value in sources.items()
                }
                if canonical_runner
                else {}
            )

            async def legacy_runner(
                ctx: Any,
                handler: Any = handler,
                raw_inputs: Mapping[str, Any] = sources,
                use_context: bool = canonical_runner,
            ) -> Any:
                if use_context:
                    return await _call(handler, ctx)
                from .plan_executor import PlanNodeContext

                parsed_plan = plan_module.parse_plan(current)
                adapter = PlanNodeContext(ctx, parsed_plan.step(step_id), parsed_plan)
                adapter.inputs = dict(raw_inputs)
                adapter.parameters = dict(parameters)
                return await _call(handler, adapter)

            graph = ExecutionGraph(
                id=f"cap-{secrets.token_hex(6)}",
                name=capability.name,
                nodes=(
                    ExecutionNode(
                        id="execute",
                        name=capability.name,
                        runner=capability.id,
                        runner_fn=legacy_runner,
                        inputs=refs,
                        parameters=parameters,
                        outputs=tuple(str(item.get("name", "output")) for item in capability.outputs if isinstance(item, Mapping)) or ("output",),
                    ),
                ),
                kind="capability",
                source_id=capability.id,
            )
        else:  # pragma: no cover - registry type guard
            raise LoomCraftError("registered capability has an unsupported contract")
        graph = self._apply_plan_policy(graph, current, step_id)
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
        plan_module.ensure_step_startable(
            current, step_id, kind="workflow", capability=workflow_id
        )
        sources = workflow.validate_inputs(payload.get("inputs"))
        parameters = workflow.validate_parameters(payload.get("parameters"))
        workflow_inputs = (
            {item.key: item for item in workflow.inputs}
            if isinstance(workflow, Workflow)
            else {}
        )
        for key, refs in sources.items():
            values = refs if isinstance(refs, (list, tuple)) else [refs]
            for ref in values:
                if isinstance(ref, str) and ":" in ref:
                    resolved = self.session.resolve_source(ref)
                    allowed = tuple(
                        getattr(workflow_inputs.get(key), "allowed_extensions", ())
                    )
                    if allowed and not resolved.filename.casefold().endswith(
                        tuple(extension.casefold() for extension in allowed)
                    ):
                        raise ContractError(
                            f"workflow input {key!r} requires one of: "
                            + ", ".join(allowed)
                        )
                elif isinstance(workflow, Workflow):
                    raise ContractError("typed workflow inputs must be source references")

        if isinstance(workflow, Workflow):
            graph = graph_from_workflow(workflow, sources=sources, parameters=parameters)
        elif isinstance(workflow, WorkflowSpec):
            from .plan_executor import _call

            async def legacy_workflow_runner(
                ctx: Any,
                handler: Any = workflow.handler,
                raw_inputs: Mapping[str, Any] = sources,
            ) -> Any:
                from .plan_executor import PlanNodeContext

                parsed_plan = plan_module.parse_plan(current)
                adapter = PlanNodeContext(ctx, parsed_plan.step(step_id), parsed_plan)
                adapter.inputs = dict(raw_inputs)
                adapter.parameters = dict(parameters)
                return await _call(handler, adapter)

            graph = ExecutionGraph(
                id=f"wf-{secrets.token_hex(6)}",
                name=workflow.name,
                nodes=(
                    ExecutionNode(
                        id="execute",
                        name=workflow.name,
                        runner=workflow.id,
                        runner_fn=legacy_workflow_runner,
                        inputs={},
                        parameters=parameters,
                        outputs=tuple(str(item.get("name", "output")) for item in workflow.outputs if isinstance(item, Mapping)) or ("output",),
                    ),
                ),
                kind="workflow",
                source_id=workflow.id,
            )
        else:  # pragma: no cover
            raise LoomCraftError("registered workflow has an unsupported contract")
        graph = self._apply_plan_policy(graph, current, step_id)
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
            # ``Run.wait`` completes only for terminal runs. Approval pauses
            # intentionally leave the handle open, so return the paused state
            # to the caller instead of hanging the agent turn.
            while run.status not in {"succeeded", "failed", "cancelled", "paused_approval"}:
                await asyncio.sleep(0.01)
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
            for row in run.failed_nodes
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
        execution = {
            "id": run.id,
            "kind": kind,
            "capability": target_id,
            "capability_version": version,
            "status": run.status,
            "step_id": step_id,
            "duration_seconds": round(time.monotonic() - started, 3),
            "attempts": max((state.attempts for state in run.nodes.values()), default=0),
            "parameters": dict(parameters),
            "failed_nodes": run.failed_nodes,
            "error": None if ok or paused else failure,
            "artifacts": artifacts,
        }
        self.session.record_execution(execution)

        node_summary = next(
            (
                str(state.detail["summary"])
                for state in run.nodes.values()
                if state.detail.get("summary")
            ),
            None,
        )
        summary = (
            node_summary or f"{label} succeeded; produced {len(artifacts)} artifact(s)"
            if ok
            else f"{label} is waiting for approval"
            if paused
            else f"{label} failed — {failure}"
        )
        projected_status = (
            "succeeded"
            if ok
            else "waiting_approval"
            if paused
            else "failed"
        )
        self._set_step(
            step_id,
            projected_status,
            summary=summary,
            execution={"kind": kind, "id": run.id, "status": run.status},
        )
        self._emit("execution_finished", {"step_id": step_id, "execution": execution})
        return ToolResponse(
            ok=ok or paused,
            result=execution,
            error=None if ok or paused else summary,
            error_code=None if ok or paused else "EXECUTION_FAILED",
        )

    @staticmethod
    def _apply_plan_policy(
        graph: ExecutionGraph, plan: Mapping[str, Any], step_id: str
    ) -> ExecutionGraph:
        """Overlay Plan-level retry/timeout/failure policy on a graph."""
        raw_step = next(
            (
                row
                for row in plan.get("steps", [])
                if isinstance(row, Mapping) and row.get("id") == step_id
            ),
            None,
        )
        if not isinstance(raw_step, Mapping):
            return graph
        # ``validate_plan`` materialises the model defaults into every step.
        # Those defaults must not erase a capability's own execution policy:
        # an omitted plan retry block should still allow a capability declared
        # with ``max_attempts=3`` to retry (this is especially important for
        # direct ``run_capability`` calls).  A non-default plan value is an
        # explicit override and wins over the catalog contract.
        retry = raw_step.get("retry") if isinstance(raw_step.get("retry"), Mapping) else {}
        raw_attempts = retry.get("max_attempts")
        raw_backoff = retry.get("backoff_seconds")
        raw_multiplier = retry.get("backoff_multiplier")
        raw_max_backoff = retry.get("max_backoff_seconds")
        timeout = raw_step.get("timeout_seconds")
        policy = str(raw_step.get("on_failure", "stop"))

        def overlay(node: ExecutionNode) -> ExecutionNode:
            inherited_attempts = max(1, int(node.max_attempts or 1))
            inherited_backoff = max(0.0, float(node.retry_backoff_seconds or 0.0))
            try:
                requested_attempts = (
                    max(1, int(raw_attempts))
                    if raw_attempts is not None
                    else inherited_attempts
                )
            except (TypeError, ValueError):
                requested_attempts = inherited_attempts
            try:
                requested_backoff = (
                    max(0.0, float(raw_backoff))
                    if raw_backoff is not None
                    else inherited_backoff
                )
            except (TypeError, ValueError):
                requested_backoff = inherited_backoff
            try:
                requested_multiplier = (
                    max(1.0, float(raw_multiplier))
                    if raw_multiplier is not None
                    else node.retry_backoff_multiplier
                )
            except (TypeError, ValueError):
                requested_multiplier = node.retry_backoff_multiplier
            try:
                requested_max_backoff = (
                    max(0.0, float(raw_max_backoff))
                    if raw_max_backoff is not None
                    else node.retry_max_backoff_seconds
                )
            except (TypeError, ValueError):
                requested_max_backoff = node.retry_max_backoff_seconds

            # A normalized default (1 attempt, 0 seconds) means “not
            # specified” when the catalog has a stronger policy. Explicit
            # non-default values remain genuine plan-level overrides.
            max_attempts = (
                requested_attempts
                if requested_attempts != 1 or inherited_attempts <= 1
                else inherited_attempts
            )
            backoff = (
                requested_backoff
                if requested_backoff != 0.0 or inherited_backoff == 0.0
                else inherited_backoff
            )
            plan_retry_override = (
                requested_attempts != 1
                or requested_backoff != 0.0
                or requested_multiplier != 2.0
                or requested_max_backoff not in {None, 60.0}
            )
            timeout_value = node.timeout_seconds if timeout is None else float(timeout)
            return replace(
                node,
                max_attempts=max_attempts,
                retry_backoff_seconds=backoff,
                retry_backoff_multiplier=(
                    requested_multiplier
                    if plan_retry_override
                    else node.retry_backoff_multiplier
                ),
                retry_max_backoff_seconds=(
                    requested_max_backoff
                    if plan_retry_override
                    else node.retry_max_backoff_seconds
                ),
                timeout_seconds=timeout_value,
                on_failure=(
                    policy
                    if policy in {"stop", "continue", "require_approval"}
                    else node.on_failure
                ),
            )

        return replace(
            graph,
            nodes=tuple(overlay(node) for node in graph.nodes),
        )

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
        if self._legacy_backend is not None:
            return self._legacy_backend.fulfill_input_request(request_id)
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

    def fulfill_inputs(self, request_id: str, uploads: Any = None) -> dict[str, Any]:
        """Compatibility spelling; uploads are read from the Session manifest."""
        if self._legacy_backend is not None:
            return self._legacy_backend.fulfill_inputs(request_id, uploads or [])
        return self.fulfill_input_request(request_id)

    def cancel_input_request(self, request_id: str) -> dict[str, Any]:
        """Give up on a request; the agent must continue without those files."""
        if self._legacy_backend is not None:
            self._legacy_backend.cancel_input_request(request_id)
            return {"request_id": request_id, "cancelled": True}
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
        if self._legacy_backend is not None:
            return self._legacy_backend.invalidate_requests_for_upload(upload_id)
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


def _short_string(value: object, name: str, maximum: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise LoomCraftError(f"{name} must be a non-empty string")
    text = value.strip()
    if len(text) > maximum or any(ord(character) < 32 for character in text):
        raise LoomCraftError(f"{name} is invalid or exceeds {maximum} characters")
    return text


def _bounded_int(value: object, name: str, minimum: int, maximum: int) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise LoomCraftError(f"{name} must be an integer")
    if not minimum <= value <= maximum:
        raise LoomCraftError(f"{name} must be between {minimum} and {maximum}")
    return value


__all__ = ["BrokerLimits", "ToolBroker", "ToolResponse"]
