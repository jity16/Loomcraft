"""Typed error surface for LoomCraft.

Every error the engine can raise is defined here so that hosts can map them onto
HTTP statuses, tool-call error codes, or UI copy without string matching.
"""

from __future__ import annotations


class LoomCraftError(Exception):
    """Base class for every LoomCraft failure."""

    #: Stable machine-readable code surfaced to agents and clients.
    code: str = "LOOMCRAFT_ERROR"

    def __init__(self, message: str, *, public_message: str | None = None) -> None:
        super().__init__(message)
        # ``message`` may embed values that are unsafe to echo back to a model
        # (file contents, absolute paths). ``public_message`` is the bounded,
        # value-free summary the broker teaches the model with.
        self.public_message = public_message or message


# ── Plan / graph ────────────────────────────────────────────────────────────


class PlanValidationError(LoomCraftError):
    """A model-authored plan is not internally consistent or not safe."""

    code = "PLAN_INVALID"


class StepTransitionError(PlanValidationError):
    """A step status transition is not permitted by the state machine."""

    code = "STEP_TRANSITION_INVALID"


class UnknownStepError(PlanValidationError):
    """The referenced step id does not exist in the current plan."""

    code = "STEP_UNKNOWN"


class DependencyError(PlanValidationError):
    """A step was started before its upstream dependencies succeeded."""

    code = "STEP_DEPENDENCIES_INCOMPLETE"


# ── Registry ────────────────────────────────────────────────────────────────


class RegistryError(LoomCraftError):
    """The capability/workflow/runner catalog rejected a lookup or a register."""

    code = "REGISTRY_ERROR"


class UnknownCapabilityError(RegistryError):
    code = "CAPABILITY_UNKNOWN"


class UnknownWorkflowError(RegistryError):
    code = "WORKFLOW_UNKNOWN"


class UnknownRunnerError(RegistryError):
    code = "RUNNER_UNKNOWN"


class ContractError(RegistryError):
    """Typed inputs or parameters do not satisfy a capability contract."""

    code = "CAPABILITY_CONTRACT_VIOLATION"


# ── Execution ───────────────────────────────────────────────────────────────


class ExecutionError(LoomCraftError):
    code = "EXECUTION_FAILED"


class RunCancelledError(ExecutionError):
    code = "RUN_CANCELLED"


class RunTimeoutError(ExecutionError):
    code = "RUN_TIMEOUT"


class GraphStalledError(ExecutionError):
    """No node is runnable, none is in flight, and the run is not complete."""

    code = "GRAPH_STALLED"


class InactiveRunWriteError(ExecutionError):
    """A cancelled or terminal run attempted a late observable write."""

    code = "RUN_INACTIVE_WRITE"


# ── Sources / artifacts ─────────────────────────────────────────────────────


class SourceError(LoomCraftError):
    code = "SOURCE_INVALID"


class SourceIntegrityError(SourceError):
    """A manifest-owned input no longer matches its trusted metadata."""

    code = "SOURCE_INTEGRITY_FAILED"


class ArtifactError(LoomCraftError):
    code = "ARTIFACT_ERROR"


# ── Inputs ──────────────────────────────────────────────────────────────────


class InputRequestError(LoomCraftError):
    """A model-authored request for user files is not safe or well formed."""

    code = "INPUT_REQUEST_INVALID"


class InputFulfillmentError(InputRequestError):
    """Required file slots are still unsatisfied."""

    code = "INPUT_REQUEST_UNFULFILLED"


# ── Event log ───────────────────────────────────────────────────────────────


class EventLogError(LoomCraftError):
    """The append-only event log cannot be extended or recovered safely."""

    code = "EVENT_LOG_CORRUPT"


# ── Broker ──────────────────────────────────────────────────────────────────


class BrokerError(LoomCraftError):
    code = "BROKER_ACTION_FAILED"


class UnsupportedActionError(BrokerError):
    code = "BROKER_ACTION_UNSUPPORTED"


class InvalidArgumentError(BrokerError):
    """A tool payload was structurally wrong in a way schema checks missed."""

    code = "BROKER_INVALID_ARGUMENT"


class ActionBudgetError(BrokerError):
    """The turn exceeded its per-turn tool-call budget."""

    code = "BROKER_ACTION_LIMIT_EXCEEDED"


class RepeatedActionError(BrokerError):
    """The same tool call was repeated without making progress."""

    code = "BROKER_ACTION_REPEATED"


class AwaitingInputsError(BrokerError):
    """The turn is blocked until the user fulfils or cancels a file request."""

    code = "BROKER_AWAITING_INPUTS"


class ExecutionBusyError(BrokerError):
    """A previous execution is still active or awaiting confirmed cleanup."""

    code = "BROKER_EXECUTION_BUSY"


__all__ = [
    "LoomCraftError",
    "PlanValidationError",
    "StepTransitionError",
    "UnknownStepError",
    "DependencyError",
    "RegistryError",
    "UnknownCapabilityError",
    "UnknownWorkflowError",
    "UnknownRunnerError",
    "ContractError",
    "ExecutionError",
    "RunCancelledError",
    "RunTimeoutError",
    "GraphStalledError",
    "InactiveRunWriteError",
    "SourceError",
    "SourceIntegrityError",
    "ArtifactError",
    "InputRequestError",
    "InputFulfillmentError",
    "EventLogError",
    "BrokerError",
    "UnsupportedActionError",
    "InvalidArgumentError",
    "ActionBudgetError",
    "RepeatedActionError",
    "AwaitingInputsError",
    "ExecutionBusyError",
]
