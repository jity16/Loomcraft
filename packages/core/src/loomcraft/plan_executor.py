"""Adapter that runs an AI-authored :class:`~loomcraft.plan.Plan` on ``Engine``.

The upstream engine intentionally works on a small, typed ``ExecutionGraph``.
The extracted runtime additionally exposes ``execute_plan`` for hosts that want
the whole published plan to be scheduled in one call.  This module is the thin
bridge between those contracts; it does not introduce a second scheduler.
"""

from __future__ import annotations

import asyncio
import inspect
import secrets
from dataclasses import replace
from typing import Any, Callable, Mapping, Sequence

from .context import NodeContext, NodeResult
from .engine import Engine, ExecutionGraph, ExecutionNode, Run, graph_from_workflow
from .errors import ContractError, PlanValidationError
from .plan import Plan, PlanStep, parse_plan
from .registry import Capability, CapabilitySpec, Registry, StepResult, Workflow, WorkflowSpec
from .store import Session


def _refs(value: object) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return (value,)
    if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray, str)):
        return tuple(str(item) for item in value if isinstance(item, str) and item.strip())
    return ()


def _typed_sources(
    capability: Capability,
    raw: object,
    *,
    allow_upstream: bool,
) -> dict[str, tuple[str, ...]]:
    """Validate explicit source bindings without rejecting upstream ports.

    When a step has dependencies, some required inputs may arrive as artifacts
    keyed by their output port at runtime. Explicit bindings are still checked
    for unknown keys, cardinality, duplicates, and source-ref shape.
    """
    if not allow_upstream:
        return {
            key: tuple(values)
            for key, values in capability.validate_inputs(raw).items()
        }
    if raw is None:
        raw = {}
    if not isinstance(raw, Mapping):
        raise ContractError("capability inputs must be an object")
    by_key = {item.key: item for item in capability.inputs}
    unknown = sorted(set(raw) - set(by_key))
    if unknown:
        raise ContractError("unknown capability inputs: " + ", ".join(unknown))
    result: dict[str, tuple[str, ...]] = {}
    for key, value in raw.items():
        refs = _refs(value)
        if not refs or len(refs) > by_key[key].max_files:
            raise ContractError(
                f"capability input {key!r} must contain 1..{by_key[key].max_files} sources"
            )
        if len(refs) != len(set(refs)):
            raise ContractError(f"capability input {key!r} contains a duplicate source")
        result[key] = refs
    return result


def _validate_explicit_sources(
    engine: Engine,
    specs: Sequence[Any],
    sources: Mapping[str, Sequence[str]],
) -> None:
    by_key = {item.key: item for item in specs}
    for key, refs in sources.items():
        for source_ref in refs:
            resolved = engine.session.resolve_source(source_ref)
            allowed = tuple(getattr(by_key.get(key), "allowed_extensions", ()))
            if allowed and not resolved.filename.casefold().endswith(
                tuple(extension.casefold() for extension in allowed)
            ):
                raise ContractError(
                    f"input {key!r} requires one of: " + ", ".join(allowed)
                )


class PlanNodeContext:
    """Compatibility view passed to legacy dynamic/review/answer handlers."""

    def __init__(self, node: NodeContext, step: PlanStep, plan: Plan) -> None:
        self._node = node
        self.plan = plan.to_dict()
        self.step = step.model_dump(mode="json")
        self.inputs = {
            key: [item.source_ref for item in values]
            for key, values in node.inputs.items()
        }
        self.dependencies: dict[str, Any] = dict(node.dependencies)
        self.parameters = dict(node.parameters)
        self.attempt = node.attempt
        self.run_id = node.run_id
        self.node_id = node.node_id
        self.workdir = node.workdir

    def log(self, message: str, **data: Any) -> None:
        self._node.log(message, "info")

    def progress(self, fraction: float | None = None, message: str = "", **data: Any) -> None:
        self._node.progress(float(fraction or 0.0), message)

    def input(self, key: str) -> Any:
        values = self.inputs.get(key) or []
        if not isinstance(values, (list, tuple)):
            return values
        if len(values) != 1:
            raise KeyError(f"input {key!r} is not singular")
        return values[0]

    def input_list(self, key: str) -> list[Any]:
        values = self.inputs.get(key) or []
        return list(values) if isinstance(values, (list, tuple)) else [values]

    def has_input(self, key: str) -> bool:
        return bool(self.inputs.get(key))

    def emit(self, *args: Any, **kwargs: Any) -> Any:
        return self._node.emit(*args, **kwargs)

    def emit_path(self, *args: Any, **kwargs: Any) -> Any:
        return self._node.emit_path(*args, **kwargs)

    @property
    def cancelled(self) -> bool:
        return self._node.cancelled


def _coerce_result(value: object) -> NodeResult:
    if isinstance(value, NodeResult):
        return value
    result = StepResult.from_value(value)
    detail = dict(result.metadata)
    if result.summary:
        detail["summary"] = result.summary
    if result.status == "waiting_approval":
        return NodeResult.needs_approval(result.error or "approval required", **detail)
    if result.status == "skipped":
        return NodeResult.skip(result.error or result.summary or "step skipped", **detail)
    if result.status == "failed":
        return NodeResult.fail(
            result.error or result.summary or "step failed",
            retryable=result.retryable,
            **detail,
        )
    return NodeResult.ok(output=result.output, **detail)


async def _call(handler: Any, context: Any) -> NodeResult:
    from .legacy_executor import ApprovalRequired

    try:
        value = handler(context)
        if inspect.isawaitable(value):
            value = await value
        return _coerce_result(value)
    except ApprovalRequired as exc:
        return NodeResult.needs_approval(str(exc), **dict(exc.payload))


def _step_bindings(plan: Plan, inputs: Mapping[str, Any] | None, step: PlanStep) -> Mapping[str, Any]:
    value = dict(inputs or {})
    scoped = value.get(step.id)
    if isinstance(scoped, Mapping):
        return scoped
    return value


def _binding_inputs(binding: object) -> object:
    if isinstance(binding, Mapping) and (
        "inputs" in binding or "parameters" in binding
    ):
        return binding.get("inputs", {})
    return binding


def build_plan_graph(
    plan: Plan | Mapping[str, Any],
    registry: Registry,
    engine: Engine,
    *,
    inputs: Mapping[str, Any] | None = None,
    graph_id: str | None = None,
) -> ExecutionGraph:
    """Convert a published Plan into one Engine graph.

    Typed capabilities use their registered runner directly.  Dynamic/review/
    answer steps use the optional kind handler and otherwise get safe defaults.
    Registered workflows remain executable as one node through a small wrapper;
    direct ``run_workflow`` continues to expose their full internal sub-DAG.
    """
    parsed = plan if isinstance(plan, Plan) else parse_plan(plan)
    nodes: list[ExecutionNode] = []

    for step in parsed.steps:
        binding = _step_bindings(parsed, inputs, step)
        source_map: dict[str, tuple[str, ...]] = {}
        input_ports: dict[str, str] = {}
        input_extensions: dict[str, tuple[str, ...]] = {}
        parameters: dict[str, Any] = {}
        config: dict[str, Any] = {"plan_step": step.id, "kind": step.kind}
        outputs: tuple[str, ...] = ("output",)
        max_attempts = step.retry.max_attempts
        backoff = step.retry.backoff_seconds
        backoff_multiplier = step.retry.backoff_multiplier
        max_backoff = step.retry.max_backoff_seconds
        timeout = step.timeout_seconds
        requires_approval = False

        if step.kind == "capability" or (step.kind == "review" and step.capability):
            capability = registry.capability(step.capability)
            if isinstance(capability, Capability):
                raw_inputs = _binding_inputs(binding)
                source_map = _typed_sources(
                    capability,
                    raw_inputs,
                    allow_upstream=bool(step.depends_on),
                )
                input_ports = {
                    item.effective_port: item.key for item in capability.inputs
                }
                input_extensions = {
                    item.key: item.allowed_extensions for item in capability.inputs
                }
                _validate_explicit_sources(
                    engine, capability.inputs, source_map
                )
                raw_parameters = binding.get("parameters", {}) if isinstance(binding, Mapping) else {}
                parameters = capability.validate_parameters(raw_parameters)
                runner = registry.runner(capability.runner)
                outputs = tuple(port.name for port in capability.outputs) or ("output",)
                max_attempts = step.retry.max_attempts if step.retry.max_attempts > 1 else capability.max_attempts
                backoff = step.retry.backoff_seconds or capability.retry_backoff_seconds
                plan_retry_override = (
                    step.retry.max_attempts > 1
                    or step.retry.backoff_seconds > 0
                    or step.retry.backoff_multiplier != 2.0
                    or step.retry.max_backoff_seconds != 60.0
                )
                if not plan_retry_override:
                    backoff_multiplier = 2.0
                    max_backoff = None
                timeout = step.timeout_seconds or capability.timeout_seconds
                requires_approval = capability.requires_approval
                runner_fn = runner
            elif isinstance(capability, CapabilitySpec) and (
                capability.handler is not None
                or (capability.runner and registry.has_runner(capability.runner))
            ):
                handler = capability.handler or registry.runner(capability.runner)
                canonical_runner = capability.handler is None
                raw_inputs = _binding_inputs(binding)
                validated_inputs = capability.validate_inputs(raw_inputs)
                source_map = (
                    {
                        key: _refs(value)
                        for key, value in validated_inputs.items()
                    }
                    if canonical_runner
                    else {}
                )
                raw_parameters = binding.get("parameters", {}) if isinstance(binding, Mapping) else {}
                parameters = capability.validate_parameters(raw_parameters)
                async def legacy_capability(
                    ctx: NodeContext,
                    handler: Any = handler,
                    step: PlanStep = step,
                    parsed_plan: Plan = parsed,
                    use_canonical: bool = canonical_runner,
                ) -> NodeResult:
                    adapter = PlanNodeContext(ctx, step, parsed_plan)
                    raw = _step_bindings(parsed_plan, inputs, step)
                    if isinstance(raw, Mapping):
                        raw_inputs = _binding_inputs(raw)
                        adapter.inputs = dict(raw_inputs) if isinstance(raw_inputs, Mapping) else {}
                        if isinstance(raw.get("parameters"), Mapping):
                            adapter.parameters = {
                                **adapter.parameters,
                                **dict(raw["parameters"]),
                            }
                    return await _call(handler, ctx if use_canonical else adapter)

                runner_fn = legacy_capability
                outputs = tuple(str(item.get("name", "output")) for item in capability.outputs if isinstance(item, Mapping)) or ("output",)
            else:  # pragma: no cover - guarded by publish validation
                raise PlanValidationError(f"capability step {step.id!r} has no runnable handler")
        elif step.kind == "workflow":
            workflow = registry.workflow(step.capability)
            if isinstance(workflow, WorkflowSpec) and workflow.handler is not None:
                raw_inputs = _binding_inputs(binding)
                workflow.validate_inputs(raw_inputs)
                # The legacy handler receives raw JSON values rather than
                # Engine source references.
                source_map = {}
                if isinstance(binding, Mapping) and isinstance(binding.get("parameters"), Mapping):
                    parameters = dict(binding["parameters"])
                async def legacy_workflow(
                    ctx: NodeContext,
                    handler: Any = workflow.handler,
                    step: PlanStep = step,
                    parsed_plan: Plan = parsed,
                ) -> NodeResult:
                    adapter = PlanNodeContext(ctx, step, parsed_plan)
                    raw = _step_bindings(parsed_plan, inputs, step)
                    if isinstance(raw, Mapping):
                        raw_inputs = _binding_inputs(raw)
                        adapter.inputs = dict(raw_inputs) if isinstance(raw_inputs, Mapping) else {}
                        if isinstance(raw.get("parameters"), Mapping):
                            adapter.parameters = {
                                **adapter.parameters,
                                **dict(raw["parameters"]),
                            }
                    return await _call(handler, adapter)

                runner_fn = legacy_workflow
            elif isinstance(workflow, Workflow):
                raw_inputs = _binding_inputs(binding)
                if step.depends_on:
                    # Workflow-level inputs may be supplied by an upstream Plan
                    # step just like capability ports. Validate every explicit
                    # key now and let Engine bind dependency artifacts later.
                    proxy = Capability(
                        id=workflow.id,
                        name=workflow.name,
                        description=workflow.description,
                        runner="workflow.noop",
                        inputs=workflow.inputs,
                    )
                    source_map = _typed_sources(proxy, raw_inputs, allow_upstream=True)
                else:
                    source_map = {
                        key: tuple(values)
                        for key, values in workflow.validate_inputs(raw_inputs).items()
                    }
                raw_parameters = binding.get("parameters", {}) if isinstance(binding, Mapping) else {}
                parameters = workflow.validate_parameters(raw_parameters)
                input_extensions = {
                    item.key: item.allowed_extensions for item in workflow.inputs
                }
                _validate_explicit_sources(engine, workflow.inputs, source_map)

                async def run_workflow(
                    ctx: NodeContext,
                    workflow: Workflow = workflow,
                    parent_engine: Engine = engine,
                ) -> NodeResult:
                    nested_sources = {
                        key: tuple(item.source_ref for item in values)
                        for key, values in ctx.inputs.items()
                    }
                    graph = graph_from_workflow(
                        workflow,
                        sources=nested_sources,
                        parameters=ctx.parameters,
                        graph_id=f"{ctx.run_id}-{ctx.node_id}-workflow",
                    )
                    if ctx.config.get("approved"):
                        graph = replace(
                            graph,
                            nodes=tuple(
                                replace(
                                    child,
                                    requires_approval=False,
                                    config={**child.config, "approved": True},
                                )
                                for child in graph.nodes
                            ),
                        )
                    nested_engine = Engine(
                        registry,
                        parent_engine.session,
                        max_parallel=parent_engine.max_parallel,
                        emit=parent_engine._emit,  # type: ignore[attr-defined]
                        stream_logs=parent_engine.stream_logs,
                    )
                    nested = nested_engine.submit(graph)
                    nested.plan_step_id = ctx.node_id
                    try:
                        while nested.status not in {
                            "succeeded",
                            "failed",
                            "cancelled",
                            "paused_approval",
                        }:
                            ctx.raise_if_cancelled()
                            await asyncio.sleep(0.01)
                    except asyncio.CancelledError:
                        await nested.cancel()
                        raise
                    if nested.status == "paused_approval":
                        await nested.cancel()
                        return NodeResult.fail(
                            "workflow reached a dynamic approval boundary; "
                            "execute it with run_workflow for per-node approval"
                        )
                    if nested.status != "succeeded":
                        errors = "; ".join(
                            f"{item['node_id']}: {item['error']}"
                            for item in nested.failed_nodes
                        )
                        return NodeResult.fail(errors or "workflow execution failed")
                    for artifact in nested.artifacts:
                        ctx.adopt_artifact(artifact)
                    return NodeResult.ok(
                        workflow_run_id=nested.id,
                        nodes={
                            node_id: state.status
                            for node_id, state in nested.nodes.items()
                        },
                    )

                runner_fn = run_workflow
                max_attempts = step.retry.max_attempts
                requires_approval = any(
                    child.requires_approval for child in workflow.nodes
                )
            else:  # pragma: no cover
                raise PlanValidationError(f"workflow step {step.id!r} has no runnable handler")
        else:
            handler = registry.handler_for(step.kind)
            if handler is None and step.kind == "answer":
                async def answer(ctx: NodeContext, title: str = step.title, description: str = step.description) -> NodeResult:
                    return NodeResult.ok(message=description or title)

                runner_fn = answer
            elif handler is None and step.kind == "review":
                async def review(
                    ctx: NodeContext,
                    title: str = step.title,
                    step_id: str = step.id,
                ) -> NodeResult:
                    return NodeResult.ok(
                        message=f"approved {title}", step_id=step_id
                    )

                runner_fn = review
                requires_approval = True
            elif handler is None:
                raise PlanValidationError(f"no handler registered for {step.kind} step {step.id!r}")
            else:
                async def dynamic(ctx: NodeContext, handler: Any = handler, step: PlanStep = step, parsed: Plan = parsed) -> NodeResult:
                    return await _call(handler, PlanNodeContext(ctx, step, parsed))

                runner_fn = dynamic
            source_map = {
                key: _refs(value)
                for key, value in (binding.items() if isinstance(binding, Mapping) else [])
                if key not in {"parameters"}
            }
            if isinstance(binding, Mapping) and isinstance(binding.get("parameters"), Mapping):
                parameters = dict(binding["parameters"])

        nodes.append(
            ExecutionNode(
                id=step.id,
                name=step.title,
                runner=step.capability or f"loomcraft.plan.{step.kind}",
                runner_fn=runner_fn,
                depends_on=tuple(step.depends_on),
                inputs=source_map,
                input_ports=input_ports,
                input_extensions=input_extensions,
                parameters=parameters,
                config=config,
                outputs=outputs,
                timeout_seconds=timeout,
                max_attempts=max(1, max_attempts),
                retry_backoff_seconds=max(0.0, backoff),
                retry_backoff_multiplier=max(1.0, backoff_multiplier),
                retry_max_backoff_seconds=max(0.0, max_backoff) if max_backoff is not None else None,
                requires_approval=requires_approval,
                on_failure=step.on_failure,
            )
        )

    return ExecutionGraph(
        id=graph_id or f"plan-{secrets.token_hex(8)}",
        name=parsed.goal,
        nodes=tuple(nodes),
        kind="plan",  # type: ignore[arg-type]
        source_id=f"revision:{parsed.revision}",
    )


class PlanExecutor:
    """Run a complete Plan through the canonical Engine driver."""

    def __init__(self, registry: Registry, session: Session, *, engine: Engine | None = None) -> None:
        self.registry = registry
        self.session = session
        self.engine = engine or Engine(registry, session)

    async def execute(
        self,
        plan: Plan | Mapping[str, Any],
        *,
        inputs: Mapping[str, Any] | None = None,
        run_id: str | None = None,
        timeout_seconds: float | None = None,
        on_submitted: Callable[[Run], None] | None = None,
    ) -> Any:
        parsed = plan if isinstance(plan, Plan) else parse_plan(plan)
        if timeout_seconds is not None and float(timeout_seconds) <= 0:
            raise ValueError("timeout_seconds must be positive")
        graph = build_plan_graph(parsed, self.registry, self.engine, inputs=inputs)
        run = self.engine.submit(graph, run_id=run_id)
        if on_submitted is not None:
            on_submitted(run)
        # A paused approval run deliberately does not set Run._done. Return its
        # current handle instead of spinning forever; callers can approve and
        # await the same handle through Engine/Run.
        deadline = None if timeout_seconds is None else asyncio.get_running_loop().time() + float(timeout_seconds)
        while run.status not in {"succeeded", "failed", "cancelled", "paused_approval"}:
            if deadline is not None and asyncio.get_running_loop().time() >= deadline:
                run.error = "plan execution timed out"
                emit = getattr(self.engine, "_emit", None)
                if callable(emit):
                    emit("run_timeout", {"run_id": run.id, "timeout_seconds": timeout_seconds})
                await run.cancel()
                break
            await asyncio.sleep(0.01)
        return run


__all__ = ["PlanExecutor", "PlanNodeContext", "build_plan_graph"]
