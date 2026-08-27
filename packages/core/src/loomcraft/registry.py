"""The catalog: typed capabilities, composite workflows, and their runners.

This is the seam between LoomCraft and your domain.  The engine, the broker, and
the renderer all work against this registry and never import anything of yours
directly — you register contracts and callables at startup, and the agent
discovers them through ``capability_search`` / ``catalog_search``.

A **capability** is one atomic, typed unit of work: declared inputs (with
accepted extensions and cardinality), declared parameters (with types and
ranges), declared output ports, and exactly one runner.  Because the contract is
data, the same declaration produces the agent-facing schema, the server-side
validation, and the execution graph — they cannot drift apart.

A **workflow** is a fixed multi-node sub-DAG: a standard operating procedure you
want available as a single unit, still executed by the same engine.
"""

from __future__ import annotations

import json
import re
from copy import deepcopy
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .context import RunnerFn
from .errors import (
    ContractError,
    RegistryError,
    UnknownCapabilityError,
    UnknownRunnerError,
    UnknownWorkflowError,
)
from .graph import validate as validate_graph

ParameterType = Literal["integer", "number", "boolean", "string", "object", "array"]

ID_PATTERN = r"^[a-z][a-z0-9_.-]{1,159}$"
KEY_PATTERN = r"^[a-z][a-z0-9_]{0,63}$"
PORT_PATTERN = r"^[A-Za-z][A-Za-z0-9_]{0,63}$"


# ── Contract pieces ─────────────────────────────────────────────────────────


class Port(BaseModel):
    """A named output (or workflow input) slot with a declared artifact type."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(pattern=PORT_PATTERN)
    artifact_type: str = Field(min_length=1, max_length=100)
    required: bool = True
    description: str = Field(default="", max_length=500)


class Parameter(BaseModel):
    """A typed, range-checked knob the agent may set on a capability run."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    type: ParameterType
    description: str = Field(min_length=1, max_length=500)
    default: Any = None
    minimum: float | None = None
    maximum: float | None = None
    enum: tuple[Any, ...] = ()

    def json_schema(self) -> dict[str, Any]:
        schema: dict[str, Any] = {"type": self.type, "description": self.description}
        if self.default is not None:
            schema["default"] = self.default
        if self.minimum is not None:
            schema["minimum"] = self.minimum
        if self.maximum is not None:
            schema["maximum"] = self.maximum
        if self.enum:
            schema["enum"] = list(self.enum)
        return schema

    def validate_value(self, name: str, value: object) -> Any:
        """Coerce and range-check one model-supplied parameter value."""
        if self.type == "boolean":
            if not isinstance(value, bool):
                raise ContractError(f"parameter {name!r} must be boolean")
        elif self.type == "integer":
            if not isinstance(value, int) or isinstance(value, bool):
                raise ContractError(f"parameter {name!r} must be an integer")
        elif self.type == "number":
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                raise ContractError(f"parameter {name!r} must be numeric")
        elif self.type == "string":
            if not isinstance(value, str) or not value.strip():
                raise ContractError(f"parameter {name!r} must be a non-empty string")
            value = value.strip()
            if len(value) > 2000:
                raise ContractError(f"parameter {name!r} is too long")
        elif self.type == "array":
            if not isinstance(value, list):
                raise ContractError(f"parameter {name!r} must be an array")
            if len(value) > 256:
                raise ContractError(f"parameter {name!r} has too many entries")
        elif self.type == "object":
            if not isinstance(value, dict):
                raise ContractError(f"parameter {name!r} must be an object")
            encoded = json.dumps(
                value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            ).encode("utf-8")
            if len(encoded) > 64 * 1024:
                raise ContractError(f"parameter {name!r} object is too large")
        if self.minimum is not None and isinstance(value, (int, float)) and value < self.minimum:
            raise ContractError(f"parameter {name!r} is below its minimum")
        if self.maximum is not None and isinstance(value, (int, float)) and value > self.maximum:
            raise ContractError(f"parameter {name!r} exceeds its maximum")
        if self.enum and value not in self.enum:
            raise ContractError(f"parameter {name!r} is not an allowed value")
        return value


class CapabilityInput(BaseModel):
    """One declared file input slot."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    key: str = Field(pattern=KEY_PATTERN)
    name: str = Field(min_length=1, max_length=160)
    description: str = Field(min_length=1, max_length=800)
    artifact_type: str = Field(default="file", min_length=1, max_length=100)
    allowed_extensions: tuple[str, ...] = ()
    max_files: int = Field(default=1, ge=1, le=12)
    port_name: str | None = Field(default=None, pattern=PORT_PATTERN)

    @field_validator("allowed_extensions")
    @classmethod
    def _valid_extensions(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(dict.fromkeys(value.strip().lower() for value in values))
        for value in normalized:
            if not value.startswith(".") or len(value) < 2:
                raise ValueError(f"input extension {value!r} must start with a dot")
        return normalized

    @property
    def effective_port(self) -> str:
        return self.port_name or self.key

    def json_schema(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "name": self.name,
            "description": self.description,
            "type": "source_ref" if self.max_files == 1 else "source_ref[]",
            "artifact_type": self.artifact_type,
            "allowed_extensions": list(self.allowed_extensions),
            "max_files": self.max_files,
        }


# ── Capability ──────────────────────────────────────────────────────────────


class Capability(BaseModel):
    """One typed, atomic unit of work the agent may compose into a plan."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(pattern=ID_PATTERN)
    name: str = Field(min_length=1, max_length=160)
    version: str = Field(default="1", min_length=1, max_length=40)
    description: str = Field(min_length=1, max_length=2000)
    #: Key of the registered runner that executes this capability.
    runner: str = Field(pattern=ID_PATTERN)
    inputs: tuple[CapabilityInput, ...] = ()
    #: Accepted combinations of input keys. Exactly one must match at call time,
    #: which is how a capability offers "PLINK bundle *or* VCF" without letting
    #: the agent supply half of each.
    input_variants: tuple[tuple[str, ...], ...] = ()
    outputs: tuple[Port, ...] = ()
    parameters: dict[str, Parameter] = Field(default_factory=dict)
    #: Fixed runner configuration. Never model-writable.
    config: dict[str, Any] = Field(default_factory=dict)
    tags: tuple[str, ...] = ()
    #: Per-node execution policy, overridable by the host.
    timeout_seconds: float | None = Field(default=None, gt=0)
    max_attempts: int = Field(default=1, ge=1, le=10)
    retry_backoff_seconds: float = Field(default=1.0, ge=0)
    #: Require a human decision before this capability's result counts.
    requires_approval: bool = False

    @model_validator(mode="after")
    def _valid_contract(self) -> "Capability":
        keys = [item.key for item in self.inputs]
        if len(keys) != len(set(keys)):
            raise ValueError("capability input keys must be unique")
        ports = [item.effective_port for item in self.inputs]
        if len(ports) != len(set(ports)):
            raise ValueError("capability input ports must be unique")
        output_names = [item.name for item in self.outputs]
        if len(output_names) != len(set(output_names)):
            raise ValueError("capability output ports must be unique")

        known = set(keys)
        for variant in self.input_variants:
            if not variant or len(variant) != len(set(variant)):
                raise ValueError("input variants must be non-empty and unique")
            unknown = sorted(set(variant) - known)
            if unknown:
                raise ValueError(
                    "input variant references unknown keys: " + ", ".join(unknown)
                )
        for name, parameter in self.parameters.items():
            if parameter.default is not None:
                parameter.validate_value(name, parameter.default)
        return self

    @property
    def effective_variants(self) -> tuple[tuple[str, ...], ...]:
        """Declared variants, or "all inputs" when none were declared."""
        if self.input_variants:
            return self.input_variants
        return (tuple(item.key for item in self.inputs),) if self.inputs else ((),)

    # ── Call-time validation ────────────────────────────────────────────────

    def validate_inputs(self, raw: object) -> dict[str, list[str]]:
        """Validate a ``{input_key: source_ref | [source_ref]}`` mapping."""
        if raw is None:
            raw = {}
        if not isinstance(raw, dict):
            raise ContractError("capability inputs must be an object")
        by_key = {item.key: item for item in self.inputs}
        unknown = sorted(set(raw) - set(by_key))
        if unknown:
            raise ContractError("unknown capability inputs: " + ", ".join(unknown))

        normalized: dict[str, list[str]] = {}
        for key, raw_values in raw.items():
            spec = by_key[key]
            values = raw_values if isinstance(raw_values, list) else [raw_values]
            if not values or len(values) > spec.max_files:
                raise ContractError(
                    f"capability input {key!r} must contain 1..{spec.max_files} sources"
                )
            clean: list[str] = []
            for value in values:
                if not isinstance(value, str) or not value.strip():
                    raise ContractError(
                        f"capability input {key!r} must use source_ref strings"
                    )
                source_ref = value.strip()
                if source_ref in clean:
                    raise ContractError(
                        f"capability input {key!r} contains a duplicate source"
                    )
                clean.append(source_ref)
            normalized[key] = clean

        provided = set(normalized)
        variants = self.effective_variants
        matched = [variant for variant in variants if set(variant) <= provided]
        if len(matched) != 1:
            expected = " or ".join(
                "+".join(variant) or "(none)" for variant in variants
            )
            raise ContractError(
                f"capability {self.id!r} requires exactly one input variant: {expected}"
            )
        # Reject half-supplied alternatives so an agent can't blend two variants.
        for variant in variants:
            overlap = provided & set(variant)
            if overlap and not set(variant) <= provided:
                raise ContractError(
                    "partial capability input variant: " + ", ".join(sorted(overlap))
                )
        return normalized

    def validate_parameters(self, raw: object) -> dict[str, Any]:
        """Validate model-supplied parameters, layering them over defaults."""
        if raw is None:
            raw = {}
        if not isinstance(raw, dict):
            raise ContractError("capability parameters must be an object")
        unknown = sorted(set(raw) - set(self.parameters))
        if unknown:
            raise ContractError("unknown capability parameters: " + ", ".join(unknown))
        effective: dict[str, Any] = {
            key: deepcopy(parameter.default)
            for key, parameter in self.parameters.items()
            if parameter.default is not None
        }
        for key, value in raw.items():
            effective[key] = self.parameters[key].validate_value(key, value)
        return effective

    def contract(self) -> dict[str, Any]:
        """The agent-facing description returned by ``capability_search``."""
        return {
            "type": "capability",
            "id": self.id,
            "name": self.name,
            "version": self.version,
            "description": self.description,
            "tags": list(self.tags),
            "input_variants": [list(variant) for variant in self.effective_variants],
            "inputs": [item.json_schema() for item in self.inputs],
            "parameters": {
                key: value.json_schema() for key, value in self.parameters.items()
            },
            "outputs": [item.model_dump(mode="json") for item in self.outputs],
            "requires_approval": self.requires_approval,
            "execution_tool": "run_capability",
        }


# ── Workflow ────────────────────────────────────────────────────────────────


class WorkflowNode(BaseModel):
    """One node of a registered composite workflow."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(pattern=KEY_PATTERN)
    name: str = Field(min_length=1, max_length=160)
    runner: str = Field(pattern=ID_PATTERN)
    depends_on: tuple[str, ...] = ()
    description: str = Field(default="", max_length=1000)
    #: Which workflow-level input keys feed this node.
    inputs: tuple[str, ...] = ()
    outputs: tuple[Port, ...] = ()
    config: dict[str, Any] = Field(default_factory=dict)
    timeout_seconds: float | None = Field(default=None, gt=0)
    max_attempts: int = Field(default=1, ge=1, le=10)
    retry_backoff_seconds: float = Field(default=1.0, ge=0)
    requires_approval: bool = False


class Workflow(BaseModel):
    """A registered multi-node SOP, executed by the same engine as a capability."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(pattern=ID_PATTERN)
    name: str = Field(min_length=1, max_length=160)
    version: str = Field(default="1", min_length=1, max_length=40)
    description: str = Field(min_length=1, max_length=2000)
    inputs: tuple[CapabilityInput, ...] = ()
    nodes: tuple[WorkflowNode, ...] = Field(min_length=1)
    parameters: dict[str, Parameter] = Field(default_factory=dict)
    tags: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _valid_graph(self) -> "Workflow":
        ids = [node.id for node in self.nodes]
        if len(ids) != len(set(ids)):
            raise ValueError("workflow node ids must be unique")
        adjacency = {node.id: list(node.depends_on) for node in self.nodes}
        issues = validate_graph(adjacency)
        if issues:
            raise ValueError("; ".join(str(issue) for issue in issues))
        known_inputs = {item.key for item in self.inputs}
        for node in self.nodes:
            unknown = sorted(set(node.inputs) - known_inputs)
            if unknown:
                raise ValueError(
                    f"node {node.id!r} references unknown workflow inputs: "
                    + ", ".join(unknown)
                )
        return self

    def validate_inputs(self, raw: object) -> dict[str, list[str]]:
        proxy = Capability(
            id=self.id,
            name=self.name,
            version=self.version,
            description=self.description,
            runner="workflow.noop",
            inputs=self.inputs,
            input_variants=(tuple(item.key for item in self.inputs),)
            if self.inputs
            else (),
        )
        return proxy.validate_inputs(raw)

    def validate_parameters(self, raw: object) -> dict[str, Any]:
        proxy = Capability(
            id=self.id,
            name=self.name,
            version=self.version,
            description=self.description,
            runner="workflow.noop",
            parameters=self.parameters,
        )
        return proxy.validate_parameters(raw)

    def contract(self) -> dict[str, Any]:
        return {
            "type": "workflow",
            "id": self.id,
            "name": self.name,
            "version": self.version,
            "description": self.description,
            "tags": list(self.tags),
            "inputs": [item.json_schema() for item in self.inputs],
            "parameters": {
                key: value.json_schema() for key, value in self.parameters.items()
            },
            "nodes": [
                {
                    "id": node.id,
                    "name": node.name,
                    "depends_on": list(node.depends_on),
                    "description": node.description,
                }
                for node in self.nodes
            ],
            "execution_tool": "run_workflow",
        }


# ── Registry ────────────────────────────────────────────────────────────────

_WORD = re.compile(r"[a-z0-9]+")


def _tokens(text: str) -> list[str]:
    return _WORD.findall(text.lower())


class Registry:
    """The mutable catalog of capabilities, workflows, and runners.

    Build one at startup, register everything your domain offers, and hand it to
    :class:`~loomcraft.broker.ToolBroker` and :class:`~loomcraft.engine.Engine`.
    Nothing else in LoomCraft knows what your capabilities do.
    """

    def __init__(self) -> None:
        self._capabilities: dict[str, Capability] = {}
        self._workflows: dict[str, Workflow] = {}
        self._runners: dict[str, RunnerFn] = {}

    # ── Registration ────────────────────────────────────────────────────────

    def register_runner(self, key: str, fn: RunnerFn, *, replace: bool = False) -> None:
        if not re.match(ID_PATTERN, key):
            raise RegistryError(f"invalid runner key {key!r}")
        if key in self._runners and not replace:
            raise RegistryError(f"runner {key!r} is already registered")
        self._runners[key] = fn

    def runner(self, key: str) -> RunnerFn:
        fn = self._runners.get(key)
        if fn is None:
            raise UnknownRunnerError(f"no runner registered for {key!r}")
        return fn

    def has_runner(self, key: str | None) -> bool:
        return bool(key) and key in self._runners

    def register_capability(
        self, capability: Capability, *, replace: bool = False
    ) -> Capability:
        if capability.id in self._capabilities and not replace:
            raise RegistryError(f"capability {capability.id!r} is already registered")
        self._capabilities[capability.id] = capability
        return capability

    def capability(self, capability_id: str | None) -> Capability:
        found = self._capabilities.get(capability_id or "")
        if found is None:
            raise UnknownCapabilityError(f"unknown capability {capability_id!r}")
        return found

    def has_capability(self, capability_id: str | None) -> bool:
        return bool(capability_id) and capability_id in self._capabilities

    def register_workflow(self, workflow: Workflow, *, replace: bool = False) -> Workflow:
        if workflow.id in self._workflows and not replace:
            raise RegistryError(f"workflow {workflow.id!r} is already registered")
        self._workflows[workflow.id] = workflow
        return workflow

    def workflow(self, workflow_id: str | None) -> Workflow:
        found = self._workflows.get(workflow_id or "")
        if found is None:
            raise UnknownWorkflowError(f"unknown workflow {workflow_id!r}")
        return found

    def has_workflow(self, workflow_id: str | None) -> bool:
        return bool(workflow_id) and workflow_id in self._workflows

    # ── Convenience decorator ───────────────────────────────────────────────

    def capability_runner(self, capability: Capability, *, replace: bool = False):
        """Register a capability and its runner in one step.

        ::

            @registry.capability_runner(Capability(id="csv.profile", runner="csv.profile", ...))
            async def profile(ctx: NodeContext) -> NodeResult:
                ...
        """

        def decorate(fn: RunnerFn) -> RunnerFn:
            self.register_runner(capability.runner, fn, replace=replace)
            self.register_capability(capability, replace=replace)
            return fn

        return decorate

    # ── Introspection ───────────────────────────────────────────────────────

    @property
    def capabilities(self) -> dict[str, Capability]:
        return dict(self._capabilities)

    @property
    def workflows(self) -> dict[str, Workflow]:
        return dict(self._workflows)

    @property
    def runners(self) -> dict[str, RunnerFn]:
        return dict(self._runners)

    def validate(self) -> list[str]:
        """Return every dangling reference in the catalog.

        Call this at startup and fail fast: a capability pointing at a missing
        runner is a bug the agent would otherwise discover mid-plan.
        """
        problems: list[str] = []
        for capability in self._capabilities.values():
            if capability.runner not in self._runners:
                problems.append(
                    f"capability {capability.id!r} references unknown runner "
                    f"{capability.runner!r}"
                )
        for workflow in self._workflows.values():
            for node in workflow.nodes:
                if node.runner not in self._runners:
                    problems.append(
                        f"workflow {workflow.id!r} node {node.id!r} references "
                        f"unknown runner {node.runner!r}"
                    )
        return problems

    # ── Search (what the agent calls) ───────────────────────────────────────

    def search(
        self,
        query: str,
        *,
        scope: Literal["all", "capabilities", "workflows"] = "all",
        limit: int = 5,
    ) -> list[dict[str, Any]]:
        """Rank catalog entries against a natural-language query.

        Deliberately simple lexical scoring: id and tag hits outrank name hits,
        which outrank description hits.  Swap in embeddings by subclassing and
        overriding this — the broker only needs a ranked list of contracts.
        """
        terms = _tokens(query)
        pool: list[tuple[str, Any]] = []
        if scope in {"all", "capabilities"}:
            pool.extend(("capability", item) for item in self._capabilities.values())
        if scope in {"all", "workflows"}:
            pool.extend(("workflow", item) for item in self._workflows.values())

        scored: list[tuple[float, str, Any]] = []
        for kind, item in pool:
            haystack_id = _tokens(item.id)
            haystack_tags = [token for tag in item.tags for token in _tokens(tag)]
            haystack_name = _tokens(item.name)
            haystack_desc = _tokens(item.description)
            score = 0.0
            for term in terms:
                if term in haystack_id:
                    score += 5
                if term in haystack_tags:
                    score += 4
                if term in haystack_name:
                    score += 3
                if term in haystack_desc:
                    score += 1
                # Prefix match keeps "profil" finding "profile".
                if any(token.startswith(term) for token in haystack_id + haystack_name):
                    score += 0.5
            if score > 0 or not terms:
                scored.append((score, item.id, item))
        scored.sort(key=lambda row: (-row[0], row[1]))
        return [item.contract() for _, _, item in scored[: max(1, limit)]]

    def catalog_summary(self, *, limit: int = 40) -> dict[str, Any]:
        """A compact catalog overview for ``session_context``."""
        return {
            "capabilities": [
                {
                    "id": item.id,
                    "name": item.name,
                    "tags": list(item.tags),
                    "description": item.description[:200],
                }
                for item in list(self._capabilities.values())[:limit]
            ],
            "workflows": [
                {
                    "id": item.id,
                    "name": item.name,
                    "tags": list(item.tags),
                    "description": item.description[:200],
                }
                for item in list(self._workflows.values())[:limit]
            ],
            "capability_count": len(self._capabilities),
            "workflow_count": len(self._workflows),
        }


def merge_registries(*registries: Registry) -> Registry:
    """Combine catalogs — useful for plugin-style composition."""
    merged = Registry()
    for registry in registries:
        for key, fn in registry.runners.items():
            merged.register_runner(key, fn, replace=True)
        for capability in registry.capabilities.values():
            merged.register_capability(capability, replace=True)
        for workflow in registry.workflows.values():
            merged.register_workflow(workflow, replace=True)
    return merged


def source_ref_of(kind: Literal["upload", "artifact", "scratch"], identifier: str) -> str:
    """Build a canonical ``upload:``/``artifact:``/``scratch:`` reference."""
    return f"{kind}:{identifier}"


__all__ = [
    "Capability",
    "CapabilityInput",
    "Parameter",
    "ParameterType",
    "Port",
    "Registry",
    "Workflow",
    "WorkflowNode",
    "merge_registries",
    "source_ref_of",
]
