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

from pydantic import BaseModel, ConfigDict, Field, model_validator

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
    holds. Also agent-reported.
``answer``
    Composing the final response for the user.
"""

StepStatus = Literal["pending", "running", "succeeded", "failed", "skipped"]

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
    "pending": frozenset({"pending", "running", "succeeded", "failed", "skipped"}),
    "running": frozenset({"running", "succeeded", "failed", "skipped"}),
    "succeeded": frozenset({"succeeded"}),
    "failed": frozenset({"failed", "running"}),
    "skipped": frozenset({"skipped", "running"}),
}

TERMINAL_STEP_STATUSES: frozenset[str] = frozenset({"succeeded", "failed", "skipped"})

MAX_STEPS = 24
MAX_REVISION = 100

STEP_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$"


# ── Models ──────────────────────────────────────────────────────────────────


class PlanStep(BaseModel):
    """One node of the plan DAG."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    id: str = Field(pattern=STEP_ID_PATTERN)
    title: str = Field(min_length=1, max_length=160)
    kind: StepKind
    depends_on: list[str] = Field(default_factory=list, max_length=MAX_STEPS)
    #: Registered capability / workflow id. Required for those kinds, forbidden
    #: for the rest so a ``dynamic`` step can never smuggle in an execution id.
    capability: str | None = Field(default=None, max_length=160)
    description: str = Field(default="", max_length=1000)

    # Server-owned execution state. Anything a model sends here is discarded by
    # ``validate_plan`` — it exists so the same model can be re-served to the UI.
    status: StepStatus = "pending"
    summary: str | None = Field(default=None, max_length=2000)
    execution: dict[str, Any] | None = None

    @model_validator(mode="after")
    def _capability_matches_kind(self) -> "PlanStep":
        if self.kind in {"capability", "workflow"}:
            if not self.capability:
                raise ValueError(
                    f"{self.kind} step must reference a registered {self.kind} id"
                )
        elif self.capability is not None:
            raise ValueError(f"{self.kind} step cannot declare a capability")
        return self

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL_STEP_STATUSES

    @property
    def is_agent_reportable(self) -> bool:
        return self.kind in AGENT_REPORTABLE_KINDS


class Plan(BaseModel):
    """A complete, versioned task DAG."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    goal: str = Field(min_length=1, max_length=2000)
    summary: str = Field(default="", max_length=2000)
    revision: int = Field(ge=1, le=MAX_REVISION)
    #: Why this revision replaced the previous one. Required for revision > 1.
    reason: str | None = Field(default=None, max_length=2000)
    steps: list[PlanStep] = Field(min_length=1, max_length=MAX_STEPS)

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
        return [
            step
            for step in self.steps
            if step.status == "pending"
            and all(statuses.get(d) == "succeeded" for d in step.depends_on)
        ]

    def blocked_steps(self) -> list[PlanStep]:
        """Pending steps with an upstream that failed or was skipped."""
        statuses = {step.id: step.status for step in self.steps}
        return [
            step
            for step in self.steps
            if step.status == "pending"
            and any(statuses.get(d) in {"failed", "skipped"} for d in step.depends_on)
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
            if isinstance(step, dict) and step.get("status") == "running"
        ]
        if running:
            message = (
                "cannot replace a plan while a step is running: "
                + ", ".join(str(item) for item in running)
            )
            raise PlanValidationError(message, public_message=message)

    normalized = plan.to_dict()
    for step in normalized["steps"]:
        step["status"] = "pending"
        step["summary"] = None
        step["execution"] = None
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
                for dependency in step["depends_on"]
            ):
                step["status"] = "skipped"
                step["summary"] = step["summary"] or "upstream step did not succeed"
                changed = True
    return plan


__all__ = [
    "AGENT_REPORTABLE_KINDS",
    "MAX_REVISION",
    "MAX_STEPS",
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
]
