"""Run a whole published :class:`~loomcraft.plan.Plan` as one execution graph.

``run_capability`` executes one step at a time, with the agent deciding what to
dispatch next. That is the right shape when the model is reasoning between
steps. It is the wrong shape when the plan is already settled and the point is
to *get through it*: a fifteen-step investigation whose middle eight steps are
independent should not be serialised behind the model's turn loop.

This module compiles a plan into a single :class:`~loomcraft.engine.ExecutionGraph`
and hands it to the same engine. Independent branches run at once, each step
carries its own retry budget and timeout, approval gates park the run, and a
failed branch either stops its dependents or — with ``on_failure="continue"`` —
lets them proceed.

It is deliberately an *adapter*, not a second scheduler. Every guarantee comes
from :class:`~loomcraft.engine.Engine`; this file only decides what the nodes
are.
"""

from __future__ import annotations

import asyncio
import secrets
from dataclasses import replace
from typing import Any, Callable, Mapping, Sequence

from .context import NodeContext, NodeResult
from .engine import Engine, ExecutionGraph, ExecutionNode, Run, graph_from_workflow
from .errors import ContractError, PlanValidationError
from .plan import Plan, PlanStep, RetryPolicy, parse_plan
from .registry import Capability, Registry, Workflow
from .store import Session


# ── Policy resolution ───────────────────────────────────────────────────────


def resolve_retry(
    policy: RetryPolicy,
    *,
    attempts: int,
    backoff: float,
    multiplier: float = 2.0,
    max_backoff: float | None = None,
) -> tuple[int, float, float, float | None]:
    """Combine a plan step's retry policy with the catalog's defaults.

    A plan that says nothing about retry inherits whatever the capability
    declared — publishing a plan must never quietly downgrade a capability that
    asked for three attempts. A plan that *does* specify retry is an explicit
    operator decision and wins outright.
    """
    if policy.is_default:
        return attempts, backoff, multiplier, max_backoff
    return (
        policy.max_attempts,
        policy.backoff_seconds,
        policy.backoff_multiplier,
        policy.max_backoff_seconds,
    )


# ── Input binding ───────────────────────────────────────────────────────────


def _refs(value: object) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return (value,)
    if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray, str)):
        return tuple(
            str(item) for item in value if isinstance(item, str) and item.strip()
        )
    return ()


def _step_binding(
    inputs: Mapping[str, Any] | None, step: PlanStep, step_ids: frozenset[str]
) -> Mapping[str, Any]:
    """Pick this step's slice out of the caller-supplied input map.

    Accepts both ``{"step_id": {...}}`` and a flat mapping, so a single-step
    plan does not need the extra nesting. The two are told apart by whether the
    top-level keys name steps — otherwise a step-scoped map would be handed
    whole to every step that had no entry in it, and read as unknown inputs.
    """
    value = dict(inputs or {})
    if not value:
        return {}
    scoped = value.get(step.id)
    if isinstance(scoped, Mapping):
        return scoped
    if all(key in step_ids for key in value):
        return {}
    return value


def _split_binding(binding: object) -> tuple[object, dict[str, Any]]:
    """Separate ``{"inputs": ..., "parameters": ...}`` from a bare input map."""
    if isinstance(binding, Mapping) and ("inputs" in binding or "parameters" in binding):
        raw_parameters = binding.get("parameters")
        return (
            binding.get("inputs", {}),
            dict(raw_parameters) if isinstance(raw_parameters, Mapping) else {},
        )
    return binding, {}


def _typed_sources(
    capability: Capability | Workflow,
    raw: object,
    *,
    allow_upstream: bool,
) -> dict[str, tuple[str, ...]]:
    """Validate explicit source bindings without rejecting upstream ports.

    A step with dependencies may legitimately leave a required input unbound:
    it arrives at runtime as an upstream artifact. Explicit bindings are still
    checked for unknown keys, cardinality and duplicates — we relax
    *completeness*, not correctness.
    """
    if not allow_upstream:
        return {
            key: tuple(value)
            for key, value in capability.validate_inputs(raw).items()
        }
    if raw is None:
        raw = {}
    if not isinstance(raw, Mapping):
        raise ContractError("inputs must be an object")
    by_key = {item.key: item for item in capability.inputs}
    unknown = sorted(set(raw) - set(by_key))
    if unknown:
        raise ContractError("unknown inputs: " + ", ".join(unknown))
    result: dict[str, tuple[str, ...]] = {}
    for key, value in raw.items():
        refs = _refs(value)
        if not refs or len(refs) > by_key[key].max_files:
            raise ContractError(
                f"input {key!r} must contain 1..{by_key[key].max_files} sources"
            )
        if len(refs) != len(set(refs)):
            raise ContractError(f"input {key!r} contains a duplicate source")
        result[key] = refs
    return result


def _check_explicit_sources(
    engine: Engine,
    specs: Sequence[Any],
    sources: Mapping[str, Sequence[str]],
) -> None:
    """Resolve every explicitly bound source now, so a typo fails at build time.

    Discovering an unknown upload halfway through a fifteen-step run, after the
    expensive steps already burned, is strictly worse than refusing to start.
    """
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


# ── Default handlers for agent-owned step kinds ─────────────────────────────


def _answer_node(step: PlanStep) -> Callable[[NodeContext], Any]:
    async def answer(ctx: NodeContext) -> NodeResult:
        return NodeResult.ok(message=step.description or step.title)

    return answer


def _review_node(step: PlanStep) -> Callable[[NodeContext], Any]:
    async def review(ctx: NodeContext) -> NodeResult:
        # Reached only after the approval gate cleared, so the decision has
        # already been made by a person.
        return NodeResult.ok(message=f"approved: {step.title}", step_id=step.id)

    return review


def _handler_node(
    handler: Callable[[NodeContext], Any], step: PlanStep
) -> Callable[[NodeContext], Any]:
    async def run(ctx: NodeContext) -> NodeResult:
        value = handler(ctx)
        if hasattr(value, "__await__"):
            value = await value
        if not isinstance(value, NodeResult):
            raise ContractError(
                f"step handler for {step.kind!r} must return a NodeResult"
            )
        return value

    return run


def _workflow_node(
    workflow: Workflow, registry: Registry, engine: Engine, step: PlanStep
) -> Callable[[NodeContext], Any]:
    """Run a registered workflow as one node, preserving its internal sub-DAG.

    The alternative — inlining the workflow's nodes into the plan graph — would
    let a plan step reach inside an SOP and depend on its internals, which is
    exactly what registering it as a unit was meant to prevent.
    """

    async def run_workflow(ctx: NodeContext) -> NodeResult:
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
            # The outer gate already collected a decision for this step; do not
            # ask again for each inner node.
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
            engine.session,
            max_parallel=engine.max_parallel,
            emit=engine._emit,
            stream_logs=engine.stream_logs,
        )
        nested = nested_engine.submit(graph)
        nested.plan_step_id = ctx.node_id
        try:
            await nested.settled()
        except asyncio.CancelledError:
            await nested.cancel()
            raise
        if nested.status == "paused_approval":
            # A gate inside a workflow needs per-node routing, which only the
            # direct run_workflow path exposes. Fail honestly rather than
            # silently approving on the operator's behalf.
            await nested.cancel()
            return NodeResult.fail(
                f"workflow {workflow.id!r} reached an approval boundary; run it "
                "with run_workflow so each gate can be resolved individually"
            )
        if nested.status != "succeeded":
            errors = "; ".join(
                f"{item['node_id']}: {item['error']}" for item in nested.failed_nodes
            )
            return NodeResult.fail(errors or "workflow execution failed")
        for artifact in nested.artifacts:
            ctx.adopt_artifact(artifact)
        return NodeResult.ok(
            workflow_run_id=nested.id,
            nodes={node_id: state.status for node_id, state in nested.nodes.items()},
        )

    return run_workflow


# ── Graph construction ──────────────────────────────────────────────────────


def build_plan_graph(
    plan: Plan | Mapping[str, Any],
    registry: Registry,
    engine: Engine,
    *,
    inputs: Mapping[str, Any] | None = None,
    graph_id: str | None = None,
) -> ExecutionGraph:
    """Compile a published plan into one runnable graph.

    Capability steps use their registered runner. Workflow steps run as a single
    node wrapping a nested run. ``dynamic``/``review``/``answer`` steps use the
    host's registered step handler, or a safe default that produces no artifacts
    and claims nothing.
    """
    parsed = plan if isinstance(plan, Plan) else parse_plan(plan)
    step_ids = frozenset(step.id for step in parsed.steps)
    nodes: list[ExecutionNode] = []

    for step in parsed.steps:
        raw_binding = _step_binding(inputs, step, step_ids)
        raw_inputs, parameters = _split_binding(raw_binding)
        sources: dict[str, tuple[str, ...]] = {}
        input_ports: dict[str, str] = {}
        input_extensions: dict[str, tuple[str, ...]] = {}
        config: dict[str, Any] = {"plan_step": step.id, "kind": step.kind}
        outputs: tuple[str, ...] = ("output",)
        requires_approval = False
        attempts, backoff, multiplier, max_backoff = resolve_retry(
            step.retry, attempts=1, backoff=0.0
        )
        timeout = step.timeout_seconds

        if step.kind == "capability" or (step.kind == "review" and step.capability):
            capability = registry.capability(step.capability)
            if not isinstance(capability, Capability):  # pragma: no cover - guard
                raise PlanValidationError(
                    f"capability step {step.id!r} has no typed contract"
                )
            sources = _typed_sources(
                capability, raw_inputs, allow_upstream=bool(step.depends_on)
            )
            _check_explicit_sources(engine, capability.inputs, sources)
            input_ports = {item.effective_port: item.key for item in capability.inputs}
            input_extensions = {
                item.key: item.allowed_extensions for item in capability.inputs
            }
            parameters = capability.validate_parameters(parameters)
            outputs = tuple(port.name for port in capability.outputs) or ("output",)
            config = {**dict(capability.config), **config}
            runner_fn = registry.runner(capability.runner)
            requires_approval = capability.requires_approval
            attempts, backoff, multiplier, max_backoff = resolve_retry(
                step.retry,
                attempts=capability.max_attempts,
                backoff=capability.retry_backoff_seconds,
            )
            timeout = step.timeout_seconds or capability.timeout_seconds

        elif step.kind == "workflow":
            workflow = registry.workflow(step.capability)
            if not isinstance(workflow, Workflow):  # pragma: no cover - guard
                raise PlanValidationError(
                    f"workflow step {step.id!r} has no typed contract"
                )
            sources = _typed_sources(
                workflow, raw_inputs, allow_upstream=bool(step.depends_on)
            )
            _check_explicit_sources(engine, workflow.inputs, sources)
            input_extensions = {
                item.key: item.allowed_extensions for item in workflow.inputs
            }
            parameters = workflow.validate_parameters(parameters)
            runner_fn = _workflow_node(workflow, registry, engine, step)
            requires_approval = any(child.requires_approval for child in workflow.nodes)

        else:
            handler = registry.step_handler(step.kind)
            if handler is not None:
                runner_fn = _handler_node(handler, step)
            elif step.kind == "answer":
                runner_fn = _answer_node(step)
            elif step.kind == "review":
                # Nothing verifies itself. With no host handler, a review is a
                # human decision point, not an automatic pass.
                runner_fn = _review_node(step)
                requires_approval = True
            else:
                raise PlanValidationError(
                    f"step {step.id!r} is {step.kind!r} and needs a registered step "
                    "handler; call registry.register_step_handler("
                    f"{step.kind!r}, ...) or execute it with update_step",
                    public_message=(
                        f"no handler registered for {step.kind} step {step.id!r}"
                    ),
                )
            sources = {
                key: _refs(value)
                for key, value in (
                    raw_inputs.items() if isinstance(raw_inputs, Mapping) else ()
                )
            }

        nodes.append(
            ExecutionNode(
                id=step.id,
                name=step.title,
                runner=step.capability or f"loomcraft.plan.{step.kind}",
                runner_fn=runner_fn,
                depends_on=tuple(step.depends_on),
                inputs=sources,
                input_ports=input_ports,
                input_extensions=input_extensions,
                parameters=parameters,
                config=config,
                outputs=outputs,
                timeout_seconds=timeout,
                max_attempts=max(1, attempts),
                retry_backoff_seconds=max(0.0, backoff),
                retry_backoff_multiplier=max(1.0, multiplier),
                retry_max_backoff_seconds=(
                    max(0.0, max_backoff) if max_backoff is not None else None
                ),
                requires_approval=requires_approval,
                on_failure=step.on_failure,
            )
        )

    return ExecutionGraph(
        id=graph_id or f"plan-{secrets.token_hex(8)}",
        name=parsed.goal,
        nodes=tuple(nodes),
        kind="plan",
        source_id=f"revision:{parsed.revision}",
    )


class PlanExecutor:
    """Schedules a complete plan on one :class:`~loomcraft.engine.Engine`."""

    def __init__(
        self, registry: Registry, session: Session, *, engine: Engine | None = None
    ) -> None:
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
    ) -> Run:
        """Build, submit and await the plan graph.

        Returns when the run is terminal or parked at an approval gate.
        ``timeout_seconds`` bounds the whole plan, not a single step.
        """
        parsed = plan if isinstance(plan, Plan) else parse_plan(plan)
        if timeout_seconds is not None and float(timeout_seconds) <= 0:
            raise ValueError("timeout_seconds must be positive")
        graph = build_plan_graph(parsed, self.registry, self.engine, inputs=inputs)
        run = self.engine.submit(graph, run_id=run_id)
        if on_submitted is not None:
            on_submitted(run)
        if timeout_seconds is None:
            await run.settled()
            return run
        try:
            await asyncio.wait_for(run.settled(), timeout=float(timeout_seconds))
        except asyncio.TimeoutError:
            run.error = "plan execution timed out"
            self.engine._emit(
                "run_timeout",
                {"execution_id": run.id, "timeout_seconds": float(timeout_seconds)},
            )
            await run.cancel()
        return run


__all__ = ["PlanExecutor", "build_plan_graph", "resolve_retry"]
