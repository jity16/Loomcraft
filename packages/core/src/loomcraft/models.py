"""Compatibility import surface for the pre-monorepo extracted API.

The canonical contracts live in :mod:`loomcraft.plan` and
:mod:`loomcraft.inputs`.  Keeping this small module means existing applications
can change only their package path while the validator and state machine remain
single-sourced.
"""

from .errors import (
    InputRequestError as InputRequestValidationError,
    PlanValidationError,
)
from .inputs import (
    FileInputRequest,
    FileRequirement,
    allocate_input_uploads,
    allocate_uploads,
    validate_fulfillment,
    validate_input_fulfillment,
    validate_input_request,
)
from .plan import (
    AGENT_REPORTABLE_KINDS,
    AnalysisCoverage,
    AnalysisCoverageStatus,
    AnalysisObjective,
    DEFAULT_MAX_STEPS,
    FAILURE_POLICIES,
    MAX_PLAN_STEPS,
    MAX_REVISION,
    MAX_STEPS,
    Plan,
    PlanStep,
    RetryPolicy,
    STEP_KINDS,
    STEP_STATUSES,
    STEP_TRANSITIONS,
    StepKind,
    StepStatus,
    diff_plans,
    get_step,
    parse_plan,
    propagate_skips,
    task_phase,
    topological_layers,
    topological_order,
    update_step,
    validate_plan,
)

ValidationError = ValueError
TaskPlan = Plan
TaskPlanStep = PlanStep

__all__ = [
    "AGENT_REPORTABLE_KINDS",
    "AnalysisCoverage",
    "AnalysisCoverageStatus",
    "AnalysisObjective",
    "DEFAULT_MAX_STEPS",
    "FAILURE_POLICIES",
    "FileInputRequest",
    "FileRequirement",
    "InputRequestValidationError",
    "PlanValidationError",
    "MAX_PLAN_STEPS",
    "MAX_REVISION",
    "MAX_STEPS",
    "Plan",
    "PlanStep",
    "RetryPolicy",
    "STEP_KINDS",
    "STEP_STATUSES",
    "STEP_TRANSITIONS",
    "StepKind",
    "StepStatus",
    "TaskPlan",
    "TaskPlanStep",
    "ValidationError",
    "allocate_input_uploads",
    "allocate_uploads",
    "diff_plans",
    "get_step",
    "parse_plan",
    "propagate_skips",
    "task_phase",
    "topological_layers",
    "topological_order",
    "update_step",
    "validate_fulfillment",
    "validate_input_fulfillment",
    "validate_input_request",
    "validate_plan",
]
