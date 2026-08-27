# API reference

Every public symbol, tool, event, and endpoint.

- [Python: plan](#python-plan) · [graph](#python-graph) · [registry](#python-registry)
- [Python: execution](#python-execution) · [session](#python-session) · [events](#python-events)
- [Python: inputs](#python-inputs) · [tools](#python-tools) · [broker](#python-broker) · [agents](#python-agents)
- [Python: server](#python-server) · [errors](#python-errors)
- [Agent tools](#agent-tools)
- [Events](#events)
- [HTTP API](#http-api)
- [TypeScript](#typescript)

---

## Python: plan

```python
from loomcraft import Plan, PlanStep, parse_plan, validate_plan, update_step, get_step, propagate_skips
from loomcraft.plan import ensure_dependencies_succeeded, ensure_step_startable, STEP_TRANSITIONS, AGENT_REPORTABLE_KINDS
```

| Symbol | Signature / value |
| --- | --- |
| `Plan` | Pydantic model: `goal`, `summary`, `revision`, `reason`, `steps` |
| `Plan.adjacency` | `dict[str, list[str]]` |
| `Plan.by_id` | `dict[str, PlanStep]` |
| `Plan.layers` | `list[list[str]]` — each inner list may run concurrently |
| `Plan.step(id)` | `PlanStep`; raises `UnknownStepError` |
| `Plan.ready_steps()` | Pending steps whose dependencies all succeeded |
| `Plan.blocked_steps()` | Pending steps with a failed/skipped upstream |
| `Plan.is_complete` | All steps terminal |
| `Plan.progress` | `{'pending': n, …, 'total': n}` |
| `PlanStep` | `id`, `title`, `kind`, `depends_on`, `capability`, `description`, `status`, `summary`, `execution` |
| `parse_plan(raw)` | `Plan`; raises `PlanValidationError` |
| `validate_plan(raw, current=None, *, registry=None)` | Normalised dict with state reset |
| `update_step(plan, id, status, *, summary=None, execution=None)` | New plan dict |
| `get_step(plan, id)` | `dict` |
| `propagate_skips(plan)` | New plan dict with downstream skipped |
| `ensure_dependencies_succeeded(plan, id)` | Raises `DependencyError` |
| `ensure_step_startable(plan, id, *, kind, capability=None)` | Full precondition check |
| `STEP_TRANSITIONS` | `{status: frozenset(allowed)}` |
| `AGENT_REPORTABLE_KINDS` | `{"answer", "dynamic", "review"}` |
| `MAX_STEPS` / `MAX_REVISION` | `24` / `100` |

---

## Python: graph

```python
from loomcraft import layers, topological_order, find_cycle, is_dag, critical_path, to_dot
from loomcraft.graph import validate, descendants, ancestors, roots, leaves, adjacency_from
```

All take `Mapping[str, Sequence[str]]` (node → dependencies) and import nothing
from LoomCraft — usable standalone.

| Function | Returns |
| --- | --- |
| `validate(adj)` | `list[GraphIssue]` — empty means a valid DAG |
| `is_dag(adj)` | `bool` |
| `find_cycle(adj)` | One cycle as an ordered list, or `[]` |
| `topological_order(adj)` | Stable order; raises `ValueError` on a cycle |
| `layers(adj)` | Dependency levels |
| `descendants(adj, node)` / `ancestors(adj, node)` | `set[str]` |
| `roots(adj)` / `leaves(adj)` | `list[str]` |
| `critical_path(adj, weights=None)` | Longest weighted chain |
| `to_dot(adj, labels=None)` | Graphviz DOT source |
| `adjacency_from(nodes, id_key="id", deps_key="depends_on")` | Build from dict-like nodes |

---

## Python: registry

```python
from loomcraft import Registry, Capability, CapabilityInput, Parameter, Port, Workflow, WorkflowNode, merge_registries
```

### `Registry`

| Method | Purpose |
| --- | --- |
| `register_runner(key, fn, *, replace=False)` | Register an async runner |
| `register_capability(cap, *, replace=False)` | Register a contract |
| `register_workflow(wf, *, replace=False)` | Register a composite DAG |
| `capability_runner(cap, *, replace=False)` | Decorator registering both |
| `capability(id)` / `workflow(id)` / `runner(key)` | Look up; raises on miss |
| `has_capability(id)` / `has_workflow(id)` / `has_runner(key)` | `bool` |
| `capabilities` / `workflows` / `runners` | Copies of the maps |
| `validate()` | `list[str]` of dangling references — call at startup |
| `search(query, *, scope="all", limit=5)` | Ranked contracts |
| `catalog_summary(limit=40)` | Compact overview for `session_context` |

### `Capability`

| Field | Default | Notes |
| --- | --- | --- |
| `id` | — | `^[a-z][a-z0-9_.-]{1,159}$` |
| `name`, `description` | — | The agent reads these |
| `version` | `"1"` | |
| `runner` | — | Registered runner key |
| `inputs` | `()` | `tuple[CapabilityInput, ...]` |
| `input_variants` | `()` | Accepted key combinations |
| `outputs` | `()` | `tuple[Port, ...]` |
| `parameters` | `{}` | `dict[str, Parameter]` |
| `config` | `{}` | Fixed; never model-writable |
| `tags` | `()` | Improves search |
| `timeout_seconds` | `None` | Per attempt |
| `max_attempts` | `1` | 1–10 |
| `retry_backoff_seconds` | `1.0` | Exponential |
| `requires_approval` | `False` | Advisory flag for the UI |

Methods: `validate_inputs(raw)`, `validate_parameters(raw)`, `contract()`,
`effective_variants`.

### `CapabilityInput`

`key` (`^[a-z][a-z0-9_]{0,63}$`), `name`, `description`, `artifact_type`,
`allowed_extensions`, `max_files` (1–12), `port_name`.

### `Parameter`

`type` (`integer|number|boolean|string|object|array`), `description`, `default`,
`minimum`, `maximum`, `enum`. Methods: `json_schema()`, `validate_value(name, value)`.

### `Port`

`name` (`^[A-Za-z][A-Za-z0-9_]{0,63}$`), `artifact_type`, `required`, `description`.

### `Workflow` / `WorkflowNode`

`Workflow`: `id`, `name`, `version`, `description`, `inputs`, `nodes`,
`parameters`, `tags`. `WorkflowNode`: `id`, `name`, `runner`, `depends_on`,
`description`, `inputs`, `outputs`, `config`, plus the same execution-policy
fields as `Capability`.

---

## Python: execution

```python
from loomcraft import Engine, ExecutionGraph, ExecutionNode, Run, NodeState, NodeContext, NodeResult, graph_from_capability, graph_from_workflow
```

### `Engine(registry, session, *, max_parallel=8, emit=None, stream_logs=False)`

| Method | Purpose |
| --- | --- |
| `submit(graph, *, run_id=None)` | Start in the background; returns `Run` |
| `execute(graph, *, run_id=None)` | Submit and await |
| `get(run_id)` | `Run \| None` |
| `cancel(run_id)` / `cancel_all()` | Stop and await quiescence |

### `Run`

`id`, `graph`, `status`, `nodes`, `error`, `cancelled`, `pending_approvals`,
`duration_seconds`, `artifacts`, `failed_nodes`.
Methods: `await wait()`, `await cancel()`, `approve(node_id, approved=True)`,
`to_dict()`.

`status`: `created` → `running` ⇄ `paused_approval` → `succeeded`/`failed`/`cancelled`.

### `NodeContext`

`run_id`, `node_id`, `attempt`, `inputs`, `parameters`, `config`, `workdir`,
`output_ports`, `cancelled`.
Methods: `input(key)`, `input_list(key)`, `optional_input(key)`, `has_input(key)`,
`log(msg, level)`, `progress(fraction, msg)`, `emit(port, filename, data, *, content_type=None)`,
`emit_path(port, path, *, content_type=None)`, `raise_if_cancelled()`, `await wait_cancelled()`.

### `NodeResult`

`NodeResult.ok(**detail)` · `NodeResult.fail(msg, *, retryable=False, **detail)` ·
`NodeResult.retry(msg, **detail)` · `NodeResult.needs_approval(msg, **detail)`.

### Graph builders

```python
graph_from_capability(capability, *, sources, parameters, graph_id=None) -> ExecutionGraph
graph_from_workflow(workflow, *, sources, parameters, graph_id=None) -> ExecutionGraph
```

`ExecutionGraph`: `id`, `name`, `nodes`, `kind`, `source_id`, `adjacency`,
`layers`, `node(id)`. Construction validates the DAG.

---

## Python: session

```python
from loomcraft import Session, SessionStore, ResolvedSource
```

### `SessionStore(root, *, in_memory_events=False, max_sessions=512)`

`create(session_id=None)`, `get(id)`, `get_or_create(id)`, `list_ids()`, `delete(id)`.

### `Session(session_id, root, *, event_log=None, max_upload_bytes=2GiB, max_session_bytes=8GiB)`

| Area | Members |
| --- | --- |
| Layout | `uploads_dir`, `artifacts_dir`, `scratch_dir`, `control_dir`, `run_dir(run_id)` |
| Metadata | `meta()`, `update_meta(**fields)` |
| Uploads | `list_uploads()`, `save_upload(filename, data, *, content_type=None)`, `delete_upload(id)`, `total_upload_bytes()` |
| Plans | `current_plan()`, `plan_history()`, `publish_plan(plan)`, `update_current_plan(plan)` |
| Executions | `list_executions()`, `record_execution(execution)` |
| Artifacts | `list_artifacts()`, `get_artifact(id)`, `add_artifact(source, **kwargs)`, `register_scratch_artifacts(entries, *, step_id=None)` |
| Sources | `resolve_source(source_ref) -> ResolvedSource` |
| Events | `events`, `emit(event, data)` |
| UI | `history(*, after_seq=0)` |
| Lifecycle | `delete()` |

`ResolvedSource`: `source_ref`, `kind`, `path`, `filename`, `size`, `checksum`,
`content_type`.

---

## Python: events

```python
from loomcraft import Event, EventLog, MemoryEventLog, EVENT_TYPES
```

`EventLog(path)`: `append(event, data=None)`, `read(*, after_seq=0)`,
`iter_events(*, after_seq=0)`, `last_seq`, `subscribe(cb) -> unsubscribe`,
`verify()`.

`MemoryEventLog()` — same API, no filesystem, no hash chain.

`Event`: `seq`, `event`, `data`, `ts`. Methods: `to_dict()`, `sse()`,
`Event.from_dict(row)`.

---

## Python: inputs

```python
from loomcraft import FileInputRequest, FileRequirement, validate_input_request, allocate_uploads, validate_fulfillment
from loomcraft.inputs import pending_requests, requests_using_upload
```

| Function | Purpose |
| --- | --- |
| `validate_input_request(raw)` | Validate and stamp a server-owned `request_id` |
| `allocate_uploads(request, uploads)` | `{requirement_key: [upload_id]}` |
| `validate_fulfillment(request, uploads)` | Allocate; raise if a required slot is unmet |
| `pending_requests(events)` | Replay the log for unresolved requests |
| `requests_using_upload(events, upload_id)` | Requests a deleted file satisfied |

`FileRequirement`: `key`, `label`, `description`, `required`, `min_files`,
`max_files`, `allowed_extensions`, `field_hints`.

---

## Python: tools

```python
from loomcraft import tool_specs, to_dialect, anthropic_tools, openai_tools, mcp_tools, ToolSpec, SYSTEM_PROMPT
```

`tool_specs(*, include_workflows=True, include_inspection=True, max_search_results=10)`
→ `list[ToolSpec]`.

`to_dialect(specs, dialect)` where dialect is `canonical | anthropic | openai |
openai_responses | mcp`.

`ToolSpec`: `name`, `description`, `parameters`; `to_anthropic()`, `to_openai()`,
`to_openai_responses()`, `to_mcp()`, `to_dict()`.

Constants: `loomcraft.tools.READ_ONLY_TOOLS`, `MUTATING_TOOLS`, `PLAN_SCHEMA`,
`PLAN_STEP_SCHEMA`, `INPUT_REQUEST_SCHEMA`, and one per tool name.

---

## Python: broker

```python
from loomcraft import ToolBroker, BrokerLimits, ToolResponse
```

### `ToolBroker(session, registry, *, engine=None, limits=None, on_event=None)`

| Method | Purpose |
| --- | --- |
| `await dispatch(name, payload)` | Validate and execute; returns `ToolResponse` |
| `begin_turn()` | Reset per-turn budgets |
| `awaiting_inputs` | Blocked on a file request |
| `active_run` | `Run \| None` |
| `await close()` | Cancel anything in flight |
| `fulfill_input_request(request_id)` | Confirm the user's uploads |
| `cancel_input_request(request_id)` | Decline a request |
| `invalidate_requests_for_upload(upload_id)` | Re-open affected requests |

`BrokerLimits`: `max_actions_per_turn=64`, `max_identical_actions=3`,
`max_inspect_bytes=16384`, `max_inspect_lines=40`, `search_limit=10`.

`ToolResponse`: `ok`, `result`, `error`, `error_code`; `to_dict()`,
`to_tool_result_text()`.

---

## Python: agents

```python
from loomcraft import AnthropicAgent, OpenAICompatibleAgent, ScriptedAgent, TurnResult, ToolCall, execute_tool_calls
```

### `AnthropicAgent(*, client=None, model="claude-opus-5", system=SYSTEM_PROMPT, max_tokens=16000, effort="high", thinking=None, max_iterations=24, tools=None, extra_body=None)`

`await run_turn(broker, message, *, history=None, on_event=None) -> TurnResult`

### `OpenAICompatibleAgent(client, *, model, system=SYSTEM_PROMPT, max_iterations=24, tools=None, extra_body=None)`

### `ScriptedAgent(script, *, final_text="")`

`script` is a list of `(tool_name, arguments)` pairs, or a callable receiving the
responses so far and returning the next batch.

### `TurnResult`

`text`, `stop_reason`, `iterations`, `tool_calls`, `tool_results`, `messages`,
`usage`, `error`, `ok`.

### `execute_tool_calls(broker, calls, *, on_event=None)`

Runs a batch concurrently and emits `tool_call`/`tool_result` events.

---

## Python: server

```python
from loomcraft.server import create_app, create_router, TurnManager, FASTAPI_AVAILABLE
```

```python
create_app(store, registry, agent_factory, *, title="LoomCraft",
           prefix="/api/v1/loomcraft", limits=None, cors_origins=None)

create_router(store, registry, agent_factory, *, prefix=..., limits=None, manager=None)
```

`agent_factory(session) -> Agent` is called per turn, so model, effort, or prompt
can vary per task.

`TurnManager`: `is_busy(sid)`, `broker(sid)`, `start(...)`, `await cancel(sid)`,
`await shutdown()`.

---

## Python: errors

All derive from `LoomCraftError` with `.code` and `.public_message`.

| Exception | `.code` |
| --- | --- |
| `PlanValidationError` | `PLAN_INVALID` |
| `StepTransitionError` | `STEP_TRANSITION_INVALID` |
| `UnknownStepError` | `STEP_UNKNOWN` |
| `DependencyError` | `STEP_DEPENDENCIES_INCOMPLETE` |
| `RegistryError` | `REGISTRY_ERROR` |
| `UnknownCapabilityError` | `CAPABILITY_UNKNOWN` |
| `UnknownWorkflowError` | `WORKFLOW_UNKNOWN` |
| `UnknownRunnerError` | `RUNNER_UNKNOWN` |
| `ContractError` | `CAPABILITY_CONTRACT_VIOLATION` |
| `ExecutionError` | `EXECUTION_FAILED` |
| `GraphStalledError` | `GRAPH_STALLED` |
| `SourceError` | `SOURCE_INVALID` |
| `SourceIntegrityError` | `SOURCE_INTEGRITY_FAILED` |
| `ArtifactError` | `ARTIFACT_ERROR` |
| `InputRequestError` | `INPUT_REQUEST_INVALID` |
| `InputFulfillmentError` | `INPUT_REQUEST_UNFULFILLED` |
| `EventLogError` | `EVENT_LOG_CORRUPT` |
| `ActionBudgetError` | `BROKER_ACTION_LIMIT_EXCEEDED` |
| `RepeatedActionError` | `BROKER_ACTION_REPEATED` |
| `AwaitingInputsError` | `BROKER_AWAITING_INPUTS` |
| `ExecutionBusyError` | `BROKER_EXECUTION_BUSY` |
| `UnsupportedActionError` | `BROKER_ACTION_UNSUPPORTED` |

---

## Agent tools

| Tool | Required | Optional |
| --- | --- | --- |
| `session_context` | — | — |
| `capability_search` | `query` | `limit` |
| `catalog_search` | `query` | `scope`, `limit` |
| `inspect_source` | `source_ref` | `max_bytes`, `max_lines` |
| `publish_plan` | `plan` | — |
| `update_step` | `step_id`, `status` | `summary` |
| `request_inputs` | `request` | — |
| `run_capability` | `capability_id`, `step_id`, `inputs` | `parameters` |
| `run_workflow` | `workflow_id`, `step_id`, `inputs` | `parameters` |
| `register_artifacts` | `step_id`, `artifacts` | — |

Every response: `{ok, result?, error?, error_code?}`.

---

## Events

| Event | `data` |
| --- | --- |
| `plan_published` | `{plan}` |
| `step_updated` | `{revision, step}` |
| `execution_started` | `{step_id, execution_kind, execution_id, capability, nodes}` |
| `execution_progress` | `{execution_id, node_id, status, attempt?, max_attempts?, fraction?, message?, error?, retry_in_seconds?, duration_seconds?}` |
| `execution_finished` | `{step_id, execution}` |
| `node_log` | `{execution_id, node_id, level, message}` |
| `artifact_registered` | `{artifact}` |
| `input_required` | `{request}` |
| `input_fulfilled` | `{request_id, allocated}` |
| `input_cancelled` | `{request_id}` |
| `input_invalidated` | `{request_id, upload_id}` |
| `approval_required` | `{execution_id, nodes}` |
| `approval_resolved` | `{execution_id, node_id, approved}` |
| `tool_call` * | `{item_id, tool, step_id?}` |
| `tool_result` * | `{item_id, tool, ok, exit_code, error?, error_code?}` |
| `message` / `message_delta` * | `{item_id, text}` / `{item_id, delta}` |
| `notice` / `error` | `{message}` |
| `done` | `{ok}` |

`*` stream-only — not persisted, not replayable, `seq: -1`.

---

## HTTP API

Default prefix `/api/v1/loomcraft`.

| Method | Path | Body / query | Returns |
| --- | --- | --- | --- |
| `POST` | `/sessions` | — | Session metadata |
| `GET` | `/sessions` | — | `{sessions: [id]}` |
| `GET` | `/sessions/{id}` | — | Session metadata |
| `GET` | `/sessions/{id}/history` | `?after_seq=` | Full state snapshot |
| `DELETE` | `/sessions/{id}` | — | `{deleted}` |
| `GET` | `/catalog` | — | `{capabilities, workflows}` |
| `POST` | `/sessions/{id}/uploads` | multipart `file` | Upload record |
| `DELETE` | `/sessions/{id}/uploads/{uid}` | — | `{deleted, invalidated_request_ids}` |
| `POST` | `/sessions/{id}/turn` | `{message}` | `text/event-stream` |
| `GET` | `/sessions/{id}/events` | `?after_seq=` | `text/event-stream` |
| `POST` | `/sessions/{id}/cancel` | — | `{cancelled}` |
| `POST` | `/sessions/{id}/input-requests/{rid}/fulfill` | — | `{request_id, allocated}` |
| `POST` | `/sessions/{id}/input-requests/{rid}/cancel` | — | `{request_id}` |
| `POST` | `/sessions/{id}/executions/{rid}/approve` | `{node_id, approved}` | Confirmation |
| `GET` | `/sessions/{id}/artifacts` | — | `{artifacts}` |
| `GET` | `/sessions/{id}/artifacts/{aid}` | — | File download |
| `GET` | `/healthz` | — | `{ok, problems, capabilities, workflows}` |

---

## TypeScript

```ts
import {
  // state
  initialLoomState, reduceLoomEvent, hydrateLoomState, appendUserMessage,
  deriveTaskPhase, planProgress, readySteps, orientationActivities,
  pendingInputRequests, parsePlan, parseInputRequest,
  // layout
  layoutPlan, assignLayers, fitToViewport, countCrossings,
  // transport
  LoomClient, LoomHttpError, LoomProtocolError,
  // react
  useLoomSession, LoomWorkbench, PlanGraph, StepDetail,
  PlanProgress, Timeline, ArtifactList, InputRequestPanel,
  ApprovalPanel, ExecutionList, OrientationPanel, formatBytes,
} from "@loomcraft/renderer";
import "@loomcraft/renderer/styles.css";
```

### State

| Function | Signature |
| --- | --- |
| `reduceLoomEvent` | `(state, event) => LoomState` — pure, never throws |
| `hydrateLoomState` | `(history) => LoomState` |
| `appendUserMessage` | `(state, message) => LoomState` |
| `deriveTaskPhase` | `(state, busy) => TaskPhase` |
| `planProgress` | `(plan) => {total, succeeded, failed, running, pending, skipped, fraction}` |
| `readySteps` | `(plan) => PlanStep[]` |
| `orientationActivities` | `(timeline, limit?) => OrientationActivity[]` |
| `pendingInputRequests` | `(state) => InputRequest[]` |

### Layout

`layoutPlan(plan, options?) => Layout` with `{nodes, edges, width, height, layers}`.
Options: `nodeWidth`, `nodeHeight`, `columnGap`, `rowGap`, `padding`,
`direction`, `sweeps`.
Also `assignLayers(steps)`, `fitToViewport(layout, viewport, options?)`,
`countCrossings(layout)`.

### `LoomClient(options?)`

Options: `baseUrl`, `headers`, `fetchImpl`.

Methods: `createSession`, `listSessions`, `getHistory`, `deleteSession`,
`getCatalog`, `uploadFile`, `deleteUpload`, `fulfillInputRequest`,
`cancelInputRequest`, `approveNode`, `listArtifacts`, `artifactUrl`,
`downloadArtifact`, `cancelTurn`, `runTurn`, `streamEvents`.

`runTurn` returns `{state: "terminal", ok} | {state: "detached"} | {state: "aborted"}`.

### `useLoomSession(options)`

Options: `sessionId`, `baseUrl`, `headers`, `hydrate`, `reattachOnDetach`, `client`.

Returns: `state`, `busy`, `phase`, `visiblePlan`, `selectedRevision`,
`selectRevision`, `selectedStepId`, `selectStep`, `orientation`,
`pendingRequests`, `error`, `send`, `cancel`, `upload`, `deleteUpload`,
`fulfillRequest`, `cancelRequest`, `approve`, `download`, `refresh`, `client`.

### CSS custom properties

| Group | Tokens |
| --- | --- |
| Chrome surfaces | `--lc-canvas`, `--lc-surface`, `--lc-sunken`, `--lc-hairline`, `--lc-line` |
| Graph pane | `--lc-graph-canvas`, `--lc-graph-dot` |
| Text | `--lc-ink`, `--lc-ink-2`, `--lc-ink-3` |
| Status | `--lc-accent`, `--lc-accent-wash`, `--lc-run`, `--lc-run-wash`, `--lc-ok`, `--lc-ok-wash`, `--lc-warn`, `--lc-warn-wash`, `--lc-err`, `--lc-err-wash`, `--lc-idle` |
| Edges | `--lc-edge`, `--lc-edge-active`, `--lc-edge-done` |
| Shape | `--lc-radius`, `--lc-radius-sm`, `--lc-shadow`, `--lc-font`, `--lc-mono` |

Two splits in that list are deliberate rather than incidental:

- **Graph pane vs. chrome.** The default theme puts warm paper behind the
  chrome and a cool grey behind the DAG, so the canvas reads as a surface set
  into the page rather than another sheet of the same paper. Set
  `--lc-graph-canvas` to `var(--lc-canvas)` if you want them flush.
- **Edges vs. status.** A stroke is one or two pixels wide and has to keep its
  meaning at 40% zoom, so `--lc-edge-active` / `--lc-edge-done` run more
  saturated than the `--lc-run` / `--lc-ok` used for node chrome. Point them at
  the status tokens if you would rather have exactly one green.

Node fills are derived, not tokens: each status fill is `color-mix` of its
status colour at 2.5–3.5% over `--lc-surface`. The border and the status dot
carry the state; a saturated fill per status turns a twenty-node canvas into a
patchwork and makes the titles harder to scan.
