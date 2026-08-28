"""The Plan protocol: a versioned, validated task DAG authored by an agent.

A Plan is the contract between the model and everything else.  The model
*proposes* one through the ``publish_plan`` tool; the server validates it, freezes
it outside the model's reach, and from then on the plan is the single source of
truth for what may execute, in what order, and what the UI renders.

Three rules make this trustworthy:

1. **Structure is checked, not trusted.** Unique ids, resolvable dependencies,
   acyclicity, and bounded size are enforced before a plan is ever published.
2. **Execution state is server-owned.** A published plan always starts with every
   step ``pending``; the model cannot declare its own work finished.
3. **Revisions are monotonic and explained.** A replan must increase ``revision``
   and, when replacing an existing plan, state a ``reason``. The old revision is
   retained for audit.
"""

from __future__ import annotations

from typing import Any, Literal, Mapping

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .errors import (
    DependencyError,
    PlanValidationError,
    StepTransitionError,
    UnknownStepError,
)
from .graph import GraphIssue, layers, validate as validate_graph

# ── Vocabulary ──────────────────────────────────────────────────────────────

StepKind = Literal["answer", "capability", "workflow", "dynamic", "review"]
STEP_KINDS = ("answer", "capability", "workflow", "dynamic", "review")
FAILURE_POLICIES = ("stop", "continue", "require_approval")
RUN_STATUSES = ("created", "running", "paused_approval", "waiting_approval", "succeeded", "failed", "cancelled", "interrupted")
MAX_INPUT_REQUIREMENTS = 24
"""What a step *is*, which determines who is allowed to complete it.

``capability``
    One typed, server-registered unit of work. Only ``run_capability`` may move
    it, so the model can request the work but never fake the result.
``workflow``
    A registered composite sub-DAG (a fixed SOP). Only ``run_workflow`` moves it.
``dynamic``
    Work the agent performs itself in its sandbox (a script it wrote, a shell
    command). The agent reports status via ``update_step``.
``review``
    An explicit verification step — read the artifacts, decide whether the result
    holds. Also agent-reported.
``answer``
    Composing the final response for the user.
"""

StepStatus = Literal[
    "pending",
    "ready",
    "running",
    "waiting_approval",
    "succeeded",
    "failed",
    "skipped",
    "cancelled",
]
STEP_STATUSES = (
    "pending",
    "ready",
    "running",
    "waiting_approval",
    "succeeded",
    "failed",
    "skipped",
    "cancelled",
)

AGENT_REPORTABLE_KINDS: frozenset[str] = frozenset({"answer", "dynamic", "review"})
"""Kinds whose status the agent may set directly through ``update_step``.

``capability`` and ``workflow`` steps are deliberately excluded: their status is
written only by the server-owned execution tools, so a step reading ``succeeded``
always corresponds to a real run with real artifacts.
"""

#: Allowed step status transitions. ``failed -> running`` and ``skipped ->
#: running`` are what make retry-in-place possible without a replan; ``succeeded``
#: is terminal so a completed step can never be silently rewritten.
STEP_TRANSITIONS: Mapping[str, frozenset[str]] = {
    "pending": frozenset(
        {
            "pending",
            "ready",
            "running",
            "waiting_approval",
            "succeeded",
            "failed",
            "skipped",
            "cancelled",
        }
    ),
    "ready": frozenset({"ready", "running", "skipped", "cancelled"}),
    "running": frozenset(
        {"running", "waiting_approval", "succeeded", "failed", "skipped", "cancelled"}
    ),
    "waiting_approval": frozenset(
        {"waiting_approval", "running", "succeeded", "failed", "cancelled"}
    ),
    "succeeded": frozenset({"succeeded"}),
    "failed": frozenset({"failed", "running", "cancelled"}),
    "skipped": frozenset({"skipped", "running"}),
    "cancelled": frozenset({"cancelled", "running"}),
}

TERMINAL_STEP_STATUSES: frozenset[str] = frozenset(
    {"succeeded", "failed", "skipped", "cancelled"}
)

# 24 is the default visual guideline in the upstream examples.  The wire
# contract accepts larger plans so hosts with generated DAGs can opt into a
# higher ceiling; the renderer remains deterministic for both sizes.
MAX_STEPS = 256
DEFAULT_MAX_STEPS = 24
MAX_PLAN_STEPS = MAX_STEPS
MAX_REVISION = 1_000_000

AnalysisCoverageStatus = Literal[
    "planned", "executed", "not_estimable", "blocked", "deferred_by_scope"
]

STEP_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$"


# ── Models ──────────────────────────────────────────────────────────────────


class RetryPolicy(BaseModel):
    """Per-step retry policy shared by the plan and execution adapters."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    max_attempts: int = Field(default=1, ge=0, le=20)
    backoff_seconds: float = Field(default=0.0, ge=0, le=3600)
    backoff_multiplier: float = Field(default=2.0, ge=1, le=10)
    max_backoff_seconds: float = Field(default=60.0, ge=0, le=86400)

    @classmethod
    def from_raw(cls, raw: object | None) -> "RetryPolicy":
        """Compatibility constructor used by legacy node/edge payloads."""
        if raw is None:
            return cls()
        if isinstance(raw, cls):
            return raw
        return cls.model_validate(raw)

    @model_validator(mode="after")
    def _normalise_zero_attempts(self) -> "RetryPolicy":
        # Older DAG payloads used zero to mean “no retry”.  One total attempt
        # is the unambiguous representation used by the scheduler.
        if self.max_attempts == 0:
            return self.model_copy(update={"max_attempts": 1})
        return self

    def delay_for(self, failed_attempt: int) -> float:
        exponent = max(0, int(failed_attempt) - 1)
        return min(
            self.max_backoff_seconds,
            self.backoff_seconds * (self.backoff_multiplier**exponent),
        )

    def to_dict(self) -> dict[str, Any]:
        return self.model_dump(mode="json")


class AnalysisObjective(BaseModel):
    """Optional, domain-neutral research objective metadata."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    id: str = Field(pattern=STEP_ID_PATTERN)
    question: str = Field(min_length=1, max_length=1000)
    status: AnalysisCoverageStatus | None = None
    estimand: str = Field(default="", max_length=500)
    independent_unit: str = Field(default="", max_length=300)
    expected_outputs: list[str] = Field(default_factory=list, max_length=12)
    method_families: list[str] = Field(default_factory=list, max_length=12)
    validation_requirements: list[str] = Field(default_factory=list, max_length=12)

    @field_validator(
        "expected_outputs", "method_families", "validation_requirements"
    )
    @classmethod
    def _normalise_items(cls, values: list[str]) -> list[str]:
        cleaned: list[str] = []
        for value in values:
            item = value.strip()
            if not item:
                continue
            if len(item) > 300:
                raise ValueError("objective list items must contain at most 300 characters")
            if item not in cleaned:
                cleaned.append(item)
        return cleaned


class AnalysisCoverage(BaseModel):
    """Evidence ledger connecting an objective to steps and artifacts."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    objective_id: str = Field(pattern=STEP_ID_PATTERN)
    status: Literal[
        "planned", "executed", "not_estimable", "blocked", "deferred_by_scope"
    ]
    reason: str = Field(min_length=1, max_length=1000)
    selected_method: str | None = Field(default=None, max_length=300)
    step_ids: list[str] = Field(default_factory=list, max_length=12)
    artifact_refs: list[str] = Field(default_factory=list, max_length=12)
    next_action: str | None = Field(default=None, max_length=500)

    @field_validator("step_ids", "artifact_refs")
    @classmethod
    def _normalise_references(cls, values: list[str]) -> list[str]:
        cleaned: list[str] = []
        for value in values:
            item = value.strip()
            if not item or len(item) > 300:
                raise ValueError("evidence references must contain 1..300 characters")
            if item not in cleaned:
                cleaned.append(item)
        return cleaned


class PlanStep(BaseModel):
    """One node of the plan DAG."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    id: str = Field(pattern=STEP_ID_PATTERN)
    title: str = Field(min_length=1, max_length=160)
    kind: StepKind
    depends_on: list[str] = Field(default_factory=list, max_length=MAX_STEPS)
    #: Registered capability / workflow id. Required for those kinds. A review
    #: may optionally bind a review-only capability; dynamic/answer steps may
    #: never smuggle in an execution id.
    capability: str | None = Field(default=None, max_length=160)
    description: str = Field(default="", max_length=1000)

    # Server-owned execution state. Anything a model sends here is discarded by
    # ``validate_plan`` — it exists so the same model can be re-served to the UI.
    status: StepStatus = "pending"
    summary: str | None = Field(default=None, max_length=2000)
    execution: dict[str, Any] | None = None
    retry: RetryPolicy = Field(default_factory=RetryPolicy)
    timeout_seconds: float | None = Field(default=None, gt=0)
    on_failure: Literal["stop", "continue", "require_approval"] = "stop"
    metadata: dict[str, Any] = Field(default_factory=dict)
    attempts: int = Field(default=0, ge=0, le=1000)

    @model_validator(mode="after")
    def _capability_matches_kind(self) -> "PlanStep":
        if self.kind in {"capability", "workflow"}:
            if not self.capability:
                raise ValueError(
                    f"{self.kind} step must reference a registered {self.kind} id"
                )
        elif self.kind == "review":
            pass
        elif self.capability is not None:
            raise ValueError(f"{self.kind} step cannot declare a capability")
        return self

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL_STEP_STATUSES

    @property
    def is_agent_reportable(self) -> bool:
        return self.kind in AGENT_REPORTABLE_KINDS

    def to_dict(self) -> dict[str, Any]:
        return self.model_dump(mode="json")


class Plan(BaseModel):
    """A complete, versioned task DAG."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    goal: str = Field(min_length=1, max_length=2000)
    summary: str = Field(default="", max_length=2000)
    revision: int = Field(ge=1, le=MAX_REVISION)
    #: Why this revision replaced the previous one. Required for revision > 1.
    reason: str | None = Field(default=None, max_length=2000)
    steps: list[PlanStep] = Field(min_length=1, max_length=MAX_STEPS)
    analysis_profile: str | None = Field(default=None, max_length=500)
    objectives: list[AnalysisObjective] = Field(default_factory=list, max_length=64)
    analysis_coverage: list[AnalysisCoverage] = Field(default_factory=list, max_length=64)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _valid_dag(self) -> "Plan":
        ids = [step.id for step in self.steps]
        duplicates = sorted({i for i in ids if ids.count(i) > 1})
        if duplicates:
            raise ValueError(
                "plan step ids must be unique; duplicated: " + ", ".join(duplicates)
            )
        issues: list[GraphIssue] = validate_graph(self.adjacency)
        if issues:
            raise ValueError("; ".join(str(issue) for issue in issues))
        objective_ids = [item.id for item in self.objectives]
        if len(objective_ids) != len(set(objective_ids)):
            raise ValueError("analysis objective ids must be unique")
        coverage_ids = [item.objective_id for item in self.analysis_coverage]
        if len(coverage_ids) != len(set(coverage_ids)):
            raise ValueError("analysis coverage objective ids must be unique")
        objective_set = set(objective_ids)
        unknown_objectives = sorted(set(coverage_ids) - objective_set)
        if unknown_objectives:
            raise ValueError(
                "analysis coverage references unknown objectives: "
                + ", ".join(unknown_objectives)
            )
        if self.analysis_profile and not self.objectives:
            raise ValueError("analysis_profile requires at least one analysis objective")
        if self.objectives and not self.analysis_coverage:
            raise ValueError("analysis_coverage is required when objectives are declared")
        missing_objectives = sorted(objective_set - set(coverage_ids))
        if missing_objectives:
            raise ValueError(
                "analysis objectives missing coverage: " + ", ".join(missing_objectives)
            )
        known_steps = set(ids)
        coverage_by_id = {item.objective_id: item for item in self.analysis_coverage}
        for objective in self.objectives:
            coverage = coverage_by_id.get(objective.id)
            if objective.status is not None and coverage is not None and objective.status != coverage.status:
                raise ValueError(f"analysis objective {objective.id!r} status disagrees with coverage")
        for item in self.analysis_coverage:
            unknown_steps = sorted(set(item.step_ids) - known_steps)
            if unknown_steps:
                raise ValueError(
                    "analysis coverage references unknown steps: "
                    + ", ".join(unknown_steps)
                )
            if item.status == "executed" and not (item.step_ids or item.artifact_refs):
                raise ValueError(
                    "executed analysis coverage requires a supporting step or artifact"
                )
            if item.status in {"not_estimable", "blocked", "deferred_by_scope"} and not item.next_action:
                raise ValueError(f"{item.status} analysis coverage requires next_action")
        return self

    # ── Derived views ───────────────────────────────────────────────────────

    @property
    def adjacency(self) -> dict[str, list[str]]:
        return {step.id: list(step.depends_on) for step in self.steps}

    @property
    def by_id(self) -> dict[str, PlanStep]:
        return {step.id: step for step in self.steps}

    @property
    def layers(self) -> list[list[str]]:
        """Dependency levels — each inner list may execute concurrently."""
        return layers(self.adjacency)

    def step(self, step_id: str) -> PlanStep:
        found = self.by_id.get(step_id)
        if found is None:
            raise UnknownStepError(f"unknown plan step {step_id!r}")
        return found

    def ready_steps(self) -> list[PlanStep]:
        """Pending steps whose dependencies have all succeeded."""
        statuses = {step.id: step.status for step in self.steps}
        by_id = {step.id: step for step in self.steps}
        return [
            step
            for step in self.steps
            if step.status in {"pending", "ready"}
            and all(
                statuses.get(d) == "succeeded"
                or (
                    statuses.get(d) in {"failed", "skipped"}
                    and by_id.get(d) is not None
                    and by_id[d].on_failure == "continue"
                )
                for d in step.depends_on
            )
        ]

    def blocked_steps(self) -> list[PlanStep]:
        """Pending steps with an upstream that failed or was skipped."""
        statuses = {step.id: step.status for step in self.steps}
        by_id = self.by_id
        return [
            step
            for step in self.steps
            if step.status == "pending"
            and any(
                statuses.get(d) in {"failed", "skipped", "cancelled"}
                and not (
                    statuses.get(d) in {"failed", "skipped"}
                    and by_id.get(d) is not None
                    and by_id[d].on_failure == "continue"
                )
                for d in step.depends_on
            )
        ]

    @property
    def is_complete(self) -> bool:
        return all(step.is_terminal for step in self.steps)

    @property
    def progress(self) -> dict[str, int]:
        counts = {status: 0 for status in STEP_TRANSITIONS}
        for step in self.steps:
            counts[step.status] += 1
        counts["total"] = len(self.steps)
        return counts

    def to_dict(self) -> dict[str, Any]:
        return self.model_dump(mode="json")

    @classmethod
    def from_raw(cls, raw: object, validate_graph: bool = True) -> "Plan":
        """Compatibility constructor with the extracted dataclass API."""
        if isinstance(raw, cls):
            return raw.model_copy(deep=True)
        return cls.model_validate(raw)

    @classmethod
    def from_json(cls, value: str) -> "Plan":
        import json

        try:
            raw = json.loads(value)
        except (TypeError, ValueError) as exc:
            raise PlanValidationError("plan JSON is invalid") from exc
        return cls.from_raw(raw)

    def to_json(self, *, indent: int | None = 2) -> str:
        import json

        return json.dumps(self.to_dict(), ensure_ascii=False, indent=indent, allow_nan=False)

    def validate_graph(self) -> None:
        issues = validate_graph(self.adjacency)
        if issues:
            raise PlanValidationError("; ".join(str(issue) for issue in issues))

    def validate(self, registry: Any | None = None) -> "Plan":
        self.validate_graph()
        if registry is not None:
            for step in self.steps:
                if step.kind == "capability" and not registry.has_capability(step.capability):
                    raise PlanValidationError(
                        "capability step must reference a registered capability"
                    )
                if step.kind == "workflow" and not registry.has_workflow(step.capability):
                    raise PlanValidationError(
                        "workflow step must reference a registered workflow"
                    )
                if step.kind == "review" and step.capability:
                    _validate_review_capability(step, registry)
        return self


# ── Validation entry points ─────────────────────────────────────────────────


def _public_summary(error: Exception) -> str:
    """Field-level validation hints without echoing the rejected input values.

    A model that gets its own bad payload back tends to repeat it. A location +
    reason summary is enough to repair the plan and cannot leak file contents.
    """
    errors_method = getattr(error, "errors", None)
    if not callable(errors_method):
        return str(error)[:1200] or "invalid plan"
    try:
        rows = errors_method()
    except Exception:  # pragma: no cover - defensive boundary around pydantic
        return "invalid plan"
    hints: list[str] = []
    for row in rows[:8]:
        if not isinstance(row, dict):
            continue
        location = row.get("loc") or ()
        rendered = ""
        for part in location if isinstance(location, (tuple, list)) else ():
            rendered = f"{rendered}[{part}]" if isinstance(part, int) else (
                f"{rendered}.{part}" if rendered else str(part)
            )
        message = str(row.get("msg") or "invalid value")
        if message.startswith("Value error, "):
            message = message[len("Value error, ") :]
        hints.append(f"{rendered or 'plan'}: {message}")
    if not hints:
        return "invalid plan"
    suffix = " …" if len(rows) > len(hints) else ""
    return ("; ".join(hints) + suffix)[:1200]


def parse_plan(raw: object) -> Plan:
    """Validate ``raw`` into a :class:`Plan` without applying revision rules."""
    try:
        return Plan.model_validate(raw)
    except PlanValidationError:
        raise
    except Exception as exc:
        raise PlanValidationError(str(exc), public_message=_public_summary(exc)) from exc


def _validate_review_capability(step: PlanStep, registry: Any) -> None:
    """Require an explicitly review-scoped capability for a bound review step."""
    if not registry.has_capability(step.capability):
        raise PlanValidationError(
            f"{step.id}: unknown review capability {step.capability!r}",
            public_message=f"{step.id}: unknown review capability {step.capability!r}",
        )
    capability = registry.capability(step.capability)
    runner = str(getattr(capability, "runner", "") or "")
    tags = {str(item).casefold() for item in getattr(capability, "tags", ())}
    metadata = getattr(capability, "metadata", {})
    declared = metadata.get("step_kinds", ()) if isinstance(metadata, Mapping) else ()
    review_scoped = (
        runner.startswith("review.")
        or "review" in tags
        or (isinstance(declared, (list, tuple, set)) and "review" in declared)
    )
    if not review_scoped:
        message = (
            f"review step {step.id!r} may bind only a capability tagged 'review' "
            "or using a review.* runner"
        )
        raise PlanValidationError(message, public_message=message)


def validate_plan(
    raw: object,
    current: Mapping[str, Any] | None = None,
    *,
    registry: Any | None = None,
) -> dict[str, Any]:
    """Validate and normalise a model-authored plan for trusted publication.

    ``current`` is the plan being replaced, if any. ``registry`` — anything with
    ``has_capability``/``has_workflow`` — additionally checks that every
    ``capability``/``workflow`` step points at something that actually exists,
    so an agent cannot publish a plan it has no way to execute.

    Returns a plain dict with every step reset to ``pending`` and all execution
    state cleared: publication is a *proposal*, never a claim of progress.
    """
    plan = parse_plan(raw)

    if registry is not None:
        unknown: list[str] = []
        for step in plan.steps:
            if step.kind == "capability" and not registry.has_capability(step.capability):
                unknown.append(f"{step.id}: unknown capability {step.capability!r}")
            elif step.kind == "workflow" and not registry.has_workflow(step.capability):
                unknown.append(f"{step.id}: unknown workflow {step.capability!r}")
            elif step.kind == "review" and step.capability:
                try:
                    _validate_review_capability(step, registry)
                except PlanValidationError as exc:
                    unknown.append(exc.public_message)
        if unknown:
            raise PlanValidationError(
                "; ".join(unknown),
                public_message="; ".join(unknown)[:1200],
            )

    current_revision = int((current or {}).get("revision", 0) or 0)
    if plan.revision <= current_revision:
        message = f"plan revision must increase beyond {current_revision}"
        raise PlanValidationError(message, public_message=message)
    if current_revision and not (plan.reason or "").strip():
        message = "a revised plan must explain the replan reason"
        raise PlanValidationError(message, public_message=message)

    if current:
        running = [
            step.get("id")
            for step in current.get("steps", [])
            if isinstance(step, dict)
            and step.get("status") in {"running", "waiting_approval"}
        ]
        if running:
            message = (
                "cannot replace a plan while a step is running: "
                + ", ".join(str(item) for item in running)
            )
            raise PlanValidationError(message, public_message=message)

        previous_objectives = {
            item.get("id")
            for item in current.get("objectives", [])
            if isinstance(item, Mapping) and isinstance(item.get("id"), str)
        }
        current_objectives = {item.id for item in plan.objectives}
        dropped = sorted(previous_objectives - current_objectives)
        if dropped:
            message = "a revised plan cannot silently drop analysis objectives: " + ", ".join(dropped)
            raise PlanValidationError(message, public_message=message)

    normalized = plan.to_dict()
    for step in normalized["steps"]:
        step["status"] = "pending"
        step["summary"] = None
        step["execution"] = None
        step["attempts"] = 0
    return normalized


def update_step(
    current: Mapping[str, Any],
    step_id: str,
    status: StepStatus,
    *,
    summary: str | None = None,
    execution: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Return a validated copy of ``current`` with one trusted state update.

    Enforces the :data:`STEP_TRANSITIONS` state machine. The result is
    re-validated as a whole, so a state write can never leave a plan that the
    engine or the renderer would refuse to load.
    """
    if status not in STEP_TRANSITIONS:
        raise StepTransitionError(f"unsupported step status {status!r}")
    plan = parse_plan(current).to_dict()
    target = next((step for step in plan["steps"] if step["id"] == step_id), None)
    if target is None:
        raise UnknownStepError(f"unknown plan step {step_id!r}")

    previous = target["status"]
    if status not in STEP_TRANSITIONS[previous]:
        message = f"invalid step transition {previous!r} -> {status!r}"
        raise StepTransitionError(message, public_message=message)

    target["status"] = status
    if summary is not None:
        target["summary"] = summary[:2000]
    if execution is not None:
        target["execution"] = dict(execution)
    return parse_plan(plan).to_dict()


def get_step(current: Mapping[str, Any], step_id: str) -> dict[str, Any]:
    """Return one step of a persisted plan as a plain dict."""
    plan = parse_plan(current).to_dict()
    step = next((row for row in plan["steps"] if row["id"] == step_id), None)
    if step is None:
        raise UnknownStepError(f"unknown plan step {step_id!r}")
    return step


def ensure_dependencies_succeeded(current: Mapping[str, Any], step_id: str) -> None:
    """Raise unless every dependency of ``step_id`` has succeeded.

    This is the gate that keeps a model from jumping ahead in its own plan: the
    DAG is not decoration, it is an execution precondition.
    """
    by_id = {
        step["id"]: step
        for step in current.get("steps", [])
        if isinstance(step, dict) and "id" in step
    }
    step = by_id.get(step_id)
    if step is None:
        raise UnknownStepError(f"unknown plan step {step_id!r}")
    incomplete = [
        dependency
        for dependency in step.get("depends_on", [])
        if by_id.get(dependency, {}).get("status") != "succeeded"
        and not (
            by_id.get(dependency, {}).get("status") in {"failed", "skipped"}
            and by_id.get(dependency, {}).get("on_failure") == "continue"
        )
    ]
    if incomplete:
        message = (
            f"step {step_id!r} has incomplete dependencies: " + ", ".join(incomplete)
        )
        raise DependencyError(message, public_message=message)


def ensure_step_startable(
    current: Mapping[str, Any],
    step_id: str,
    *,
    kind: str,
    capability: str | None = None,
) -> dict[str, Any]:
    """Validate that ``step_id`` may start now as ``kind``/``capability``.

    Checks kind agreement, capability authorisation, dependency readiness, and
    that the step has not already run. Returns the plan dict for the caller to
    keep mutating.
    """
    step = get_step(current, step_id)
    if step["kind"] != kind:
        message = f"step {step_id!r} is {step['kind']!r}, expected {kind!r}"
        raise PlanValidationError(message, public_message=message)
    if capability is not None and step.get("capability") != capability:
        message = f"step {step_id!r} does not authorize {capability!r}"
        raise PlanValidationError(message, public_message=message)
    ensure_dependencies_succeeded(current, step_id)
    if step["status"] != "pending":
        message = (
            f"step {step_id!r} cannot start from {step['status']!r}; "
            "publish a revised plan or retry the step first"
        )
        raise StepTransitionError(message, public_message=message)
    return dict(current)


def propagate_skips(current: Mapping[str, Any]) -> dict[str, Any]:
    """Mark every pending step whose upstream failed/was skipped as ``skipped``.

    Applied repeatedly until it reaches a fixed point, so one failure near the
    root correctly closes out the whole subtree in a single call.
    """
    plan = parse_plan(current).to_dict()
    changed = True
    while changed:
        changed = False
        statuses = {step["id"]: step["status"] for step in plan["steps"]}
        for step in plan["steps"]:
            if step["status"] != "pending":
                continue
            if any(
                statuses.get(dependency) in {"failed", "skipped"}
                and next(
                    (
                        candidate.get("on_failure", "stop")
                        for candidate in plan["steps"]
                        if candidate["id"] == dependency
                    ),
                    "stop",
                )
                != "continue"
                for dependency in step["depends_on"]
            ):
                step["status"] = "skipped"
                step["summary"] = step["summary"] or "upstream step did not succeed"
                changed = True
    return plan


def topological_order(plan: Plan | Mapping[str, Any]) -> list[str]:
    """Stable topological order for a Plan (compatibility convenience)."""
    parsed = plan if isinstance(plan, Plan) else parse_plan(plan)
    from .graph import topological_order as graph_order

    return graph_order(parsed.adjacency)


def topological_layers(plan: Plan | Mapping[str, Any]) -> list[list[str]]:
    """Dependency layers; each layer is eligible for concurrent execution."""
    parsed = plan if isinstance(plan, Plan) else parse_plan(plan)
    return layers(parsed.adjacency)


def task_phase(plan: Plan | Mapping[str, Any] | None, busy: bool = False) -> str:
    """Derive a UI phase without conflating an empty session with completion."""
    if plan is None:
        return "orienting" if busy else "idle"
    parsed = plan if isinstance(plan, Plan) else parse_plan(plan)
    if any(step.status in {"running", "waiting_approval"} for step in parsed.steps):
        return "executing"
    if any(step.status in {"pending", "ready"} for step in parsed.steps):
        return "planned"
    return "completed"


def diff_plans(
    previous: Plan | Mapping[str, Any] | None,
    current: Plan | Mapping[str, Any],
) -> dict[str, Any]:
    """Return a stable revision diff for UI/audit consumers."""
    after = current if isinstance(current, Plan) else parse_plan(current)
    before = None if previous is None else (previous if isinstance(previous, Plan) else parse_plan(previous))
    before_steps = {step.id: step.model_dump(mode="json") for step in before.steps} if before else {}
    after_steps = {step.id: step.model_dump(mode="json") for step in after.steps}
    before_objectives = {item.id for item in before.objectives} if before else set()
    after_objectives = {item.id for item in after.objectives}
    return {
        "from_revision": before.revision if before else None,
        "to_revision": after.revision,
        "added_steps": sorted(set(after_steps) - set(before_steps)),
        "removed_steps": sorted(set(before_steps) - set(after_steps)),
        "changed_steps": sorted(
            identifier
            for identifier in set(before_steps) & set(after_steps)
            if before_steps[identifier] != after_steps[identifier]
        ),
        "added_objectives": sorted(after_objectives - before_objectives),
        "removed_objectives": sorted(before_objectives - after_objectives),
        "reason": after.reason,
    }


__all__ = [
    "AGENT_REPORTABLE_KINDS",
    "MAX_REVISION",
    "MAX_STEPS",
    "MAX_PLAN_STEPS",
    "DEFAULT_MAX_STEPS",
    "FAILURE_POLICIES",
    "RUN_STATUSES",
    "MAX_INPUT_REQUIREMENTS",
    "STEP_KINDS",
    "STEP_STATUSES",
    "AnalysisCoverage",
    "AnalysisCoverageStatus",
    "AnalysisObjective",
    "RetryPolicy",
    "Plan",
    "PlanStep",
    "STEP_TRANSITIONS",
    "TERMINAL_STEP_STATUSES",
    "StepKind",
    "StepStatus",
    "ensure_dependencies_succeeded",
    "ensure_step_startable",
    "get_step",
    "parse_plan",
    "propagate_skips",
    "update_step",
    "validate_plan",
    "topological_order",
    "topological_layers",
    "task_phase",
    "diff_plans",
]
