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

import hashlib
import inspect
import json
import math
import re
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any, Callable, Literal, Mapping, Sequence

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


# ── Extracted-runtime compatibility contracts ──────────────────────────────


@dataclass
class StepResult:
    """Mapping-friendly result accepted by both execution adapters."""

    output: Any = None
    summary: str | None = None
    artifacts: list[dict[str, Any]] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    status: str = "succeeded"
    error: str | None = None
    retryable: bool = False

    @classmethod
    def from_value(cls, value: Any) -> "StepResult":
        if isinstance(value, cls):
            return value
        if value is None:
            return cls()
        if isinstance(value, Mapping):
            control_keys = {"output", "summary", "artifacts", "metadata", "status", "error", "retryable"}
            is_control = bool(control_keys.intersection(value)) or value.get("status") in {
                "succeeded",
                "failed",
                "skipped",
            }
            if is_control:
                artifacts = value.get("artifacts") or []
                metadata = value.get("metadata") or {}
                if not isinstance(artifacts, list) or not all(isinstance(item, Mapping) for item in artifacts):
                    raise ValueError("step result artifacts must be a list of objects")
                if not isinstance(metadata, Mapping):
                    raise ValueError("step result metadata must be an object")
                status = str(value.get("status", "succeeded"))
                if status not in {
                    "succeeded",
                    "failed",
                    "skipped",
                    "waiting_approval",
                }:
                    raise ValueError("step result status is invalid")
                output = value.get("output")
                if "output" not in value:
                    output = {key: item for key, item in value.items() if key not in control_keys}
                return cls(
                    output=output,
                    summary=str(value["summary"]) if value.get("summary") is not None else None,
                    artifacts=[dict(item) for item in artifacts],
                    metadata=dict(metadata),
                    status=status,
                    error=str(value["error"])[:2000] if value.get("error") is not None else None,
                    retryable=bool(value.get("retryable", False)),
                )
        return cls(output=value)

    @staticmethod
    def ok(output: Any = None, **metadata: Any) -> "StepResult":
        return StepResult(output=output, metadata=metadata)

    @staticmethod
    def fail(message: str, *, retryable: bool = False, **metadata: Any) -> "StepResult":
        return StepResult(status="failed", error=str(message)[:2000], retryable=retryable, metadata=metadata)

    @staticmethod
    def retry(message: str, **metadata: Any) -> "StepResult":
        return StepResult.fail(message, retryable=True, **metadata)


@dataclass
class CapabilitySpec:
    """Legacy JSON-Schema capability adapted to the typed registry."""

    id: str
    name: str
    description: str = ""
    handler: Callable[[Any], Any] | None = None
    version: str = "1.0.0"
    input_schema: dict[str, Any] = field(default_factory=dict)
    parameter_schema: dict[str, Any] = field(default_factory=dict)
    outputs: list[dict[str, Any]] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    runner: str | None = None

    def to_catalog(self) -> dict[str, Any]:
        reserved = {
            "type",
            "id",
            "name",
            "version",
            "execution_tool",
            "inputs",
            "parameters",
            "outputs",
        }
        return {
            "type": "capability",
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "version": self.version,
            "execution_tool": "run_capability",
            "inputs": deepcopy(self.input_schema),
            "parameters": deepcopy(self.parameter_schema),
            "outputs": deepcopy(self.outputs),
            **{
                key: deepcopy(value)
                for key, value in self.metadata.items()
                if key not in {"path", "command", "argv"} | reserved
            },
        }

    def validate_inputs(self, value: Any) -> dict[str, Any]:
        from .schema import validate as validate_schema

        if value is None:
            value = {}
        if not isinstance(value, Mapping):
            raise ContractError("capability inputs must be an object")
        validate_schema(value, self.input_schema, "inputs")
        return dict(value)

    def validate_parameters(self, value: Any) -> dict[str, Any]:
        from .schema import validate as validate_schema

        if value is None:
            value = {}
        if not isinstance(value, Mapping):
            raise ContractError("capability parameters must be an object")
        validate_schema(value, self.parameter_schema, "parameters")
        return dict(value)


@dataclass
class WorkflowSpec:
    """Legacy single-handler workflow contract."""

    id: str
    name: str
    description: str = ""
    handler: Callable[[Any], Any] | None = None
    version: str = "1.0.0"
    input_schema: dict[str, Any] = field(default_factory=dict)
    outputs: list[dict[str, Any]] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_catalog(self) -> dict[str, Any]:
        reserved = {
            "type",
            "id",
            "name",
            "version",
            "execution_tool",
            "inputs",
            "outputs",
        }
        return {
            "type": "workflow",
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "version": self.version,
            "execution_tool": "run_workflow",
            "inputs": deepcopy(self.input_schema),
            "outputs": deepcopy(self.outputs),
            **{
                key: deepcopy(value)
                for key, value in self.metadata.items()
                if key not in {"path", "command", "argv"} | reserved
            },
        }

    def validate_inputs(self, value: Any) -> dict[str, Any]:
        from .schema import validate as validate_schema

        if value is None:
            value = {}
        if not isinstance(value, Mapping):
            raise ContractError("workflow inputs must be an object")
        validate_schema(value, self.input_schema, "inputs")
        return dict(value)


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
            if not math.isfinite(float(value)):
                raise ContractError(f"parameter {name!r} must be finite")
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
            if (
                not value.startswith(".")
                or not 2 <= len(value) <= 32
                or any(
                    not (character.isalnum() or character in {".", "_", "-", "+"})
                    for character in value[1:]
                )
            ):
                raise ValueError("input extension is invalid")
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
    #: Require a human decision before the runner is invoked.
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
            "timeout_seconds": self.timeout_seconds,
            "max_attempts": self.max_attempts,
            "retry_backoff_seconds": self.retry_backoff_seconds,
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
        self._legacy_capabilities: dict[str, CapabilitySpec] = {}
        self._legacy_workflows: dict[str, WorkflowSpec] = {}
        self._handlers: dict[str, Callable[[Any], Any]] = {}
        self._catalog_entries: dict[str, list[dict[str, Any]]] = {
            "operations": [],
            "tools": [],
            "skills": [],
            "runners": [],
        }

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
        self,
        capability: Capability | CapabilitySpec | None = None,
        *,
        id: str | None = None,
        name: str | None = None,
        description: str = "",
        handler: Callable[[Any], Any] | None = None,
        version: str = "1.0.0",
        input_schema: Mapping[str, Any] | None = None,
        parameter_schema: Mapping[str, Any] | None = None,
        outputs: Sequence[Mapping[str, Any]] | None = None,
        metadata: Mapping[str, Any] | None = None,
        runner: str | None = None,
        replace: bool = False,
    ) -> Capability | CapabilitySpec:
        """Register typed contracts or the extracted JSON-Schema shorthand.

        The typed :class:`Capability` path remains the canonical API.  The
        keyword form is intentionally retained as a compatibility adapter so
        applications can migrate incrementally without a second broker.
        """
        if capability is None:
            legacy = CapabilitySpec(
                id=id or "",
                name=name or id or "",
                description=description,
                handler=handler,
                version=version,
                input_schema=deepcopy(dict(input_schema or {})),
                parameter_schema=deepcopy(dict(parameter_schema or {})),
                outputs=[deepcopy(dict(item)) for item in (outputs or ())],
                metadata=deepcopy(dict(metadata or {})),
                runner=runner,
            )
            self._check_legacy_identity(legacy.id, legacy.name, "capability")
            if (legacy.id in self._legacy_capabilities or legacy.id in self._capabilities) and not replace:
                raise RegistryError(f"capability {legacy.id!r} is already registered")
            if replace:
                self._capabilities.pop(legacy.id, None)
            self._legacy_capabilities[legacy.id] = legacy
            return legacy
        if isinstance(capability, CapabilitySpec):
            self._check_legacy_identity(capability.id, capability.name, "capability")
            if (capability.id in self._legacy_capabilities or capability.id in self._capabilities) and not replace:
                raise RegistryError(f"capability {capability.id!r} is already registered")
            if replace:
                self._capabilities.pop(capability.id, None)
            self._legacy_capabilities[capability.id] = capability
            return capability
        if (capability.id in self._capabilities or capability.id in self._legacy_capabilities) and not replace:
            raise RegistryError(f"capability {capability.id!r} is already registered")
        if replace:
            self._legacy_capabilities.pop(capability.id, None)
        self._capabilities[capability.id] = capability
        return capability

    def capability(self, capability_id: str | None) -> Capability | CapabilitySpec:
        identifier = capability_id or ""
        found = self._capabilities.get(identifier) or self._legacy_capabilities.get(identifier)
        if found is None:
            raise UnknownCapabilityError(f"unknown capability {capability_id!r}")
        return found

    def has_capability(self, capability_id: str | None) -> bool:
        return bool(capability_id) and (
            capability_id in self._capabilities or capability_id in self._legacy_capabilities
        )

    def register_workflow(
        self,
        workflow: Workflow | WorkflowSpec | None = None,
        *,
        id: str | None = None,
        name: str | None = None,
        description: str = "",
        handler: Callable[[Any], Any] | None = None,
        version: str = "1.0.0",
        input_schema: Mapping[str, Any] | None = None,
        outputs: Sequence[Mapping[str, Any]] | None = None,
        metadata: Mapping[str, Any] | None = None,
        replace: bool = False,
    ) -> Workflow | WorkflowSpec:
        if workflow is None:
            legacy = WorkflowSpec(
                id=id or "",
                name=name or id or "",
                description=description,
                handler=handler,
                version=version,
                input_schema=deepcopy(dict(input_schema or {})),
                outputs=[deepcopy(dict(item)) for item in (outputs or ())],
                metadata=deepcopy(dict(metadata or {})),
            )
            self._check_legacy_identity(legacy.id, legacy.name, "workflow")
            if (legacy.id in self._legacy_workflows or legacy.id in self._workflows) and not replace:
                raise RegistryError(f"workflow {legacy.id!r} is already registered")
            if replace:
                self._workflows.pop(legacy.id, None)
            self._legacy_workflows[legacy.id] = legacy
            return legacy
        if isinstance(workflow, WorkflowSpec):
            self._check_legacy_identity(workflow.id, workflow.name, "workflow")
            if (workflow.id in self._legacy_workflows or workflow.id in self._workflows) and not replace:
                raise RegistryError(f"workflow {workflow.id!r} is already registered")
            if replace:
                self._workflows.pop(workflow.id, None)
            self._legacy_workflows[workflow.id] = workflow
            return workflow
        if (workflow.id in self._workflows or workflow.id in self._legacy_workflows) and not replace:
            raise RegistryError(f"workflow {workflow.id!r} is already registered")
        if replace:
            self._legacy_workflows.pop(workflow.id, None)
        self._workflows[workflow.id] = workflow
        return workflow

    def workflow(self, workflow_id: str | None) -> Workflow | WorkflowSpec:
        identifier = workflow_id or ""
        found = self._workflows.get(identifier) or self._legacy_workflows.get(identifier)
        if found is None:
            raise UnknownWorkflowError(f"unknown workflow {workflow_id!r}")
        return found

    def has_workflow(self, workflow_id: str | None) -> bool:
        return bool(workflow_id) and (
            workflow_id in self._workflows or workflow_id in self._legacy_workflows
        )

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

    def register_handler(self, kind: str, handler: Callable[[Any], Any]) -> None:
        """Register a host handler for dynamic, review, or answer steps."""
        if kind not in {"dynamic", "review", "answer"}:
            raise RegistryError(
                "custom handlers are supported for answer, dynamic, and review steps"
            )
        if not callable(handler):
            raise TypeError("handler must be callable")
        self._handlers[kind] = handler

    def handler_for(self, kind: str) -> Callable[[Any], Any] | None:
        return self._handlers.get(kind)

    def register_catalog_entry(self, scope: str, entry: Mapping[str, Any]) -> dict[str, Any]:
        if scope not in self._catalog_entries:
            raise RegistryError(
                "catalog scope must be operations, tools, skills, or runners"
            )
        if not isinstance(entry, Mapping) or not isinstance(entry.get("id"), str) or not str(entry.get("id")).strip():
            raise RegistryError("catalog entry must contain a non-empty id")
        value = {
            key: deepcopy(item)
            for key, item in entry.items()
            if key not in {"path", "command", "argv", "env"}
        }
        value.setdefault("scope", scope)
        if any(item.get("id") == value["id"] for item in self._catalog_entries[scope]):
            raise RegistryError(f"catalog entry {value['id']!r} is already registered")
        self._catalog_entries[scope].append(value)
        return deepcopy(value)

    # ── Introspection ───────────────────────────────────────────────────────

    @property
    def capabilities(self) -> dict[str, Any]:
        return {**self._legacy_capabilities, **self._capabilities}

    @property
    def workflows(self) -> dict[str, Any]:
        return {**self._legacy_workflows, **self._workflows}

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
        for capability in self._legacy_capabilities.values():
            if capability.runner and capability.runner not in self._runners:
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
        scope: Literal["all", "capabilities", "workflows", "operations", "tools", "skills", "runners"] = "all",
        limit: int = 5,
    ) -> list[dict[str, Any]]:
        """Rank catalog entries against a natural-language query.

        Deliberately simple lexical scoring: id and tag hits outrank name hits,
        which outrank description hits.  Swap in embeddings by subclassing and
        overriding this — the broker only needs a ranked list of contracts.
        """
        terms = _tokens(query)
        if scope not in {"all", "capabilities", "workflows", "operations", "tools", "skills", "runners"}:
            raise RegistryError("catalog scope is invalid")
        pool: list[tuple[str, Any]] = []
        if scope in {"all", "capabilities"}:
            pool.extend(("capability", item) for item in self._capabilities.values())
            pool.extend(("capability", item) for item in self._legacy_capabilities.values())
        if scope in {"all", "workflows"}:
            pool.extend(("workflow", item) for item in self._workflows.values())
            pool.extend(("workflow", item) for item in self._legacy_workflows.values())
        if scope == "all":
            pool.extend((scope_name, item) for scope_name, rows in self._catalog_entries.items() for item in rows)
        elif scope in self._catalog_entries:
            pool.extend((scope, item) for item in self._catalog_entries[scope])

        scored: list[tuple[float, str, Any]] = []
        for kind, item in pool:
            item_id = str(getattr(item, "id", item.get("id", "")) if isinstance(item, Mapping) else getattr(item, "id", ""))
            item_name = str(item.get("name", "") if isinstance(item, Mapping) else getattr(item, "name", ""))
            item_description = str(item.get("description", "") if isinstance(item, Mapping) else getattr(item, "description", ""))
            haystack_id = _tokens(item_id)
            haystack_tags = [
                token for tag in (item.get("tags", ()) if isinstance(item, Mapping) else getattr(item, "tags", ())) for token in _tokens(str(tag))
            ]
            haystack_name = _tokens(item_name)
            haystack_desc = _tokens(item_description)
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
                scored.append((score, item_id, item))
        scored.sort(key=lambda row: (-row[0], row[1]))
        return [
            item.contract() if callable(getattr(item, "contract", None)) else item.to_catalog() if callable(getattr(item, "to_catalog", None)) else dict(item)
            for _, _, item in scored[: max(1, limit)]
        ]

    def catalog_summary(self, *, limit: int = 40) -> dict[str, Any]:
        """A compact catalog overview for ``session_context``."""
        return {
            "capabilities": [
                {
                    "id": item.id,
                    "name": item.name,
                    "tags": list(getattr(item, "tags", ())),
                    "description": item.description[:200],
                }
                for item in list(self.capabilities.values())[:limit]
            ],
            "workflows": [
                {
                    "id": item.id,
                    "name": item.name,
                    "tags": list(getattr(item, "tags", ())),
                    "description": item.description[:200],
                }
                for item in list(self.workflows.values())[:limit]
            ],
            "capability_count": len(self.capabilities),
            "workflow_count": len(self.workflows),
            "operation_count": len(self._catalog_entries["operations"]),
            "tool_count": len(self._catalog_entries["tools"]),
            "skill_count": len(self._catalog_entries["skills"]),
            "runner_count": len(self._runners),
            "runner_metadata_count": len(self._catalog_entries["runners"]),
        }

    def catalog(self) -> dict[str, Any]:
        """Return a complete, JSON-safe catalog snapshot for legacy adapters."""
        payload: dict[str, Any] = {
            "capabilities": [
                item.contract() if callable(getattr(item, "contract", None)) else item.to_catalog()
                for item in self.capabilities.values()
            ],
            "workflows": [
                item.contract() if callable(getattr(item, "contract", None)) else item.to_catalog()
                for item in self.workflows.values()
            ],
            "operations": deepcopy(self._catalog_entries["operations"]),
            "tools": deepcopy(self._catalog_entries["tools"]),
            "skills": deepcopy(self._catalog_entries["skills"]),
            "runners": deepcopy(self._catalog_entries["runners"]),
            "capability_count": len(self.capabilities),
            "workflow_count": len(self.workflows),
            "operation_count": len(self._catalog_entries["operations"]),
            "tool_count": len(self._catalog_entries["tools"]),
            "skill_count": len(self._catalog_entries["skills"]),
            "runner_count": len(self._runners),
            "runner_metadata_count": len(self._catalog_entries["runners"]),
            "step_kinds": ["answer", "capability", "workflow", "dynamic", "review"],
            "execution_capabilities": [
                "publish_plan",
                "update_step",
                "run_capability",
                "run_workflow",
                "execute_plan",
            ],
        }
        snapshot = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
        payload["snapshot_sha256"] = hashlib.sha256(snapshot).hexdigest()
        return payload

    @staticmethod
    def _check_legacy_identity(identifier: str, name: str, kind: str) -> None:
        if not isinstance(identifier, str) or not identifier.strip():
            raise RegistryError(f"{kind} id must be a non-empty string")
        if not isinstance(name, str) or not name.strip():
            raise RegistryError(f"{kind} name must be a non-empty string")
        if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,159}", identifier) is None:
            raise RegistryError(f"{kind} id has an invalid format")


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
        for kind, handler in registry._handlers.items():
            merged.register_handler(kind, handler)
        for scope, entries in registry._catalog_entries.items():
            for entry in entries:
                merged.register_catalog_entry(scope, entry)
    return merged


def source_ref_of(kind: Literal["upload", "artifact", "scratch"], identifier: str) -> str:
    """Build a canonical ``upload:``/``artifact:``/``scratch:`` reference."""
    return f"{kind}:{identifier}"


async def invoke_handler(handler: Callable[[Any], Any], context: Any) -> StepResult:
    """Invoke sync or async compatibility handlers uniformly."""
    value = handler(context)
    if inspect.isawaitable(value):
        value = await value
    return StepResult.from_value(value)


__all__ = [
    "CapabilitySpec",
    "Capability",
    "CapabilityInput",
    "Parameter",
    "ParameterType",
    "Port",
    "Registry",
    "StepResult",
    "WorkflowSpec",
    "invoke_handler",
    "Workflow",
    "WorkflowNode",
    "merge_registries",
    "source_ref_of",
]
