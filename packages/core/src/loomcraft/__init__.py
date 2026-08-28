"""LoomCraft — an AI-native DAG planning and execution engine.

An agent proposes a versioned task DAG through a validated tool surface; the
server checks it, executes the parts it is authorised to execute, streams every
state change as an event, and the renderer draws it.

Typical wiring::

    from loomcraft import Registry, Capability, Session, ToolBroker, AnthropicAgent

    registry = Registry()

    @registry.capability_runner(Capability(
        id="csv.profile",
        name="Profile a CSV",
        description="Column types, null counts, and basic statistics.",
        runner="csv.profile",
        inputs=(CapabilityInput(key="table", name="Table", description="CSV",
                                allowed_extensions=(".csv",)),),
        outputs=(Port(name="profile", artifact_type="json"),),
    ))
    async def profile(ctx: NodeContext) -> NodeResult:
        ...

    session = SessionStore("./data").create()
    broker = ToolBroker(session, registry)
    await AnthropicAgent().run_turn(broker, "Profile the uploaded table.")

See ``docs/`` for the full guide.
"""

from __future__ import annotations

__version__ = "0.1.0"

from .agent import (
    Agent,
    AnthropicAgent,
    OpenAICompatibleAgent,
    ScriptedAgent,
    SubprocessAgent,
    ToolCall,
    TurnResult,
    execute_tool_calls,
)
from .broker import BrokerLimits, ToolBroker, ToolResponse
from .context import (
    EmittedArtifact,
    InputFile,
    NodeContext,
    NodeResult,
    Runner,
    RunnerFn,
)
from .engine import (
    Engine,
    ExecutionGraph,
    ExecutionNode,
    NodeState,
    Run,
    graph_from_capability,
    graph_from_workflow,
)
from .errors import (
    ArtifactError,
    BrokerError,
    ContractError,
    DependencyError,
    EventLogError,
    ExecutionError,
    InputRequestError,
    InvalidArgumentError,
    LoomCraftError,
    PlanValidationError,
    RegistryError,
    SourceError,
    SourceIntegrityError,
    StepTransitionError,
    UnknownCapabilityError,
    UnknownStepError,
    UnknownWorkflowError,
)
from .events import EVENT_TYPES, Event, EventLog, MemoryEventLog
from .graph import (
    critical_path,
    find_cycle,
    is_dag,
    layers,
    topological_order,
    to_dot,
)
from .inputs import (
    FileInputRequest,
    FileRequirement,
    allocate_uploads,
    validate_fulfillment,
    validate_input_request,
)
from .plan import (
    AGENT_REPORTABLE_KINDS,
    FAILURE_POLICIES,
    STEP_TRANSITIONS,
    AnalysisCoverage,
    AnalysisObjective,
    Plan,
    PlanStep,
    RetryPolicy,
    StepKind,
    StepStatus,
    diff_plans,
    get_step,
    parse_plan,
    propagate_skips,
    task_phase,
    update_step,
    validate_plan,
)
from .plan_executor import PlanExecutor, build_plan_graph
from .protocol import AppServerBridge, ProtocolError
from .registry import (
    Capability,
    CapabilityInput,
    Parameter,
    Port,
    Registry,
    Workflow,
    WorkflowNode,
    merge_registries,
    source_ref_of,
)
from .store import (
    ResolvedSource,
    Session,
    SessionStore,
    public_artifact,
    public_execution,
    public_plan,
)
from .tools import (
    SYSTEM_PROMPT,
    ToolSpec,
    anthropic_tools,
    dynamic_tool_specs,
    mcp_tools,
    openai_tools,
    to_dialect,
    tool_specs,
)

__all__ = [
    "__version__",
    # Plan
    "AGENT_REPORTABLE_KINDS",
    "FAILURE_POLICIES",
    "AnalysisCoverage",
    "AnalysisObjective",
    "Plan",
    "PlanStep",
    "RetryPolicy",
    "STEP_TRANSITIONS",
    "StepKind",
    "StepStatus",
    "diff_plans",
    "get_step",
    "parse_plan",
    "propagate_skips",
    "task_phase",
    "update_step",
    "validate_plan",
    # Graph
    "critical_path",
    "find_cycle",
    "is_dag",
    "layers",
    "to_dot",
    "topological_order",
    # Registry
    "Capability",
    "CapabilityInput",
    "Parameter",
    "Port",
    "Registry",
    "Workflow",
    "WorkflowNode",
    "merge_registries",
    "source_ref_of",
    # Execution
    "Engine",
    "ExecutionGraph",
    "ExecutionNode",
    "NodeContext",
    "NodeResult",
    "NodeState",
    "PlanExecutor",
    "Run",
    "Runner",
    "RunnerFn",
    "EmittedArtifact",
    "InputFile",
    "build_plan_graph",
    "graph_from_capability",
    "graph_from_workflow",
    # Session / events
    "EVENT_TYPES",
    "Event",
    "EventLog",
    "MemoryEventLog",
    "ResolvedSource",
    "Session",
    "SessionStore",
    "public_artifact",
    "public_execution",
    "public_plan",
    # Inputs
    "FileInputRequest",
    "FileRequirement",
    "allocate_uploads",
    "validate_fulfillment",
    "validate_input_request",
    # Agent surface
    "Agent",
    "AnthropicAgent",
    "AppServerBridge",
    "BrokerLimits",
    "OpenAICompatibleAgent",
    "SYSTEM_PROMPT",
    "ScriptedAgent",
    "SubprocessAgent",
    "ToolBroker",
    "ToolCall",
    "ToolResponse",
    "ToolSpec",
    "TurnResult",
    "anthropic_tools",
    "dynamic_tool_specs",
    "execute_tool_calls",
    "mcp_tools",
    "openai_tools",
    "to_dialect",
    "tool_specs",
    # Errors
    "ArtifactError",
    "BrokerError",
    "ContractError",
    "DependencyError",
    "EventLogError",
    "ExecutionError",
    "InputRequestError",
    "InvalidArgumentError",
    "LoomCraftError",
    "PlanValidationError",
    "ProtocolError",
    "RegistryError",
    "SourceError",
    "SourceIntegrityError",
    "StepTransitionError",
    "UnknownCapabilityError",
    "UnknownStepError",
    "UnknownWorkflowError",
]
