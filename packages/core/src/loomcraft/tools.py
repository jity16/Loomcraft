"""The agent tool surface: what the model is allowed to do, as JSON Schema.

These ten tools are the entire contract between a model and LoomCraft.  There is
no generic "run this Python", no database handle, no HTTP escape hatch — the
model can look things up, publish a plan, ask for files, run *registered* work,
and register deliverables.  Everything else is refused by the broker.

The specs are provider-neutral. :func:`tool_specs` returns the canonical form;
the ``to_*`` adapters reshape it for the Anthropic Messages API, OpenAI-style
function calling, or MCP, so the same validated surface works across providers
without three copies of the schema drifting apart.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, Sequence

from .plan import MAX_REVISION, MAX_STEPS, STEP_ID_PATTERN
from .store import MAX_ARTIFACT_BATCH

Dialect = Literal["canonical", "anthropic", "openai", "openai_responses", "mcp"]

# ── Tool names ──────────────────────────────────────────────────────────────

SESSION_CONTEXT = "session_context"
CAPABILITY_SEARCH = "capability_search"
CATALOG_SEARCH = "catalog_search"
INSPECT_SOURCE = "inspect_source"
PUBLISH_PLAN = "publish_plan"
UPDATE_STEP = "update_step"
REQUEST_INPUTS = "request_inputs"
RUN_CAPABILITY = "run_capability"
RUN_WORKFLOW = "run_workflow"
REGISTER_ARTIFACTS = "register_artifacts"
EXECUTE_PLAN = "execute_plan"

#: Read-only tools. The broker keeps these available even while a turn is
#: blocked waiting for user files, because gathering evidence is always safe.
READ_ONLY_TOOLS: frozenset[str] = frozenset(
    {SESSION_CONTEXT, CAPABILITY_SEARCH, CATALOG_SEARCH, INSPECT_SOURCE}
)

#: Tools that change durable state or start work.
MUTATING_TOOLS: frozenset[str] = frozenset(
    {
        PUBLISH_PLAN,
        UPDATE_STEP,
        REQUEST_INPUTS,
        RUN_CAPABILITY,
        RUN_WORKFLOW,
        REGISTER_ARTIFACTS,
        EXECUTE_PLAN,
    }
)


@dataclass(frozen=True, slots=True)
class ToolSpec:
    """One provider-neutral tool definition."""

    name: str
    description: str
    parameters: dict[str, Any]

    def to_anthropic(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.parameters,
        }

    def to_openai(self) -> dict[str, Any]:
        """Chat Completions function-calling shape."""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }

    def to_openai_responses(self) -> dict[str, Any]:
        """Responses API / Codex app-server dynamic-tool shape."""
        return {
            "type": "function",
            "name": self.name,
            "description": self.description,
            "inputSchema": self.parameters,
        }

    def to_mcp(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "inputSchema": self.parameters,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "parameters": self.parameters,
        }


# ── Shared schema fragments ─────────────────────────────────────────────────

_STRING = {"type": "string"}
_NONEMPTY = {"type": "string", "minLength": 1}
_OBJECT = {"type": "object"}
_STEP_ID = {"type": "string", "pattern": STEP_ID_PATTERN}

PLAN_STEP_SCHEMA: dict[str, Any] = {
    "type": "object",
    "description": (
        "One DAG step. Use title/kind (not label/type). `capability` is required "
        "for capability and workflow steps, optional on a review step that binds "
        "a review-scoped capability, and forbidden otherwise."
    ),
    "properties": {
        "id": _STEP_ID,
        "title": {"type": "string", "minLength": 1, "maxLength": 160},
        "kind": {
            "type": "string",
            "enum": ["answer", "capability", "workflow", "dynamic", "review"],
            "description": (
                "answer = compose the final reply; capability = one registered "
                "typed unit of work; workflow = a registered multi-step SOP; "
                "dynamic = work you perform yourself in scratch; review = verify "
                "produced artifacts before relying on them."
            ),
        },
        "depends_on": {
            "type": "array",
            "maxItems": MAX_STEPS,
            "items": _STEP_ID,
            "description": "Step ids that must succeed before this step may run.",
        },
        "capability": {"type": ["string", "null"], "maxLength": 160},
        "description": {"type": "string", "maxLength": 1000},
        "retry": {
            "type": "object",
            "description": (
                "Retry budget for this step. Omit to inherit the capability's "
                "own policy; setting it overrides that policy."
            ),
            "properties": {
                "max_attempts": {"type": "integer", "minimum": 0, "maximum": 20},
                "backoff_seconds": {"type": "number", "minimum": 0, "maximum": 3600},
                "backoff_multiplier": {"type": "number", "minimum": 1, "maximum": 10},
                "max_backoff_seconds": {
                    "type": "number",
                    "minimum": 0,
                    "maximum": 86400,
                },
            },
            "additionalProperties": False,
        },
        "timeout_seconds": {
            "type": ["number", "null"],
            "exclusiveMinimum": 0,
            "description": "Wall-clock ceiling for one attempt of this step.",
        },
        "on_failure": {
            "type": "string",
            "enum": ["stop", "continue", "require_approval"],
            "description": (
                "stop = skip everything downstream (default); continue = let "
                "independent dependents run anyway, for a branch whose empty "
                "result is still a result; require_approval = park at a human "
                "decision instead of failing."
            ),
        },
        "metadata": {
            "type": "object",
            "description": "Free-form annotations carried through to the UI.",
        },
    },
    "required": ["id", "title", "kind"],
    "additionalProperties": False,
}

ANALYSIS_OBJECTIVE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "description": (
        "One question this plan exists to answer. Declare these before you "
        "execute, so the record shows what was asked, not only what was run."
    ),
    "properties": {
        "id": _STEP_ID,
        "question": {"type": "string", "minLength": 1, "maxLength": 1000},
        "status": {
            "type": ["string", "null"],
            "enum": [
                "planned",
                "executed",
                "not_estimable",
                "blocked",
                "deferred_by_scope",
                None,
            ],
        },
        "estimand": {
            "type": "string",
            "maxLength": 500,
            "description": "The quantity that would answer the question.",
        },
        "independent_unit": {
            "type": "string",
            "maxLength": 300,
            "description": "What counts as one independent observation.",
        },
        "expected_outputs": {
            "type": "array",
            "maxItems": 12,
            "items": {"type": "string", "maxLength": 300},
        },
        "method_families": {
            "type": "array",
            "maxItems": 12,
            "items": {"type": "string", "maxLength": 300},
        },
        "validation_requirements": {
            "type": "array",
            "maxItems": 12,
            "items": {"type": "string", "maxLength": 300},
        },
    },
    "required": ["id", "question"],
    "additionalProperties": False,
}

ANALYSIS_COVERAGE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "description": (
        "How one objective was discharged. `executed` requires a step or "
        "artifact as evidence; not_estimable/blocked/deferred_by_scope require "
        "a next_action. Saying a question could not be answered is a valid "
        "result — silently dropping it is not."
    ),
    "properties": {
        "objective_id": _STEP_ID,
        "status": {
            "type": "string",
            "enum": [
                "planned",
                "executed",
                "not_estimable",
                "blocked",
                "deferred_by_scope",
            ],
        },
        "reason": {"type": "string", "minLength": 1, "maxLength": 1000},
        "selected_method": {"type": ["string", "null"], "maxLength": 300},
        "step_ids": {"type": "array", "maxItems": 12, "items": _STEP_ID},
        "artifact_refs": {
            "type": "array",
            "maxItems": 12,
            "items": {"type": "string", "minLength": 1, "maxLength": 300},
        },
        "next_action": {"type": ["string", "null"], "maxLength": 500},
    },
    "required": ["objective_id", "status", "reason"],
    "additionalProperties": False,
}

PLAN_SCHEMA: dict[str, Any] = {
    "type": "object",
    "description": (
        "A complete versioned task DAG. Required fields are goal, revision and "
        "steps. Step execution state is owned by the server — do not send it."
    ),
    "properties": {
        "goal": {"type": "string", "minLength": 1, "maxLength": 2000},
        "summary": {"type": "string", "maxLength": 2000},
        "revision": {"type": "integer", "minimum": 1, "maximum": MAX_REVISION},
        "reason": {
            "type": ["string", "null"],
            "maxLength": 2000,
            "description": "Why this revision replaces the previous one. Required when revising.",
        },
        "analysis_profile": {"type": ["string", "null"], "maxLength": 500},
        "objectives": {
            "type": "array",
            "maxItems": 64,
            "items": ANALYSIS_OBJECTIVE_SCHEMA,
        },
        "analysis_coverage": {
            "type": "array",
            "maxItems": 64,
            "items": ANALYSIS_COVERAGE_SCHEMA,
            "description": "One entry per declared objective.",
        },
        "metadata": {"type": "object"},
        "steps": {
            "type": "array",
            "minItems": 1,
            "maxItems": MAX_STEPS,
            "items": PLAN_STEP_SCHEMA,
        },
    },
    "required": ["goal", "revision", "steps"],
    "additionalProperties": False,
}

FILE_REQUIREMENT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "key": {"type": "string", "pattern": r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$"},
        "label": {"type": "string", "minLength": 1, "maxLength": 160},
        "description": {"type": "string", "minLength": 1, "maxLength": 1000},
        "required": {"type": "boolean"},
        "min_files": {"type": "integer", "minimum": 0, "maximum": 12},
        "max_files": {"type": "integer", "minimum": 1, "maximum": 12},
        "allowed_extensions": {
            "type": "array",
            "maxItems": 32,
            "items": {"type": "string"},
            "description": "Lower-case extensions including the dot, e.g. '.csv'.",
        },
        "field_hints": {
            "type": "array",
            "maxItems": 32,
            "items": {"type": "string"},
            "description": "Columns or fields you expect to find inside the file.",
        },
    },
    "required": ["key", "label", "description", "required", "min_files", "max_files"],
    "additionalProperties": False,
}

INPUT_REQUEST_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "title": {"type": "string", "minLength": 1, "maxLength": 160},
        "message": {"type": "string", "minLength": 1, "maxLength": 2000},
        "requirements": {
            "type": "array",
            "minItems": 1,
            "maxItems": 24,
            "items": FILE_REQUIREMENT_SCHEMA,
        },
        "continue_prompt": {"type": "string", "minLength": 1, "maxLength": 2000},
    },
    "required": ["title", "message", "requirements", "continue_prompt"],
    "additionalProperties": False,
}

ARTIFACT_ITEM_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "path": {
            "type": "string",
            "minLength": 1,
            "description": "Relative to scratch, or prefixed with 'scratch/'.",
        },
        "display_name": {"type": ["string", "null"]},
    },
    "required": ["path"],
    "additionalProperties": False,
}


def _tool(
    name: str,
    description: str,
    properties: dict[str, Any],
    required: Sequence[str] = (),
) -> ToolSpec:
    return ToolSpec(
        name=name,
        description=description,
        parameters={
            "type": "object",
            "properties": properties,
            "required": list(required),
            "additionalProperties": False,
        },
    )


def tool_specs(
    *,
    include_workflows: bool = True,
    include_inspection: bool = True,
    include_plan_execution: bool = True,
    max_search_results: int = 10,
) -> list[ToolSpec]:
    """Return the canonical tool surface.

    Drop tools your deployment does not offer — a registry with no workflows
    should pass ``include_workflows=False`` so the model never proposes a
    ``workflow`` step it cannot run. ``include_plan_execution=False`` removes
    ``execute_plan``, leaving the agent to dispatch each step itself.
    """
    specs: list[ToolSpec] = [
        _tool(
            SESSION_CONTEXT,
            "Return trusted facts about this task: uploads, current plan, past "
            "executions, registered artifacts, and a catalog summary. Call this "
            "first — it is cheap and prevents guessing.",
            {},
        ),
        _tool(
            CAPABILITY_SEARCH,
            "Find registered typed capabilities relevant to a task. Returns full "
            "input/parameter contracts. Discovery does not authorize a run — you "
            "still need a capability step in a published plan.",
            {
                "query": _NONEMPTY,
                "limit": {"type": "integer", "minimum": 1, "maximum": max_search_results},
            },
            ["query"],
        ),
        _tool(
            CATALOG_SEARCH,
            "Search the whole pinned catalog (capabilities and workflows) and "
            "return compact facts.",
            {
                "query": _NONEMPTY,
                "scope": {
                    "type": "string",
                    "enum": ["all", "capabilities", "workflows"],
                },
                "limit": {"type": "integer", "minimum": 1, "maximum": max_search_results},
            },
            ["query"],
        ),
    ]

    if include_inspection:
        specs.append(
            _tool(
                INSPECT_SOURCE,
                "Read a bounded preview of one session-owned file without "
                "modifying it. Accepts upload:<id>, artifact:<id>, or "
                "scratch:<relative-path>. Use this to check real structure "
                "before committing to a plan.",
                {
                    "source_ref": _NONEMPTY,
                    "max_bytes": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 262144,
                    },
                    "max_lines": {"type": "integer", "minimum": 1, "maximum": 200},
                },
                ["source_ref"],
            )
        )

    specs.extend(
        [
            _tool(
                PUBLISH_PLAN,
                "Validate and publish a complete versioned task DAG. Must contain "
                "goal, revision, and steps. Publish before executing anything. To "
                "change course, publish a higher revision with a `reason`.",
                {"plan": PLAN_SCHEMA},
                ["plan"],
            ),
            _tool(
                UPDATE_STEP,
                "Report status for one answer/dynamic/review step you performed "
                "yourself. Capability and workflow steps are updated only by their "
                "execution tools.",
                {
                    "step_id": _STEP_ID,
                    "status": {
                        "type": "string",
                        "enum": ["running", "succeeded", "failed", "skipped"],
                    },
                    "summary": {"type": ["string", "null"], "maxLength": 2000},
                },
                ["step_id", "status"],
            ),
            _tool(
                REQUEST_INPUTS,
                "Publish a structured request for missing user files and end your "
                "turn. No plan or execution action is accepted until the user "
                "responds.",
                {"request": INPUT_REQUEST_SCHEMA},
                ["request"],
            ),
            _tool(
                RUN_CAPABILITY,
                "Run one registered capability, authorized by a matching "
                "capability step in the current plan. Inputs map capability input "
                "keys to source refs (upload:/artifact:/scratch:).",
                {
                    "capability_id": _NONEMPTY,
                    "step_id": _STEP_ID,
                    "inputs": _OBJECT,
                    "parameters": _OBJECT,
                },
                ["capability_id", "step_id", "inputs"],
            ),
        ]
    )

    if include_workflows:
        specs.append(
            _tool(
                RUN_WORKFLOW,
                "Run a registered multi-step workflow, authorized by a matching "
                "workflow step in the current plan.",
                {
                    "workflow_id": _NONEMPTY,
                    "step_id": _STEP_ID,
                    "inputs": _OBJECT,
                    "parameters": _OBJECT,
                },
                ["workflow_id", "step_id", "inputs"],
            )
        )

    specs.append(
        _tool(
            REGISTER_ARTIFACTS,
            f"Register 1..{MAX_ARTIFACT_BATCH} files you produced in scratch as "
            "session deliverables, in one atomic batch. Only registered files are "
            "downloadable by the user.",
            {
                "step_id": _STEP_ID,
                "artifacts": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": MAX_ARTIFACT_BATCH,
                    "items": ARTIFACT_ITEM_SCHEMA,
                },
            },
            ["step_id", "artifacts"],
        )
    )
    if include_plan_execution:
        specs.append(
            _tool(
                EXECUTE_PLAN,
                "Run the whole published plan in one audited execution. "
                "Independent steps run concurrently, each step uses its own "
                "retry and timeout, and the run parks if a step needs approval. "
                "Prefer this once the plan is settled; use run_capability when "
                "you need to think between steps. Returns when the run finishes "
                "or pauses — read the result before claiming anything.",
                {
                    "inputs": {
                        "type": "object",
                        "description": (
                            "Optional per-step bindings, keyed by step id: "
                            '{"qc": {"inputs": {"table": "upload:abc"}, '
                            '"parameters": {"threshold": 0.05}}}. Steps fed by '
                            "an upstream step need no entry."
                        ),
                    },
                    "timeout_seconds": {
                        "type": ["number", "null"],
                        "exclusiveMinimum": 0,
                        "description": "Ceiling for the whole plan, not one step.",
                    },
                },
            )
        )
    return specs


def dynamic_tool_specs(**kwargs: Any) -> list[dict[str, Any]]:
    """The tool catalog in the shape an app-server host passes to a model.

    Codex and other app-server runtimes take a list of plain tool objects and
    call back with ``item/tool/call``; this is that list. See
    :class:`loomcraft.protocol.AppServerBridge` for the other half.
    """
    return to_dialect(tool_specs(**kwargs), "openai_responses")


def to_dialect(specs: Sequence[ToolSpec], dialect: Dialect = "canonical") -> list[dict[str, Any]]:
    """Reshape canonical specs for a specific provider."""
    if dialect == "anthropic":
        return [spec.to_anthropic() for spec in specs]
    if dialect == "openai":
        return [spec.to_openai() for spec in specs]
    if dialect == "openai_responses":
        return [spec.to_openai_responses() for spec in specs]
    if dialect == "mcp":
        return [spec.to_mcp() for spec in specs]
    if dialect == "canonical":
        return [spec.to_dict() for spec in specs]
    raise ValueError(f"unsupported tool dialect {dialect!r}")


def anthropic_tools(**kwargs: Any) -> list[dict[str, Any]]:
    """Tool definitions for ``client.messages.create(tools=...)``."""
    return to_dialect(tool_specs(**kwargs), "anthropic")


def openai_tools(**kwargs: Any) -> list[dict[str, Any]]:
    """Tool definitions for OpenAI-style ``tools=[{"type": "function", ...}]``."""
    return to_dialect(tool_specs(**kwargs), "openai")


def mcp_tools(**kwargs: Any) -> list[dict[str, Any]]:
    """Tool definitions for an MCP server's ``tools/list`` response."""
    return to_dialect(tool_specs(**kwargs), "mcp")


SYSTEM_PROMPT = """\
You are an autonomous task agent operating inside LoomCraft.

Work in this order:

1. Orient. Call `session_context` to see the files, plan, and catalog you already
   have. Use `inspect_source` on real files before assuming their structure, and
   `capability_search` to find work you are allowed to run.
2. Decide whether you can proceed. If required files are missing, call
   `request_inputs` with typed slots and end your turn. Do not guess at data.
3. State the questions. When the task is investigative, declare `objectives` —
   the questions the work must answer — in the plan. Give each one an
   `analysis_coverage` entry. You will be held to them: a later revision may not
   drop an objective, only mark it answered, `not_estimable`, `blocked`, or
   `deferred_by_scope` with a `next_action`.
4. Publish a plan. Call `publish_plan` with a DAG whose `depends_on` edges reflect
   real data dependencies — independent steps with no edge between them run in
   parallel, so do not serialise work that need not be sequential. Set `retry`,
   `timeout_seconds` and `on_failure` where the work warrants it.
5. Execute. Either call `execute_plan` to run the settled graph in one go, or
   drive it step by step with `run_capability` / `run_workflow` when you need to
   reason in between. Do `dynamic` and unbound `review` steps yourself and report
   them with `update_step`. Read the artifacts a step produced before you claim
   it succeeded.
6. Replan on failure. If a step fails, publish a higher `revision` with a `reason`
   explaining what you learned and what you will do differently. Do not retry the
   same call unchanged.
7. Deliver. Register final files with `register_artifacts`. Answer with what you
   actually verified, and say plainly which objectives went unanswered and why.

Rules the server enforces, so do not fight them:

- A step only runs when all of its dependencies have succeeded — unless a
  dependency declared `on_failure: continue`.
- You cannot set the status of a `capability` or `workflow` step, or of a
  `review` step bound to a capability; only its execution tool can.
- Plan revisions must increase, and a revision replacing an earlier plan must
  carry a `reason`.
- An objective marked `executed` must cite a step or artifact as evidence.
- Every file path you supply must be a source ref: `upload:<id>`,
  `artifact:<id>`, or `scratch:<relative-path>`.

An unanswered question reported honestly is a better result than a confident
answer the evidence does not support.
"""


__all__ = [
    "ANALYSIS_COVERAGE_SCHEMA",
    "ANALYSIS_OBJECTIVE_SCHEMA",
    "ARTIFACT_ITEM_SCHEMA",
    "CAPABILITY_SEARCH",
    "CATALOG_SEARCH",
    "Dialect",
    "EXECUTE_PLAN",
    "FILE_REQUIREMENT_SCHEMA",
    "INPUT_REQUEST_SCHEMA",
    "INSPECT_SOURCE",
    "MUTATING_TOOLS",
    "PLAN_SCHEMA",
    "PLAN_STEP_SCHEMA",
    "PUBLISH_PLAN",
    "READ_ONLY_TOOLS",
    "REGISTER_ARTIFACTS",
    "REQUEST_INPUTS",
    "RUN_CAPABILITY",
    "RUN_WORKFLOW",
    "SESSION_CONTEXT",
    "SYSTEM_PROMPT",
    "UPDATE_STEP",
    "ToolSpec",
    "anthropic_tools",
    "dynamic_tool_specs",
    "mcp_tools",
    "openai_tools",
    "to_dialect",
    "tool_specs",
]
