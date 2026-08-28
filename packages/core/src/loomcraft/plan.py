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

A plan may additionally declare **objectives** — the questions the work is meant
to answer — and an **evidence ledger** (:class:`AnalysisCoverage`) binding each
objective to the steps and artifacts that discharge it. That turns "the agent
said it was done" into "here is the step and the file that answer this
question", and it is what makes an investigative run auditable after the fact.
"""

from __future__ import annotations

import json
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
    holds. Agent-reported, *unless* it binds a review-scoped capability, in which
    case the server owns its result like any other capability step.
``answer``
    Composing the final response for the user.
"""

STEP_KINDS: tuple[str, ...] = ("answer", "capability", "workflow", "dynamic", "review")

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
"""Where a step is in its lifecycle.

``ready`` and ``waiting_approval`` exist because the scheduler needs to say more
than "pending or running": a step whose dependencies are satisfied but which has
not been dispatched is materially different from one that has not been reached,
and a step parked at a human gate is different from one that is working.
"""

STEP_STATUSES: tuple[str, ...] = (
    "pending",
    "ready",
    "running",
    "waiting_approval",
    "succeeded",
    "failed",
    "skipped",
    "cancelled",
)

#: What happens to the rest of the graph when a step fails. ``stop`` (the
#: default) skips everything downstream; ``continue`` lets independent
#: dependents proceed anyway — the honest choice for an exploratory branch whose
#: failure is itself a finding; ``require_approval`` parks the step at a human
#: gate instead of failing it outright.
FAILURE_POLICIES: tuple[str, ...] = ("stop", "continue", "require_approval")

AGENT_REPORTABLE_KINDS: frozenset[str] = frozenset({"answer", "dynamic", "review"})
"""Kinds whose status the agent may set directly through ``update_step``.

``capability`` and ``workflow`` steps are deliberately excluded: their status is
written only by the server-owned execution tools, so a step reading ``succeeded``
always corresponds to a real run with real artifacts. A ``review`` step that
binds a capability is excluded on the same grounds.
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

#: The wire contract accepts large graphs so a host can generate a plan per
#: objective; 24 remains the size a reviewer can actually read at a glance, and
#: is what the examples and the renderer are tuned for.
MAX_STEPS = 256
RECOMMENDED_MAX_STEPS = 24
MAX_REVISION = 1000

STEP_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$"

#: How an objective ended up. ``not_estimable`` is the important one: some
#: questions cannot be answered from the data at hand, and a plan that says so
#: explicitly is worth more than one that quietly drops them.
AnalysisCoverageStatus = Literal[
    "planned", "executed", "not_estimable", "blocked", "deferred_by_scope"
]

COVERAGE_STATUSES: tuple[str, ...] = (
    "planned",
    "executed",
    "not_estimable",
    "blocked",
    "deferred_by_scope",
)

#: Coverage states that owe the reader a next action rather than a result.
UNRESOLVED_COVERAGE: frozenset[str] = frozenset(
    {"not_estimable", "blocked", "deferred_by_scope"}
)


# ── Models ──────────────────────────────────────────────────────────────────


class RetryPolicy(BaseModel):
    """How many times a step may be retried, and how long to wait between tries.

    Declared on the plan so the reader can see the retry budget next to the work
    it applies to. A capability carries its own policy too; the plan value wins
    only when it is explicitly non-default, so publishing a plan never silently
    downgrades a capability that declared ``max_attempts=3``.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    max_attempts: int = Field(default=1, ge=0, le=20)
    backoff_seconds: float = Field(default=0.0, ge=0, le=3600)
    backoff_multiplier: float = Field(default=2.0, ge=1, le=10)
    max_backoff_seconds: float = Field(default=60.0, ge=0, le=86400)

    @model_validator(mode="after")
    def _normalise_zero_attempts(self) -> "RetryPolicy":
        # Zero and one both mean "run it once". Normalising here keeps the
        # scheduler's arithmetic free of a special case.
        if self.max_attempts == 0:
            return self.model_copy(update={"max_attempts": 1})
        return self

    @property
    def is_default(self) -> bool:
        """True when nothing was actually asked for.

        The overlay logic needs to tell "the model omitted retry" from "the
        model asked for exactly one attempt", and a serialized plan looks the
        same either way.
        """
        return (
            self.max_attempts == 1
            and self.backoff_seconds == 0.0
            and self.backoff_multiplier == 2.0
            and self.max_backoff_seconds == 60.0
        )

    def delay_for(self, failed_attempt: int) -> float:
        """Seconds to wait after ``failed_attempt`` failed (1-based)."""
        exponent = max(0, int(failed_attempt) - 1)
        return min(
            self.max_backoff_seconds,
            self.backoff_seconds * (self.backoff_multiplier**exponent),
        )

    def to_dict(self) -> dict[str, Any]:
        return self.model_dump(mode="json")


def _clean_list(values: list[str], *, label: str, limit: int = 300) -> list[str]:
    """Trim, drop empties, reject overlong entries, and de-duplicate in order."""
    cleaned: list[str] = []
    for value in values:
        item = value.strip()
        if not item:
            continue
        if len(item) > limit:
            raise ValueError(f"{label} entries must contain at most {limit} characters")
        if item not in cleaned:
            cleaned.append(item)
    return cleaned


class AnalysisObjective(BaseModel):
    """One question the run is supposed to answer.

    Objectives are declared up front and survive replanning, which is what stops
    a long investigation from quietly narrowing its own scope until whatever it
    managed to compute looks like success.
    """

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    id: str = Field(pattern=STEP_ID_PATTERN)
    #: The question in plain language — what someone actually wants to know.
    question: str = Field(min_length=1, max_length=1000)
    status: AnalysisCoverageStatus | None = None
    #: What quantity would answer it, if the question implies one.
    estimand: str = Field(default="", max_length=500)
    #: What counts as an independent observation — the assumption most often
    #: left implicit, and the one that most often invalidates a result.
    independent_unit: str = Field(default="", max_length=300)
    expected_outputs: list[str] = Field(default_factory=list, max_length=12)
    method_families: list[str] = Field(default_factory=list, max_length=12)
    validation_requirements: list[str] = Field(default_factory=list, max_length=12)

    @field_validator("expected_outputs", "method_families", "validation_requirements")
    @classmethod
    def _normalise_items(cls, values: list[str]) -> list[str]:
        return _clean_list(values, label="objective list")


class AnalysisCoverage(BaseModel):
    """The evidence that discharges one objective — or the reason it cannot be.

    ``executed`` requires a supporting step or artifact: the ledger will not
    accept "done" without something to point at. The unresolved states require a
    ``next_action``, so an unanswered question leaves a thread to pull rather
    than a silence.
    """

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    objective_id: str = Field(pattern=STEP_ID_PATTERN)
    status: AnalysisCoverageStatus
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

    @property
    def is_evidenced(self) -> bool:
        return bool(self.step_ids or self.artifact_refs)


class PlanStep(BaseModel):
    """One node of the plan DAG."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    id: str = Field(pattern=STEP_ID_PATTERN)
    title: str = Field(min_length=1, max_length=160)
    kind: StepKind
    depends_on: list[str] = Field(default_factory=list, max_length=MAX_STEPS)
    #: Registered capability / workflow id. Required for those kinds, optional
    #: for a review bound to a review-scoped capability, and forbidden for the
    #: rest so a ``dynamic`` step can never smuggle in an execution id.
    capability: str | None = Field(default=None, max_length=160)
    description: str = Field(default="", max_length=1000)

    # Execution policy — declared by the model, enforced by the server.
    retry: RetryPolicy = Field(default_factory=RetryPolicy)
    timeout_seconds: float | None = Field(default=None, gt=0)
    on_failure: Literal["stop", "continue", "require_approval"] = "stop"
    metadata: dict[str, Any] = Field(default_factory=dict)

    # Server-owned execution state. Anything a model sends here is discarded by
    # ``validate_plan`` — it exists so the same model can be re-served to the UI.
    status: StepStatus = "pending"
    summary: str | None = Field(default=None, max_length=2000)
    execution: dict[str, Any] | None = None
    attempts: int = Field(default=0, ge=0, le=1000)

    @model_validator(mode="after")
    def _capability_matches_kind(self) -> "PlanStep":
        if self.kind in {"capability", "workflow"}:
            if not self.capability:
                raise ValueError(
                    f"{self.kind} step must reference a registered {self.kind} id"
                )
        elif self.kind == "review":
            # A review may bind a review-scoped capability; validate_plan checks
            # against the registry that it really is one.
            pass
        elif self.capability is not None:
            raise ValueError(f"{self.kind} step cannot declare a capability")
        return self

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL_STEP_STATUSES

    @property
    def is_agent_reportable(self) -> bool:
        """Whether ``update_step`` may write this step's status.

        A ``review`` bound to a capability is server-owned like any other
        execution, so the agent cannot report it complete.
        """
        if self.kind == "review" and self.capability:
            return False
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

    #: Free-form label for the kind of investigation this is, for hosts that
    #: template their objectives. Declaring one requires declaring objectives.
    analysis_profile: str | None = Field(default=None, max_length=500)
    #: The questions this plan exists to answer.
    objectives: list[AnalysisObjective] = Field(default_factory=list, max_length=64)
    #: One entry per objective, saying how it was discharged.
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
        self._validate_coverage(set(ids))
        return self

    def _validate_coverage(self, known_steps: set[str]) -> None:
        """Enforce the objective ledger: every question answered or accounted for."""
        objective_ids = [item.id for item in self.objectives]
        if len(objective_ids) != len(set(objective_ids)):
            raise ValueError("objective ids must be unique")
        covered_ids = [item.objective_id for item in self.analysis_coverage]
        if len(covered_ids) != len(set(covered_ids)):
            raise ValueError("each objective may have at most one coverage entry")

        declared = set(objective_ids)
        unknown = sorted(set(covered_ids) - declared)
        if unknown:
            raise ValueError(
                "coverage references undeclared objectives: " + ", ".join(unknown)
            )
        if self.analysis_profile and not self.objectives:
            raise ValueError("analysis_profile requires at least one objective")
        if self.objectives and not self.analysis_coverage:
            raise ValueError("a plan that declares objectives must also cover them")
        uncovered = sorted(declared - set(covered_ids))
        if uncovered:
            raise ValueError("objectives left uncovered: " + ", ".join(uncovered))

        by_objective = {item.objective_id: item for item in self.analysis_coverage}
        for objective in self.objectives:
            coverage = by_objective.get(objective.id)
            if (
                objective.status is not None
                and coverage is not None
                and objective.status != coverage.status
            ):
                raise ValueError(
                    f"objective {objective.id!r} status disagrees with its coverage"
                )

        for item in self.analysis_coverage:
            missing_steps = sorted(set(item.step_ids) - known_steps)
            if missing_steps:
                raise ValueError(
                    "coverage references unknown steps: " + ", ".join(missing_steps)
                )
            if item.status == "executed" and not item.is_evidenced:
                raise ValueError(
                    f"objective {item.objective_id!r} is marked executed but cites "
                    "no step or artifact as evidence"
                )
            if item.status in UNRESOLVED_COVERAGE and not item.next_action:
                raise ValueError(
                    f"objective {item.objective_id!r} is {item.status} and must "
                    "state a next_action"
                )

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

    def _satisfied(self, dependency: str, statuses: Mapping[str, str]) -> bool:
        """Whether ``dependency`` no longer blocks its dependents.

        Success obviously satisfies. So does a failure the plan explicitly
        marked ``continue`` — that is the author saying this branch is allowed
        to come back empty without taking the rest of the graph down with it.
        """
        status = statuses.get(dependency)
        if status == "succeeded":
            return True
        if status not in {"failed", "skipped"}:
            return False
        upstream = self.by_id.get(dependency)
        return upstream is not None and upstream.on_failure == "continue"

    def ready_steps(self) -> list[PlanStep]:
        """Steps that could start now — dependencies satisfied, not yet run."""
        statuses = {step.id: step.status for step in self.steps}
        return [
            step
            for step in self.steps
            if step.status in {"pending", "ready"}
            and all(self._satisfied(d, statuses) for d in step.depends_on)
        ]

    def blocked_steps(self) -> list[PlanStep]:
        """Pending steps held up by an upstream that did not succeed."""
        statuses = {step.id: step.status for step in self.steps}
        return [
            step
            for step in self.steps
            if step.status == "pending"
            and any(
                statuses.get(d) in {"failed", "skipped", "cancelled"}
                and not self._satisfied(d, statuses)
                for d in step.depends_on
            )
        ]

    @property
    def is_complete(self) -> bool:
        return all(step.is_terminal for step in self.steps)

    @property
    def progress(self) -> dict[str, int]:
        counts = {status: 0 for status in STEP_STATUSES}
        for step in self.steps:
            counts[step.status] += 1
        counts["total"] = len(self.steps)
        return counts

    @property
    def coverage_by_objective(self) -> dict[str, AnalysisCoverage]:
        return {item.objective_id: item for item in self.analysis_coverage}

    @property
    def unresolved_objectives(self) -> list[AnalysisCoverage]:
        """Objectives that ended without an answer, each with its next action."""
        return [
            item
            for item in self.analysis_coverage
            if item.status in UNRESOLVED_COVERAGE
        ]

    def to_dict(self) -> dict[str, Any]:
        return self.model_dump(mode="json")

    def to_json(self, *, indent: int | None = 2) -> str:
        return json.dumps(
            self.to_dict(), ensure_ascii=False, indent=indent, allow_nan=False
        )


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
    """Require a review step's capability to be explicitly review-scoped.

    Without this, ``review`` would be a hole in the kind system: any capability
    could be bound to a step the model describes as verification, and the
    reviewer would have no way to tell checking from doing.
    """
    if not registry.has_capability(step.capability):
        message = f"{step.id}: unknown review capability {step.capability!r}"
        raise PlanValidationError(message, public_message=message)
    capability = registry.capability(step.capability)
    runner = str(getattr(capability, "runner", "") or "")
    tags = {str(item).casefold() for item in getattr(capability, "tags", ())}
    review_scoped = runner.startswith("review.") or "review" in tags
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
                "cannot replace a plan while a step is running or awaiting "
                "approval: " + ", ".join(str(item) for item in running)
            )
            raise PlanValidationError(message, public_message=message)

        # A replan may change how a question gets answered; it may not make the
        # question disappear. Otherwise the easiest way to finish an
        # investigation is to stop asking the part that did not work out.
        previous_objectives = {
            item.get("id")
            for item in current.get("objectives", [])
            if isinstance(item, Mapping) and isinstance(item.get("id"), str)
        }
        dropped = sorted(previous_objectives - {item.id for item in plan.objectives})
        if dropped:
            message = (
                "a revised plan cannot drop declared objectives: "
                + ", ".join(dropped)
                + " — mark them not_estimable or deferred_by_scope instead"
            )
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


def _dependency_satisfied(dependency: Mapping[str, Any] | None) -> bool:
    """Whether a raw dependency row clears the way for its dependents."""
    if dependency is None:
        return False
    status = dependency.get("status")
    if status == "succeeded":
        return True
    return (
        status in {"failed", "skipped"}
        and dependency.get("on_failure", "stop") == "continue"
    )


def ensure_dependencies_succeeded(current: Mapping[str, Any], step_id: str) -> None:
    """Raise unless every dependency of ``step_id`` is satisfied.

    This is the gate that keeps a model from jumping ahead in its own plan: the
    DAG is not decoration, it is an execution precondition. A dependency that
    failed under an explicit ``on_failure="continue"`` policy counts as
    satisfied — the plan author already said this branch may come back empty.
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
        if not _dependency_satisfied(by_id.get(dependency))
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
    root correctly closes out the whole subtree in a single call. A dependency
    marked ``on_failure="continue"`` does not close out its dependents.
    """
    plan = parse_plan(current).to_dict()
    by_id = {step["id"]: step for step in plan["steps"]}
    changed = True
    while changed:
        changed = False
        for step in plan["steps"]:
            if step["status"] != "pending":
                continue
            if any(
                by_id.get(dependency, {}).get("status") in {"failed", "skipped"}
                and not _dependency_satisfied(by_id.get(dependency))
                for dependency in step["depends_on"]
            ):
                step["status"] = "skipped"
                step["summary"] = step["summary"] or "upstream step did not succeed"
                changed = True
    return plan


def task_phase(plan: Mapping[str, Any] | Plan | None, busy: bool = False) -> str:
    """Derive the UI phase without confusing an empty session with a finished one."""
    if plan is None:
        return "orienting" if busy else "idle"
    parsed = plan if isinstance(plan, Plan) else parse_plan(plan)
    if any(step.status in {"running", "waiting_approval"} for step in parsed.steps):
        return "executing"
    if any(step.status in {"pending", "ready"} for step in parsed.steps):
        return "planned"
    return "completed"


def diff_plans(
    previous: Mapping[str, Any] | Plan | None,
    current: Mapping[str, Any] | Plan,
) -> dict[str, Any]:
    """Summarise what one revision changed, for the revision switcher and audit."""
    after = current if isinstance(current, Plan) else parse_plan(current)
    before = (
        None
        if previous is None
        else (previous if isinstance(previous, Plan) else parse_plan(previous))
    )
    before_steps = (
        {step.id: step.to_dict() for step in before.steps} if before else {}
    )
    after_steps = {step.id: step.to_dict() for step in after.steps}
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
    "COVERAGE_STATUSES",
    "FAILURE_POLICIES",
    "MAX_REVISION",
    "MAX_STEPS",
    "RECOMMENDED_MAX_STEPS",
    "STEP_KINDS",
    "STEP_STATUSES",
    "STEP_TRANSITIONS",
    "TERMINAL_STEP_STATUSES",
    "UNRESOLVED_COVERAGE",
    "AnalysisCoverage",
    "AnalysisCoverageStatus",
    "AnalysisObjective",
    "Plan",
    "PlanStep",
    "RetryPolicy",
    "StepKind",
    "StepStatus",
    "diff_plans",
    "ensure_dependencies_succeeded",
    "ensure_step_startable",
    "get_step",
    "parse_plan",
    "propagate_skips",
    "task_phase",
    "update_step",
    "validate_plan",
]
