"""AI-facing dynamic tools for publishing and operating a Loomcraft plan.

The broker is the trust boundary between a model and the engine.  It exposes a
small, JSON-only tool surface, validates every mutating request, and delegates
domain work to injected registries/handlers.
"""

from __future__ import annotations

import json
import math
import secrets
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence

from .events import Event
from .legacy_executor import DAGExecutor, ExecutionError
from .models import (
    InputRequestValidationError,
    PlanValidationError,
    STEP_STATUSES,
    get_step,
    task_phase,
    update_step,
    validate_input_request,
    validate_input_fulfillment,
    validate_plan,
)
from .registry import Registry
from .legacy_storage import InMemoryStore, SessionStore


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
# Stable semantic aliases used by integrations that previously consumed the
# extracted application's broker constants.
ERROR_CAPABILITY_FAILED = "BROKER_CAPABILITY_EXECUTION_FAILED"
ERROR_WORKFLOW_FAILED = "BROKER_WORKFLOW_EXECUTION_FAILED"
ERROR_INPUT_INTEGRITY = "BROKER_INPUT_INTEGRITY_FAILED"
ERROR_EXECUTION_CLEANUP_PENDING = "BROKER_EXECUTION_CLEANUP_PENDING"
ERROR_STOPPING = "BROKER_STOPPING"
BROKER_MAX_ACTIONS_PER_TURN = 64
BROKER_MAX_IDENTICAL_ACTIONS = 3


@dataclass
class ActionResponse:
    ok: bool
    result: Optional[Dict[str, Any]] = None
    error: Optional[str] = None
    error_code: Optional[str] = None

    def as_dict(self) -> Dict[str, Any]:
        value: Dict[str, Any] = {"ok": self.ok}
        if self.result is not None:
            value["result"] = self.result
        if self.error:
            value["error"] = self.error[:4000]
        if not self.ok:
            value["error_code"] = self.error_code or ERROR_ACTION_FAILED
        return value

    def to_json(self) -> str:
        return json.dumps(self.as_dict(), ensure_ascii=False, allow_nan=False)


def _schema_tool(name: str, description: str, properties: Mapping[str, Any], required: Sequence[str]) -> Dict[str, Any]:
    schema = {
        "type": "object",
        "properties": dict(properties),
        "required": list(required),
        "additionalProperties": False,
    }
    # ``inputSchema`` mirrors the Codex app-server contract.  ``function`` is
    # included for OpenAI-compatible tool APIs, so one spec works in both.
    return {
        "type": "function",
        "name": name,
        "description": description,
        "inputSchema": schema,
        "function": {"name": name, "description": description, "parameters": schema},
    }


def dynamic_tool_specs() -> List[Dict[str, Any]]:
    """Return the stable native tool contract exposed to an AI provider."""
    nonempty = {"type": "string", "minLength": 1}
    plan_step = {
        "type": "object",
        "properties": {
            "id": {"type": "string", "pattern": r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$"},
            "title": {"type": "string", "minLength": 1, "maxLength": 160},
            "kind": {"type": "string", "enum": ["answer", "capability", "workflow", "dynamic", "review"]},
            "depends_on": {"type": "array", "items": {"type": "string"}, "maxItems": 24},
            "capability": {"type": ["string", "null"]},
            "description": {"type": "string", "maxLength": 1000},
            "status": {"type": "string", "enum": list(STEP_STATUSES)},
            "summary": {"type": ["string", "null"]},
            "execution": {"type": ["object", "null"]},
            "retry": {
                "type": "object",
                "properties": {
                    "max_attempts": {"type": "integer", "minimum": 0, "maximum": 20},
                    "backoff_seconds": {"type": "number", "minimum": 0, "maximum": 3600},
                    "backoff_multiplier": {"type": "number", "minimum": 1, "maximum": 10},
                    "max_backoff_seconds": {"type": "number", "minimum": 0, "maximum": 86400},
                },
                "additionalProperties": False,
            },
            "timeout_seconds": {"type": ["number", "null"], "minimum": 0.001},
            "on_failure": {"type": "string", "enum": ["stop", "continue", "require_approval"]},
            "metadata": {"type": "object"},
        },
        "required": ["id", "title", "kind"],
        "additionalProperties": False,
    }
    objective = {
        "type": "object",
        "properties": {
            "id": {"type": "string", "pattern": r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$"},
            "question": {"type": "string", "minLength": 1, "maxLength": 1000},
            "status": {"type": ["string", "null"], "enum": ["planned", "executed", "not_estimable", "blocked", "deferred_by_scope", None]},
            "estimand": {"type": "string", "maxLength": 500},
            "independent_unit": {"type": "string", "maxLength": 300},
            "expected_outputs": {"type": "array", "maxItems": 12, "items": {"type": "string", "minLength": 1, "maxLength": 300}},
            "method_families": {"type": "array", "maxItems": 12, "items": {"type": "string", "minLength": 1, "maxLength": 300}},
            "validation_requirements": {"type": "array", "maxItems": 12, "items": {"type": "string", "minLength": 1, "maxLength": 300}},
        },
        "required": ["id", "question"],
        "additionalProperties": False,
    }
    coverage = {
        "type": "object",
        "properties": {
            "objective_id": {"type": "string", "pattern": r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$"},
            "status": {"type": "string", "enum": ["planned", "executed", "not_estimable", "blocked", "deferred_by_scope"]},
            "reason": {"type": "string", "minLength": 1, "maxLength": 1000},
            "selected_method": {"type": ["string", "null"], "maxLength": 300},
            "step_ids": {"type": "array", "maxItems": 12, "items": {"type": "string"}},
            "artifact_refs": {"type": "array", "maxItems": 12, "items": {"type": "string"}},
            "next_action": {"type": ["string", "null"], "maxLength": 500},
        },
        "required": ["objective_id", "status", "reason"],
        "additionalProperties": False,
    }
    plan = {
        "type": "object",
        "properties": {
            "goal": {"type": "string", "minLength": 1, "maxLength": 2000},
            "summary": {"type": "string", "maxLength": 2000},
            "revision": {"type": "integer", "minimum": 1},
            "reason": {"type": ["string", "null"], "maxLength": 2000},
            "analysis_profile": {"type": ["string", "null"], "maxLength": 500},
            "objectives": {"type": "array", "maxItems": 64, "items": objective},
            "analysis_coverage": {"type": "array", "maxItems": 64, "items": coverage},
            "steps": {"type": "array", "minItems": 1, "maxItems": 256, "items": plan_step},
            "metadata": {"type": "object"},
        },
        "required": ["goal", "revision", "steps"],
        "additionalProperties": False,
    }
    return [
        _schema_tool("session_context", "Return trusted uploads, current plan, execution history, and catalog facts.", {}, []),
        _schema_tool("catalog_search", "Search registered capabilities and workflows. Discovery never authorizes execution.", {"query": {"type": "string"}, "scope": {"type": "string", "enum": ["all", "capabilities", "workflows", "operations", "tools", "skills", "runners"]}, "limit": {"type": "integer", "minimum": 1, "maximum": 100}}, ["query"]),
        _schema_tool("capability_search", "Retrieve typed atomic capability contracts before planning.", {"query": nonempty, "limit": {"type": "integer", "minimum": 1, "maximum": 100}}, ["query"]),
        _schema_tool("operation_search", "Search semantic operation metadata supplied by the host application.", {"query": nonempty, "limit": {"type": "integer", "minimum": 1, "maximum": 100}}, ["query"]),
        _schema_tool("inspect_table", "Inspect a bounded host-owned table without modifying it.", {"source_ref": nonempty, "format": {"type": "string"}, "max_rows": {"type": "integer", "minimum": 1, "maximum": 1000}, "sheet": {"type": ["string", "null"]}, "table": {"type": ["string", "null"]}}, ["source_ref"]),
        _schema_tool("publish_plan", "Validate and publish a complete, versioned DAG before execution.", {"plan": plan}, ["plan"]),
        _schema_tool("request_inputs", "Publish a structured request for missing user files and pause the turn.", {"request": {"type": "object", "properties": {"title": {"type": "string", "minLength": 1, "maxLength": 160}, "message": {"type": "string", "minLength": 1, "maxLength": 2000}, "requirements": {"type": "array", "minItems": 1, "maxItems": 24, "items": {"type": "object", "properties": {"key": {"type": "string", "pattern": r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$"}, "label": {"type": "string", "minLength": 1}, "description": {"type": "string", "minLength": 1}, "required": {"type": "boolean"}, "min_files": {"type": "integer", "minimum": 0}, "max_files": {"type": "integer", "minimum": 1}, "allowed_extensions": {"type": "array", "items": {"type": "string"}}, "field_hints": {"type": "array", "items": {"type": "string"}}}, "required": ["key", "label", "description", "required", "min_files", "max_files", "allowed_extensions", "field_hints"], "additionalProperties": False}}, "continue_prompt": {"type": "string", "minLength": 1, "maxLength": 2000}}, "required": ["title", "message", "requirements", "continue_prompt"], "additionalProperties": False}}, ["request"]),
        _schema_tool("update_step", "Update an answer, dynamic, or review step with a trusted status.", {"step_id": nonempty, "status": {"type": "string", "enum": list(STEP_STATUSES)}, "summary": {"type": ["string", "null"]}}, ["step_id", "status"]),
        _schema_tool("run_capability", "Run one typed atomic capability authorized by a capability step.", {"capability_id": nonempty, "step_id": nonempty, "inputs": {"type": "object"}, "parameters": {"type": "object"}}, ["capability_id", "step_id", "inputs"]),
        _schema_tool("run_workflow", "Run one registered workflow authorized by a workflow step.", {"workflow_id": nonempty, "step_id": nonempty, "inputs": {"type": "object"}}, ["workflow_id", "step_id", "inputs"]),
        _schema_tool("execute_plan", "Execute all runnable plan steps with dependency-aware parallelism.", {"inputs": {"type": "object"}, "timeout_seconds": {"type": ["number", "null"], "minimum": 0.001}}, []),
        _schema_tool("register_artifacts", "Register completed host-owned artifacts for a dynamic or review step.", {"step_id": nonempty, "artifacts": {"type": "array", "minItems": 1, "maxItems": 12, "items": {"type": "object"}}}, ["step_id", "artifacts"]),
        _schema_tool("register_artifact", "Register one completed host-owned artifact (path/display_name may be used with a local artifact store).", {"step_id": nonempty, "artifact": {"type": "object"}, "path": {"type": "string"}, "display_name": {"type": ["string", "null"]}}, ["step_id"]),
        _schema_tool("knowledge_list", "List a bounded logical path from an injected knowledge provider.", {"scope": {"type": "string"}, "path": {"type": "string"}, "limit": {"type": "integer", "minimum": 1, "maximum": 100}}, []),
        _schema_tool("knowledge_search", "Search an injected knowledge provider.", {"query": {"type": "string", "minLength": 1}, "scope": {"type": "string"}, "limit": {"type": "integer", "minimum": 1, "maximum": 100}}, ["query"]),
        _schema_tool("knowledge_read", "Read a bounded logical text resource from an injected knowledge provider.", {"path": nonempty, "scope": {"type": "string"}, "offset": {"type": "integer", "minimum": 0}, "limit": {"type": "integer", "minimum": 1, "maximum": 49152}}, ["path"]),
    ]


class ToolBroker:
    """Validate and dispatch model tool calls for one session."""

    def __init__(
        self,
        session_id: str,
        registry: Registry,
        *,
        store: Optional[SessionStore] = None,
        executor: Optional[DAGExecutor] = None,
        on_event: Optional[Callable[[Dict[str, Any]], None]] = None,
        table_inspector: Optional[Callable[[str, Mapping[str, Any]], Any]] = None,
        catalog_provider: Optional[Callable[[str, str, int], Any]] = None,
        knowledge_provider: Any = None,
        artifact_store: Any = None,
        source_resolver: Any = None,
        extra_tool_handlers: Optional[Mapping[str, Callable[[Mapping[str, Any]], Any]]] = None,
        max_actions: Optional[int] = None,
        max_identical_actions: Optional[int] = None,
    ) -> None:
        if not session_id:
            raise ValueError("session_id is required")
        self.session_id = session_id
        self.registry = registry
        if store is None and executor is not None:
            store = getattr(executor, "store", None)
        self.store = store or InMemoryStore()
        if executor is not None and getattr(executor, "store", self.store) is not self.store:
            raise ValueError("broker and executor must use the same store")
        if executor is not None and getattr(executor, "registry", registry) is not registry:
            raise ValueError("broker and executor must use the same registry")
        self.store.ensure_session(session_id) if hasattr(self.store, "ensure_session") else None
        self.executor = executor or DAGExecutor(registry, store=self.store, on_event=on_event)
        self.on_event = on_event
        self.table_inspector = table_inspector
        self.catalog_provider = catalog_provider
        self.knowledge_provider = knowledge_provider
        self.artifact_store = artifact_store
        self.source_resolver = source_resolver
        self._extra_tool_handlers = dict(extra_tool_handlers or {})
        self.max_actions = max(1, int(BROKER_MAX_ACTIONS_PER_TURN if max_actions is None else max_actions))
        self.max_identical_actions = max(1, int(BROKER_MAX_IDENTICAL_ACTIONS if max_identical_actions is None else max_identical_actions))
        self._action_count = 0
        self._repeats: Dict[str, int] = {}
        self._active_execution = False
        self._stopping = False
        self._awaiting_inputs = bool(self.store.pending_input_requests(session_id)) if hasattr(self.store, "pending_input_requests") else False
        self._knowledge_version: Optional[str] = None

    @staticmethod
    def dynamic_tool_specs() -> List[Dict[str, Any]]:
        return dynamic_tool_specs()

    async def dispatch(self, action: str, payload: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
        """Short alias for dispatch_dynamic_tool used by HTTP adapters."""
        return await self.dispatch_dynamic_tool(action, payload)

    def _emit(self, event: str, data: Mapping[str, Any]) -> Event:
        row = self.store.append_event(self.session_id, event, dict(data))
        if isinstance(row, Event):
            outgoing = row.as_dict()
        elif isinstance(row, Mapping):
            outgoing = dict(row)
            outgoing.setdefault("event", event)
            outgoing.setdefault("data", dict(data))
        else:
            outgoing = {"event": event, "data": dict(data)}
        if self.on_event is not None:
            try:
                self.on_event(outgoing)
            except Exception:
                pass
        return row if isinstance(row, Event) else Event(int(outgoing.get("seq", 1)), str(outgoing.get("event", event)), dict(outgoing.get("data", data)), outgoing.get("ts"))

    def begin_turn(self) -> None:
        """Reset per-turn action budgets while preserving session state."""
        self._action_count = 0
        self._repeats.clear()
        self._awaiting_inputs = bool(self.store.pending_input_requests(self.session_id)) if hasattr(self.store, "pending_input_requests") else False

    @staticmethod
    def _string(payload: Mapping[str, Any], key: str, maximum: int = 500) -> str:
        value = payload.get(key)
        if not isinstance(value, str) or not value.strip() or len(value.strip()) > maximum or any(ord(char) < 32 for char in value):
            raise ValueError("%s must be a non-empty bounded string" % key)
        return value.strip()

    async def dispatch_dynamic_tool(self, action: str, payload: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
        if self._stopping:
            return ActionResponse(False, error="broker is stopping", error_code=ERROR_STOPPING).as_dict()
        action = str(action or "").replace("-", "_")
        if payload is None:
            value: Dict[str, Any] = {}
        elif isinstance(payload, Mapping):
            value = dict(payload)
        else:
            return ActionResponse(False, error="tool payload must be an object", error_code=ERROR_INVALID_ARGUMENT).as_dict()
        self._action_count += 1
        if self._action_count > self.max_actions:
            return ActionResponse(False, error="broker action budget exceeded", error_code=ERROR_ACTION_LIMIT).as_dict()
        try:
            signature = json.dumps({"action": action, "payload": value}, ensure_ascii=False, sort_keys=True, default=str, allow_nan=False)
        except (TypeError, ValueError):
            signature = "%s:%s" % (action, id(value))
        if len(signature.encode("utf-8")) > 512 * 1024:
            return ActionResponse(False, error="tool payload exceeds the size limit", error_code=ERROR_INVALID_ARGUMENT).as_dict()
        self._repeats[signature] = self._repeats.get(signature, 0) + 1
        if self._repeats[signature] > self.max_identical_actions:
            return ActionResponse(False, error="identical broker action repeated without progress: %s" % action, error_code=ERROR_ACTION_REPEATED).as_dict()
        try:
            response = await self._dispatch(action, value)
        except PlanValidationError as exc:
            response = ActionResponse(False, error="plan or step validation failed: %s" % getattr(exc, "public_message", str(exc)), error_code=ERROR_PLAN_INVALID)
        except InputRequestValidationError:
            response = ActionResponse(False, error="input request validation failed", error_code=ERROR_INPUT_REQUEST_INVALID)
        except ValueError as exc:
            response = ActionResponse(False, error="tool arguments failed validation: %s" % str(exc)[:1000], error_code=ERROR_INVALID_ARGUMENT)
        except ExecutionError as exc:
            response = ActionResponse(False, error=str(exc)[:2000], error_code=ERROR_EXECUTION_BUSY)
        except Exception:
            response = ActionResponse(False, error="intelligent tool execution failed", error_code=ERROR_INTERNAL)
        return response.as_dict()

    async def _dispatch(self, action: str, payload: Dict[str, Any]) -> ActionResponse:
        readonly = {"session_context", "catalog_search", "capability_search", "operation_search", "inspect_table", "knowledge_list", "knowledge_search", "knowledge_read"}
        if action in readonly:
            return await self._readonly(action, payload)
        if self._awaiting_inputs:
            return ActionResponse(False, error="this turn is waiting for user files", error_code=ERROR_AWAITING_INPUTS)
        if action == "publish_plan":
            return self._publish_plan(payload)
        if action == "request_inputs":
            return self._request_inputs(payload)
        if action == "update_step":
            return self._update_step(payload)
        if action == "run_capability":
            return await self._run_authorized(payload, "capability")
        if action == "run_workflow":
            return await self._run_authorized(payload, "workflow")
        if action == "execute_plan":
            return await self._execute_plan(payload)
        if action in {"register_artifacts", "register_artifact"}:
            return self._register_artifacts(payload, single=action == "register_artifact")
        extra = self._extra_tool_handlers.get(action)
        if extra is not None:
            value = extra(payload)
            if hasattr(value, "__await__"):
                value = await value
            return ActionResponse(True, dict(value) if isinstance(value, Mapping) else {"result": value})
        return ActionResponse(False, error="unsupported broker action", error_code=ERROR_UNSUPPORTED_ACTION)

    async def _readonly(self, action: str, payload: Dict[str, Any]) -> ActionResponse:
        if action == "session_context":
            if payload:
                raise ValueError("session_context accepts no arguments")
            current = self.store.get_current_plan(self.session_id)
            session_meta = self.store.get_session(self.session_id) if hasattr(self.store, "get_session") else {}
            busy = self._awaiting_inputs or (isinstance(session_meta, Mapping) and session_meta.get("status") in {"running", "waiting_approval"})
            catalog = self.registry.catalog()
            snapshot = getattr(self.catalog_provider, "snapshot", None) if self.catalog_provider is not None else None
            if callable(snapshot):
                extra_catalog = snapshot()
                if hasattr(extra_catalog, "__await__"):
                    extra_catalog = await extra_catalog
                if isinstance(extra_catalog, Mapping):
                    catalog.update(dict(extra_catalog))
            return ActionResponse(True, {
                "session_id": self.session_id,
                "task_phase": task_phase(current, busy=busy),
                "current_plan": current,
                "plan_history": self.store.list_plan_history(self.session_id),
                "recent_executions": self.store.list_executions(self.session_id)[-20:],
                "messages": self.store.list_messages(self.session_id)[-20:] if hasattr(self.store, "list_messages") else [],
                "artifacts": self.store.list_artifacts(self.session_id) if hasattr(self.store, "list_artifacts") else [],
                "uploads": self.store.list_uploads(self.session_id) if hasattr(self.store, "list_uploads") else [],
                "pending_input_requests": self.store.pending_input_requests(self.session_id) if hasattr(self.store, "pending_input_requests") else [],
                "knowledge_version": self._knowledge_version,
                "catalog": catalog,
            })
        if action == "inspect_table":
            if self.table_inspector is None:
                return ActionResponse(False, error="no table inspector is configured", error_code=ERROR_UNSUPPORTED_ACTION)
            source_ref = self._string(payload, "source_ref")
            inspection_payload = dict(payload)
            if self.source_resolver is not None:
                resolved = self.source_resolver.resolve(self.session_id, source_ref)
                inspection_payload["resolved_path"] = resolved["path"]
            value = self.table_inspector(source_ref, inspection_payload)
            if hasattr(value, "__await__"):
                value = await value
            return ActionResponse(True, dict(value) if isinstance(value, Mapping) else {"result": value})
        query = self._string(payload, "query", 300) if action != "knowledge_list" and action != "knowledge_read" else ""
        if action == "catalog_search":
            scope = str(payload.get("scope", "all"))
            limit = int(payload.get("limit", 10))
            allowed_scopes = {"all", "capabilities", "workflows", "operations", "tools", "skills", "runners"}
            rows = self.registry.search(query, scope if scope in allowed_scopes else "all", limit)
            if self.catalog_provider is not None and scope not in {"capabilities", "workflows"}:
                extra = self.catalog_provider(query, scope, limit)
                if hasattr(extra, "__await__"):
                    extra = await extra
                if isinstance(extra, list):
                    rows = rows + [dict(item) for item in extra if isinstance(item, Mapping)]
            return ActionResponse(True, {"scope": scope, "query": query, "results": rows[:limit]})
        if action == "capability_search":
            return ActionResponse(True, {"scope": "capabilities", "query": query, "results": self.registry.search(query, "capabilities", int(payload.get("limit", 10)))})
        if action == "operation_search":
            if self.catalog_provider is None:
                return ActionResponse(True, {"scope": "operations", "query": query, "results": self.registry.search(query, "operations", int(payload.get("limit", 10)))})
            rows = self.catalog_provider(query, "operations", int(payload.get("limit", 10)))
            if hasattr(rows, "__await__"):
                rows = await rows
            return ActionResponse(True, {"scope": "operations", "query": query, "results": rows if isinstance(rows, list) else []})
        if self.knowledge_provider is None:
            return ActionResponse(False, error="no knowledge provider is configured", error_code=ERROR_UNSUPPORTED_ACTION)
        method_name = {"knowledge_list": "list", "knowledge_search": "search", "knowledge_read": "read"}[action]
        method = getattr(self.knowledge_provider, method_name, None)
        if not callable(method):
            return ActionResponse(False, error="knowledge provider does not implement %s" % method_name, error_code=ERROR_UNSUPPORTED_ACTION)
        value = method(payload)
        if hasattr(value, "__await__"):
            value = await value
        if isinstance(value, Mapping) and isinstance(value.get("version"), str):
            version = str(value["version"])
            if self._knowledge_version is None:
                self._knowledge_version = version
            elif self._knowledge_version != version:
                return ActionResponse(False, error="knowledge snapshot changed during this session", error_code=ERROR_KNOWLEDGE_UNAVAILABLE)
        return ActionResponse(True, dict(value) if isinstance(value, Mapping) else {"result": value})

    def _publish_plan(self, payload: Mapping[str, Any]) -> ActionResponse:
        current = self.store.get_current_plan(self.session_id)
        plan = validate_plan(payload.get("plan"), current=current, registry=self.registry)
        if current and any(step.get("status") == "running" for step in current.get("steps", [])):
            raise PlanValidationError("cannot replace a plan while a step is running")
        self.store.publish_plan(self.session_id, plan)
        self._emit("plan_published", {"plan": plan})
        return ActionResponse(True, {"plan": plan})

    def _request_inputs(self, payload: Mapping[str, Any]) -> ActionResponse:
        if self._awaiting_inputs:
            return ActionResponse(False, error="an input request is already pending", error_code=ERROR_AWAITING_INPUTS)
        request = validate_input_request(payload.get("request"))
        self._awaiting_inputs = True
        self._emit("input_required", {"request": request})
        return ActionResponse(True, {"request": request})

    @staticmethod
    def _deps_succeeded(current: Mapping[str, Any], step_id: str) -> None:
        by_id = {step["id"]: step for step in current.get("steps", [])}
        step = by_id.get(step_id)
        if step is None:
            raise PlanValidationError("unknown plan step %r" % step_id)
        incomplete = [dependency for dependency in step.get("depends_on", []) if by_id.get(dependency, {}).get("status") != "succeeded"]
        if incomplete:
            raise PlanValidationError("step %r has incomplete dependencies: %s" % (step_id, ", ".join(incomplete)))

    def _update_step(self, payload: Mapping[str, Any]) -> ActionResponse:
        current = self.store.get_current_plan(self.session_id)
        if current is None:
            raise PlanValidationError("publish a task plan before updating steps")
        step_id = self._string(payload, "step_id", 64)
        step = get_step(current, step_id)
        if step["kind"] not in {"answer", "dynamic", "review"}:
            raise PlanValidationError("capability and workflow steps are updated by execution tools")
        status = self._string(payload, "status", 32)
        if status in {"running", "succeeded"}:
            self._deps_succeeded(current, step_id)
        summary = payload.get("summary")
        if summary is not None and not isinstance(summary, str):
            raise ValueError("summary must be a string or null")
        updated = update_step(current, step_id, status, summary=summary)
        self.store.update_current_plan(self.session_id, updated)
        changed = get_step(updated, step_id)
        self._emit("step_updated", {"revision": updated["revision"], "step": changed})
        return ActionResponse(True, {"step": changed})

    async def _run_authorized(self, payload: Mapping[str, Any], kind: str) -> ActionResponse:
        if self._active_execution:
            return ActionResponse(False, error="a previous execution is still active", error_code=ERROR_EXECUTION_BUSY)
        self._active_execution = True
        try:
            return await self._run_authorized_inner(payload, kind)
        finally:
            self._active_execution = False

    async def _run_authorized_inner(self, payload: Mapping[str, Any], kind: str) -> ActionResponse:
        identifier = self._string(payload, "%s_id" % kind, 160)
        step_id = self._string(payload, "step_id", 64)
        current = self.store.get_current_plan(self.session_id)
        if current is None:
            raise PlanValidationError("publish a task plan before execution")
        planned_step = get_step(current, step_id)
        if kind == "capability" and self.registry.capability(identifier) is None:
            raise ValueError("unknown capability %r" % identifier)
        if kind == "workflow" and self.registry.workflow(identifier) is None:
            raise ValueError("unknown workflow %r" % identifier)
        inputs = payload.get("inputs", {})
        if not isinstance(inputs, Mapping):
            raise ValueError("inputs must be an object")
        if kind == "capability":
            spec = self.registry.capability(identifier)
            assert spec is not None
            inputs = spec.validate_inputs(inputs)
            parameters = payload.get("parameters", {})
            parameters = spec.validate_parameters(parameters)
        else:
            spec = self.registry.workflow(identifier)
            assert spec is not None
            inputs = spec.validate_inputs(inputs)
            parameters = {}
        execution = await self.executor.execute_step(
            self.session_id,
            step_id,
            inputs=dict(inputs),
            parameters=parameters,
            expected_kind=(
                "review"
                if planned_step.get("kind") == "review"
                else kind
            ),
            expected_capability=identifier,
        )
        failure_code = ERROR_CAPABILITY_FAILED if kind == "capability" else ERROR_WORKFLOW_FAILED
        return ActionResponse(execution.get("status") == "succeeded", execution, None if execution.get("status") == "succeeded" else "step execution failed", failure_code if execution.get("status") != "succeeded" else None)

    async def _execute_plan(self, payload: Mapping[str, Any]) -> ActionResponse:
        if self._active_execution:
            return ActionResponse(False, error="a previous execution is still active", error_code=ERROR_EXECUTION_BUSY)
        self._active_execution = True
        try:
            return await self._execute_plan_inner(payload)
        finally:
            self._active_execution = False

    async def _execute_plan_inner(self, payload: Mapping[str, Any]) -> ActionResponse:
        current = self.store.get_current_plan(self.session_id)
        if current is None:
            raise PlanValidationError("publish a task plan before execution")
        inputs = payload.get("inputs", {})
        if not isinstance(inputs, Mapping):
            raise ValueError("inputs must be an object")
        timeout = payload.get("timeout_seconds")
        if timeout is not None and (isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(float(timeout)) or timeout <= 0):
            raise ValueError("timeout_seconds must be a positive number")
        result = await self.executor.execute(current, session_id=self.session_id, inputs=dict(inputs), timeout_seconds=float(timeout) if timeout is not None else None)
        return ActionResponse(result.status == "succeeded", result.as_dict(), None if result.status == "succeeded" else "plan execution finished with status %s" % result.status, None if result.status == "succeeded" else ERROR_ACTION_FAILED)

    def _register_artifacts(self, payload: Mapping[str, Any], single: bool = False) -> ActionResponse:
        current = self.store.get_current_plan(self.session_id)
        if current is None:
            raise PlanValidationError("publish a task plan before registering artifacts")
        step_id = self._string(payload, "step_id", 64)
        step = get_step(current, step_id)
        if step["kind"] not in {"dynamic", "review"}:
            raise PlanValidationError("only dynamic and review steps may register artifacts")
        if step["status"] not in {"pending", "running"}:
            raise PlanValidationError("artifact registration step must be pending or running")
        self._deps_succeeded(current, step_id)
        if single:
            single_artifact = payload.get("artifact")
            if single_artifact is None and payload.get("path") is not None:
                single_artifact = {"path": payload.get("path"), "display_name": payload.get("display_name")}
            raw = [single_artifact]
        else:
            raw = payload.get("artifacts")
        if not isinstance(raw, list) or not 1 <= len(raw) <= 12:
            raise ValueError("artifacts must contain 1..12 objects")
        rows: List[Dict[str, Any]] = []
        seen_paths = set()
        if self.artifact_store is not None and not single and all(isinstance(item, Mapping) and isinstance(item.get("path"), str) for item in raw):
            register_batch = getattr(self.artifact_store, "register_batch", None)
            if callable(register_batch):
                normalized_batch = []
                seen_batch = set()
                for item in raw:
                    entry = dict(item)
                    entry["path"] = entry["path"].replace("\\", "/")
                    if entry["path"].startswith("/") or entry["path"] in seen_batch:
                        raise ValueError("artifact paths must be distinct and relative")
                    seen_batch.add(entry["path"])
                    normalized_batch.append(entry)
                rows = register_batch(self.session_id, normalized_batch, step_id=step_id)
                register_metadata = getattr(self.store, "register_artifact", None)
                if callable(register_metadata):
                    persisted: List[Dict[str, Any]] = []
                    for row in rows:
                        registered = register_metadata(self.session_id, row)
                        persisted.append(dict(registered) if isinstance(registered, Mapping) else row)
                    rows = persisted
                for row in rows:
                    self._emit("artifact_registered", {"step_id": step_id, "artifact": row})
                updated = update_step(current, step_id, "succeeded", summary="registered %d artifact(s)" % len(rows), execution={"artifact_ids": [row.get("id") for row in rows]})
                self.store.update_current_plan(self.session_id, updated)
                self._emit("step_updated", {"revision": updated["revision"], "step": get_step(updated, step_id)})
                return ActionResponse(True, {"artifacts": rows, "artifact_count": len(rows)})
        for item in raw:
            if not isinstance(item, Mapping):
                raise ValueError("artifact must be an object")
            artifact = dict(item)
            if len(json.dumps(artifact, ensure_ascii=False, default=str).encode("utf-8")) > 256 * 1024:
                raise ValueError("artifact metadata is too large")
            if isinstance(artifact.get("path"), str):
                artifact["path"] = artifact["path"].replace("\\", "/")
                if artifact["path"].startswith("/"):
                    raise ValueError("artifact path must be logical and relative")
                if artifact["path"] in seen_paths:
                    raise ValueError("artifact paths must be distinct")
                seen_paths.add(artifact["path"])
            if self.artifact_store is not None and isinstance(artifact.get("path"), str):
                register_file = getattr(self.artifact_store, "register_scratch", None)
                if not callable(register_file):
                    raise ValueError("configured artifact store cannot register scratch files")
                row = register_file(
                    self.session_id,
                    artifact["path"],
                    step_id=step_id,
                    display_name=artifact.get("display_name"),
                )
                register_metadata = getattr(self.store, "register_artifact", None)
                if callable(register_metadata):
                    registered = register_metadata(self.session_id, row)
                    if isinstance(registered, Mapping):
                        row = dict(registered)
            else:
                # IDs are server-owned; a model cannot overwrite another
                # session artifact by choosing an existing identifier.
                artifact["id"] = "art-%s" % secrets.token_hex(8)
                artifact["step_id"] = step_id
                register = getattr(self.store, "register_artifact", None)
                row = register(self.session_id, artifact) if callable(register) else artifact
            rows.append(row)
            self._emit("artifact_registered", {"step_id": step_id, "artifact": row})
        updated = update_step(current, step_id, "succeeded", summary="registered %d artifact(s)" % len(rows), execution={"artifact_ids": [row.get("id") for row in rows]})
        self.store.update_current_plan(self.session_id, updated)
        self._emit("step_updated", {"revision": updated["revision"], "step": get_step(updated, step_id)})
        return ActionResponse(True, {"artifacts": rows, "artifact_count": len(rows)})

    async def cancel_run(self, run_id: str) -> bool:
        """Request cancellation of a broker-owned DAG run."""
        return await self.executor.cancel(run_id)

    async def stop(self) -> None:
        """Cancel every active execution owned by this session."""
        self._stopping = True
        run_ids = list(self.executor.known_runs(self.session_id))
        for run_id in run_ids:
            await self.executor.cancel(run_id)
        for run_id in run_ids:
            try:
                await self.executor.wait(run_id, timeout=15)
            except Exception:
                # The host can inspect the persisted cancellation/error event.
                pass

    @property
    def stop_requested(self) -> bool:
        return self._stopping

    async def approve_run(self, run_id: str, step_id: str, *, approved: bool = True, comment: str = "") -> Any:
        """Resolve an approval pause through the configured executor."""
        return await self.executor.approve(run_id, step_id, approved=approved, comment=comment)

    def fulfill_inputs(self, request_id: str, uploads: Sequence[Any]) -> Dict[str, List[str]]:
        """Record a validated input allocation and unlock the next Broker turn."""
        pending = self.store.pending_input_requests(self.session_id) if hasattr(self.store, "pending_input_requests") else []
        request = next((item for item in pending if item.get("request_id") == request_id), None)
        if request is None:
            raise InputRequestValidationError("input request is not pending")
        rows: List[Mapping[str, Any]] = list(uploads)
        if rows and all(isinstance(item, str) for item in rows):
            listed = self.store.list_uploads(self.session_id) if hasattr(self.store, "list_uploads") else []
            wanted = set(str(item) for item in rows)
            rows = [item for item in listed if isinstance(item, Mapping) and item.get("id") in wanted]
        allocation = validate_input_fulfillment(request, rows)
        self._emit("input_fulfilled", {"request_id": request_id, "allocation": allocation})
        self._awaiting_inputs = False
        return allocation

    def cancel_input_request(self, request_id: str) -> None:
        pending = self.store.pending_input_requests(self.session_id) if hasattr(self.store, "pending_input_requests") else []
        if not any(item.get("request_id") == request_id for item in pending):
            raise InputRequestValidationError("input request is not pending")
        self._emit("input_cancelled", {"request_id": request_id})
        self._awaiting_inputs = False
