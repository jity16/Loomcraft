/**
 * The event reducer.
 *
 * One pure function folds the server's event stream into renderable state, and
 * the *same* function replays a persisted history on page load. That is the
 * whole reason live updates and refresh-mid-run agree: there is no second code
 * path that could drift.
 *
 * Everything here is defensive by design. Events arrive from a server that may
 * be a version ahead; unknown event names are ignored, malformed payloads are
 * coerced or dropped, and no reducer branch can throw.
 */

import type {
  Artifact,
  Execution,
  History,
  InputRequest,
  LoomEvent,
  LoomState,
  NodeProgress,
  OrientationActivity,
  Plan,
  PlanStep,
  StepKind,
  StepStatus,
  TaskPhase,
  TimelineItem,
  Upload,
} from "./types";

export const initialLoomState: LoomState = {
  plans: [],
  currentPlan: null,
  timeline: [],
  executions: [],
  artifacts: [],
  uploads: [],
  fulfilledInputRequestIds: [],
  cancelledInputRequestIds: [],
  pendingApprovals: {},
  lastSeq: 0,
  error: null,
  done: false,
};

const STEP_KINDS: StepKind[] = ["answer", "capability", "workflow", "dynamic", "review"];
const STEP_STATUSES: StepStatus[] = [
  "pending",
  "ready",
  "running",
  "waiting_approval",
  "succeeded",
  "failed",
  "skipped",
  "cancelled",
];

// ── Coercion helpers ────────────────────────────────────────────────────────

function record(value: unknown): Record<string, unknown> {
  return value !== null && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : {};
}

function text(value: unknown): string {
  return typeof value === "string" ? value : "";
}

function num(value: unknown): number | undefined {
  return typeof value === "number" && Number.isFinite(value) ? value : undefined;
}

function stringList(value: unknown): string[] {
  return Array.isArray(value) ? value.filter((item): item is string => typeof item === "string") : [];
}

// ── Payload parsers ─────────────────────────────────────────────────────────

export function parsePlan(value: unknown): Plan | null {
  const row = record(value);
  if (typeof row.revision !== "number" || !Array.isArray(row.steps)) return null;

  const steps: PlanStep[] = [];
  for (const entry of row.steps) {
    const step = record(entry);
    const kind = text(step.kind) as StepKind;
    if (!STEP_KINDS.includes(kind)) return null;
    const status = text(step.status) as StepStatus;
    steps.push({
      id: text(step.id),
      title: text(step.title),
      kind,
      depends_on: stringList(step.depends_on),
      capability: typeof step.capability === "string" ? step.capability : null,
      description: text(step.description),
      status: STEP_STATUSES.includes(status) ? status : "pending",
      summary: typeof step.summary === "string" ? step.summary : null,
      execution: Object.keys(record(step.execution)).length ? record(step.execution) : null,
      attempts: num(step.attempts),
      retry: Object.keys(record(step.retry)).length
        ? (record(step.retry) as PlanStep["retry"])
        : undefined,
      timeout_seconds: num(step.timeout_seconds) ?? null,
      on_failure:
        step.on_failure === "continue" || step.on_failure === "require_approval"
          ? step.on_failure
          : "stop",
      metadata: Object.keys(record(step.metadata)).length
        ? record(step.metadata)
        : undefined,
    });
  }
  if (!steps.length) return null;

  return {
    goal: text(row.goal),
    summary: text(row.summary),
    revision: row.revision,
    reason: typeof row.reason === "string" ? row.reason : null,
    steps,
    analysis_profile:
      typeof row.analysis_profile === "string" ? row.analysis_profile : null,
    objectives: Array.isArray(row.objectives)
      ? (row.objectives as Plan["objectives"])
      : [],
    analysis_coverage: Array.isArray(row.analysis_coverage)
      ? (row.analysis_coverage as Plan["analysis_coverage"])
      : [],
  };
}

function parseArtifact(value: unknown): Artifact | null {
  const row = record(value);
  const id = text(row.id);
  if (!id) return null;
  return {
    id,
    filename: text(row.filename),
    display_name: text(row.display_name) || undefined,
    size: num(row.size) ?? 0,
    checksum: text(row.checksum) || undefined,
    content_type: text(row.content_type) || undefined,
    source_ref: text(row.source_ref) || undefined,
    port_name: typeof row.port_name === "string" ? row.port_name : null,
    step_id: typeof row.step_id === "string" ? row.step_id : null,
    run_id: typeof row.run_id === "string" ? row.run_id : null,
    download_url: text(row.download_url) || undefined,
  };
}

function parseArtifacts(value: unknown): Artifact[] {
  if (!Array.isArray(value)) return [];
  return value.map(parseArtifact).filter((item): item is Artifact => item !== null);
}

function parseUpload(value: unknown): Upload | null {
  const row = record(value);
  const id = text(row.id);
  if (!id) return null;
  return {
    id,
    filename: text(row.filename),
    size: num(row.size) ?? 0,
    checksum: text(row.checksum) || undefined,
    content_type: text(row.content_type) || undefined,
    source_ref: text(row.source_ref) || `upload:${id}`,
    created_at: text(row.created_at) || undefined,
  };
}

export function parseInputRequest(value: unknown): InputRequest | null {
  const row = record(value);
  const requestId = text(row.request_id);
  const rawRequirements = Array.isArray(row.requirements) ? row.requirements : [];
  const requirements = rawRequirements.map((entry) => {
    const requirement = record(entry);
    return {
      key: text(requirement.key),
      label: text(requirement.label),
      description: text(requirement.description),
      required: requirement.required !== false,
      min_files: num(requirement.min_files) ?? 1,
      max_files: num(requirement.max_files) ?? 1,
      allowed_extensions: stringList(requirement.allowed_extensions),
      field_hints: stringList(requirement.field_hints),
    };
  });
  if (!requestId || !requirements.length) return null;
  return {
    request_id: requestId,
    title: text(row.title) || "Additional files needed",
    message: text(row.message),
    requirements,
    continue_prompt:
      text(row.continue_prompt) || "The requested files are uploaded. Please continue.",
  };
}

function parseExecution(
  value: unknown,
  fallback: Record<string, unknown> = {},
): Execution | null {
  const row = { ...fallback, ...record(value) };
  const kind = text(row.kind || row.execution_kind);
  if (kind !== "capability" && kind !== "workflow" && kind !== "plan") return null;

  // A run's serialized form carries its per-node state. Reading it here is what
  // lets a page reload show a plan run mid-flight rather than as a blank box.
  const nodes: Record<string, NodeProgress> = {};
  for (const [nodeId, entry] of Object.entries(record(row.nodes))) {
    if (typeof entry === "string") {
      nodes[nodeId] = { node_id: nodeId, status: entry };
      continue;
    }
    const node = record(entry);
    nodes[nodeId] = {
      node_id: text(node.node_id) || nodeId,
      status: text(node.status) || "pending",
      attempt: num(node.attempt) ?? num(node.attempts),
      max_attempts: num(node.max_attempts),
      fraction: num(node.fraction),
      message: text(node.message) || undefined,
      error: typeof node.error === "string" ? node.error : null,
      retry_in_seconds: num(node.retry_in_seconds),
      duration_seconds: num(node.duration_seconds),
    };
  }

  return {
    id:
      typeof row.id === "string"
        ? row.id
        : typeof row.execution_id === "string"
          ? row.execution_id
          : null,
    kind,
    capability: text(row.capability),
    status: text(row.status) || "running",
    step_id: text(row.step_id) || undefined,
    duration_seconds: num(row.duration_seconds),
    error: typeof row.error === "string" ? row.error : null,
    failed_nodes: Array.isArray(row.failed_nodes)
      ? row.failed_nodes.filter(
          (item): item is Record<string, unknown> => Object.keys(record(item)).length > 0,
        )
      : [],
    artifacts: parseArtifacts(row.artifacts),
    nodes: Object.keys(nodes).length ? nodes : undefined,
    revision: num(row.revision),
    attempts: num(row.attempts),
  };
}

// ── State transforms ────────────────────────────────────────────────────────

function withPlan(state: LoomState, plan: Plan): LoomState {
  const exists = state.plans.some((item) => item.revision === plan.revision);
  // History replay can deliver an older revision after a newer one. Recording
  // it in `plans` is right; making it current again is not — the switcher would
  // jump backwards under the reader.
  const isCurrent = state.currentPlan === null || plan.revision >= state.currentPlan.revision;
  return {
    ...state,
    currentPlan: isCurrent ? plan : state.currentPlan,
    plans: exists
      ? state.plans.map((item) => (item.revision === plan.revision ? plan : item))
      : [...state.plans, plan].sort((a, b) => a.revision - b.revision),
  };
}

function upsertExecution(state: LoomState, execution: Execution): LoomState {
  const index = state.executions.findIndex(
    (item) => item.id === execution.id && item.kind === execution.kind,
  );
  const executions = [...state.executions];
  if (index >= 0) {
    // Merge field-by-field so a partial update (a progress ping with no
    // artifacts) cannot blank fields an earlier event already established.
    const merged = { ...executions[index] } as unknown as Record<string, unknown>;
    for (const [key, value] of Object.entries(execution)) {
      if (value !== undefined && !(Array.isArray(value) && value.length === 0)) {
        merged[key] = value;
      }
    }
    executions[index] = merged as unknown as Execution;
  } else {
    executions.push(execution);
  }
  return { ...state, executions };
}

function withExecutionTimeline(
  state: LoomState,
  execution: Execution,
  at?: string,
): LoomState {
  const identity = execution.step_id || execution.id || execution.capability;
  const id = `execution-${execution.kind}-${identity}`;
  if (state.timeline.some((item) => item.kind === "execution" && item.id === id)) {
    return state;
  }
  return {
    ...state,
    timeline: [
      ...state.timeline,
      {
        kind: "execution",
        id,
        executionKind: execution.kind,
        executionId: execution.id,
        stepId: execution.step_id,
        at,
      },
    ],
  };
}

function mergeArtifacts(existing: Artifact[], incoming: Artifact[]): Artifact[] {
  const merged = [...existing];
  for (const artifact of incoming) {
    if (!merged.some((item) => item.id === artifact.id)) merged.push(artifact);
  }
  return merged;
}

// ── Public API ──────────────────────────────────────────────────────────────

export function appendUserMessage(state: LoomState, message: string): LoomState {
  return {
    ...state,
    done: false,
    error: null,
    timeline: [
      ...state.timeline,
      {
        kind: "user",
        id: `user-${state.timeline.length + 1}`,
        text: message,
        at: new Date().toISOString(),
      },
    ],
  };
}

/** Fold one event into state. Pure; never throws on malformed input. */
export function reduceLoomEvent(state: LoomState, incoming: LoomEvent): LoomState {
  // An SSE reconnect replays the backlog, which can overlap what the live
  // stream already delivered. Sequence numbers are the server's total order,
  // so anything at or below the watermark has already been folded in.
  if (
    typeof incoming.seq === "number" &&
    incoming.seq > 0 &&
    incoming.seq <= state.lastSeq
  ) {
    return state;
  }
  const data = record(incoming.data);
  const at = incoming.ts;
  const seq = typeof incoming.seq === "number" && incoming.seq > 0 ? incoming.seq : state.lastSeq;
  const base: LoomState = { ...state, lastSeq: Math.max(state.lastSeq, seq) };

  switch (incoming.event) {
    case "plan_published": {
      const plan = parsePlan(data.plan);
      return plan ? withPlan(base, plan) : base;
    }

    case "step_updated": {
      const revision = num(data.revision) ?? base.currentPlan?.revision;
      const step = record(data.step);
      const stepId = text(step.id);
      const target =
        base.plans.find((item) => item.revision === revision) ?? base.currentPlan;
      if (!target || !stepId) return base;
      const updated = parsePlan({
        ...target,
        steps: target.steps.map((item) => (item.id === stepId ? { ...item, ...step } : item)),
      });
      return updated ? withPlan(base, updated) : base;
    }

    case "execution_started": {
      const execution = parseExecution(data);
      if (!execution) return base;
      return withExecutionTimeline(upsertExecution(base, execution), execution, at);
    }

    case "execution_progress": {
      const executionId = text(data.execution_id);
      const nodeId = text(data.node_id);
      if (!executionId || !nodeId) return base;
      const index = base.executions.findIndex((item) => item.id === executionId);
      if (index < 0) return base;
      const progress: NodeProgress = {
        node_id: nodeId,
        status: text(data.status) || "running",
        attempt: num(data.attempt),
        max_attempts: num(data.max_attempts),
        fraction: num(data.fraction),
        message: text(data.message) || undefined,
        error: typeof data.error === "string" ? data.error : null,
        retry_in_seconds: num(data.retry_in_seconds),
        duration_seconds: num(data.duration_seconds),
      };
      const executions = [...base.executions];
      executions[index] = {
        ...executions[index],
        nodes: { ...(executions[index].nodes ?? {}), [nodeId]: progress },
      };
      return { ...base, executions };
    }

    case "execution_finished": {
      const execution = parseExecution(data.execution, { step_id: data.step_id });
      if (!execution) return base;
      const next = withExecutionTimeline(
        upsertExecution(base, execution),
        execution,
        at,
      );
      return { ...next, artifacts: mergeArtifacts(next.artifacts, execution.artifacts) };
    }

    case "artifact_registered": {
      const artifact = parseArtifact(data.artifact);
      if (!artifact || base.artifacts.some((item) => item.id === artifact.id)) return base;
      return { ...base, artifacts: [...base.artifacts, artifact] };
    }

    case "input_required": {
      const request = parseInputRequest(data.request ?? data);
      if (!request) return base;
      const id = `input-request-${request.request_id}`;
      const index = base.timeline.findIndex(
        (item) => item.kind === "input_request" && item.id === id,
      );
      const entry: TimelineItem = { kind: "input_request", id, request, at };
      return {
        ...base,
        timeline:
          index >= 0
            ? base.timeline.map((item, position) => (position === index ? entry : item))
            : [...base.timeline, entry],
      };
    }

    case "input_fulfilled": {
      const requestId = text(data.request_id);
      if (!requestId) return base;
      return {
        ...base,
        fulfilledInputRequestIds: base.fulfilledInputRequestIds.includes(requestId)
          ? base.fulfilledInputRequestIds
          : [...base.fulfilledInputRequestIds, requestId],
        cancelledInputRequestIds: base.cancelledInputRequestIds.filter(
          (id) => id !== requestId,
        ),
      };
    }

    case "input_cancelled": {
      const requestId = text(data.request_id);
      if (!requestId) return base;
      return {
        ...base,
        cancelledInputRequestIds: base.cancelledInputRequestIds.includes(requestId)
          ? base.cancelledInputRequestIds
          : [...base.cancelledInputRequestIds, requestId],
        fulfilledInputRequestIds: base.fulfilledInputRequestIds.filter(
          (id) => id !== requestId,
        ),
      };
    }

    case "input_invalidated": {
      // A file the user already supplied went away: re-open the request.
      const requestId = text(data.request_id);
      if (!requestId) return base;
      return {
        ...base,
        fulfilledInputRequestIds: base.fulfilledInputRequestIds.filter(
          (id) => id !== requestId,
        ),
        cancelledInputRequestIds: base.cancelledInputRequestIds.filter(
          (id) => id !== requestId,
        ),
      };
    }

    case "approval_required": {
      const executionId = text(data.execution_id);
      const nodes = stringList(data.nodes);
      if (!executionId || !nodes.length) return base;
      const id = `approval-${executionId}`;
      const entry: TimelineItem = { kind: "approval", id, executionId, nodes, at };
      const index = base.timeline.findIndex((item) => item.id === id);
      return {
        ...base,
        pendingApprovals: { ...base.pendingApprovals, [executionId]: nodes },
        timeline:
          index >= 0
            ? base.timeline.map((item, position) => (position === index ? entry : item))
            : [...base.timeline, entry],
      };
    }

    case "approval_resolved": {
      const executionId = text(data.execution_id);
      const nodeId = text(data.node_id);
      if (!executionId) return base;
      const remaining = (base.pendingApprovals[executionId] ?? []).filter(
        (item) => item !== nodeId,
      );
      const pendingApprovals = { ...base.pendingApprovals };
      if (remaining.length) pendingApprovals[executionId] = remaining;
      else delete pendingApprovals[executionId];
      return {
        ...base,
        pendingApprovals,
        timeline: base.timeline.map((item) =>
          item.kind === "approval" && item.executionId === executionId
            ? { ...item, resolved: { node: nodeId, approved: data.approved === true } }
            : item,
        ),
      };
    }

    case "tool_call": {
      const id = text(data.item_id) || `tool-${base.timeline.length + 1}`;
      if (base.timeline.some((item) => item.kind === "tool" && item.id === id)) return base;
      return {
        ...base,
        timeline: [
          ...base.timeline,
          {
            kind: "tool",
            id,
            tool: text(data.tool) || "tool",
            stepId: text(data.step_id) || undefined,
            status: "running",
            at,
          },
        ],
      };
    }

    case "tool_result": {
      const id = text(data.item_id);
      const error = text(data.error);
      const errorCode = text(data.error_code);
      return {
        ...base,
        timeline: base.timeline.map((item) =>
          item.kind === "tool" && item.id === id
            ? {
                ...item,
                status: "done",
                ok: data.ok !== false,
                stepId: text(data.step_id) || item.stepId,
                ...(error ? { error } : {}),
                ...(errorCode ? { errorCode } : {}),
              }
            : item,
        ),
      };
    }

    case "node_log": {
      const message = text(data.message).trim();
      if (!message) return base;
      return {
        ...base,
        timeline: [
          ...base.timeline,
          {
            kind: "notice",
            id: `log-${base.timeline.length + 1}`,
            text: `[${text(data.node_id)}] ${message}`,
            at,
          },
        ],
      };
    }

    case "message_delta": {
      const delta = text(data.delta);
      if (!delta) return base;
      const timelineId = `assistant-${text(data.item_id) || "current"}`;
      const index = base.timeline.findIndex((item) => item.id === timelineId);
      if (index >= 0) {
        return {
          ...base,
          timeline: base.timeline.map((item, position) =>
            position === index && item.kind === "assistant"
              ? { ...item, text: item.text + delta }
              : item,
          ),
        };
      }
      return {
        ...base,
        timeline: [...base.timeline, { kind: "assistant", id: timelineId, text: delta, at }],
      };
    }

    case "message": {
      const message = text(data.text).trim();
      if (!message) return base;
      const itemId = text(data.item_id);
      const timelineId = itemId ? `assistant-${itemId}` : null;
      // A streamed message finalises the placeholder its deltas created rather
      // than appending a duplicate copy of the same reply.
      if (timelineId && base.timeline.some((item) => item.id === timelineId)) {
        return {
          ...base,
          timeline: base.timeline.map((item) =>
            item.id === timelineId && item.kind === "assistant"
              ? { ...item, text: message }
              : item,
          ),
        };
      }
      return {
        ...base,
        timeline: [
          ...base.timeline,
          {
            kind: "assistant",
            id: timelineId ?? `assistant-${base.timeline.length + 1}`,
            text: message,
            at,
          },
        ],
      };
    }

    case "notice": {
      const message = text(data.message).trim();
      if (!message) return base;
      return {
        ...base,
        timeline: [
          ...base.timeline,
          { kind: "notice", id: `notice-${base.timeline.length + 1}`, text: message, at },
        ],
      };
    }

    case "error": {
      const message = text(data.message) || "the task failed";
      return {
        ...base,
        error: message,
        timeline: [
          ...base.timeline,
          { kind: "error", id: `error-${base.timeline.length + 1}`, text: message, at },
        ],
      };
    }

    case "done":
      return { ...base, done: true };

    default:
      // Forward compatibility: an unknown event must never blank the UI.
      return base;
  }
}

/** Rebuild state from a `/history` snapshot after a reload or reconnect. */
export function hydrateLoomState(history: History): LoomState {
  let state = initialLoomState;

  for (const value of history.plans ?? []) {
    const plan = parsePlan(value);
    if (plan) state = withPlan(state, plan);
  }

  for (const value of history.events ?? []) {
    const row = record(value);
    state = reduceLoomEvent(state, {
      seq: num(row.seq),
      event: text(row.event),
      data: row.data,
      ts: text(row.ts) || undefined,
    });
  }

  // The persisted plan wins over any replayed intermediate state.
  const current = parsePlan(history.current_plan);
  if (current) state = withPlan(state, current);

  const executions = [...state.executions];
  for (const value of history.executions ?? []) {
    const execution = parseExecution(value);
    if (!execution) continue;
    const index = executions.findIndex(
      (item) => item.kind === execution.kind && item.id === execution.id,
    );
    if (index >= 0) executions[index] = { ...executions[index], ...execution };
    else executions.push(execution);
  }

  let artifacts = mergeArtifacts(state.artifacts, parseArtifacts(history.artifacts));
  for (const execution of executions) {
    artifacts = mergeArtifacts(artifacts, execution.artifacts);
  }

  const uploads = (history.uploads ?? [])
    .map(parseUpload)
    .filter((item): item is Upload => item !== null);

  return { ...state, executions, artifacts, uploads, done: true };
}

// ── Derived views ───────────────────────────────────────────────────────────

export function deriveTaskPhase(state: LoomState, busy: boolean): TaskPhase {
  const steps = state.currentPlan?.steps ?? [];
  if (steps.length > 0) {
    if (steps.some((step) => step.status === "running")) return "executing";
    if (steps.every((step) => ["succeeded", "failed", "skipped"].includes(step.status))) {
      return "completed";
    }
    return busy ? "executing" : "planned";
  }
  if (busy) return "orienting";
  return state.timeline.length ? "completed" : "idle";
}

const ORIENTATION_LABELS: Array<[RegExp, string]> = [
  [/session_context/, "Reading task context"],
  [/capability_search|catalog_search/, "Searching available capabilities"],
  [/inspect_source/, "Inspecting the data"],
  [/request_inputs/, "Requesting missing files"],
  [/publish_plan/, "Publishing the plan"],
  [/update_step/, "Reporting step progress"],
  [/register_artifacts/, "Registering deliverables"],
  [/run_capability|run_workflow/, "Running work"],
];

/** Group pre-plan tool calls into a short "what is it doing" list. */
export function orientationActivities(
  timeline: TimelineItem[],
  limit = 6,
): OrientationActivity[] {
  const grouped = new Map<string, OrientationActivity>();
  for (const item of timeline) {
    if (item.kind !== "tool" || item.stepId) continue;
    const match = ORIENTATION_LABELS.find(([pattern]) => pattern.test(item.tool));
    const label = match ? match[1] : "Calling a platform tool";
    const status =
      item.status === "running" ? "running" : item.ok === false ? "failed" : "succeeded";
    const existing = grouped.get(label);
    grouped.set(label, {
      id: existing?.id ?? item.id,
      label,
      count: (existing?.count ?? 0) + 1,
      status,
    });
  }
  return [...grouped.values()].slice(-limit);
}

export function planProgress(plan: Plan | null): {
  total: number;
  succeeded: number;
  failed: number;
  running: number;
  pending: number;
  skipped: number;
  fraction: number;
} {
  const steps = plan?.steps ?? [];
  const count = (status: StepStatus) =>
    steps.filter((step) => step.status === status).length;
  const settled = count("succeeded") + count("failed") + count("skipped");
  return {
    total: steps.length,
    succeeded: count("succeeded"),
    failed: count("failed"),
    running: count("running"),
    pending: count("pending"),
    skipped: count("skipped"),
    fraction: steps.length ? settled / steps.length : 0,
  };
}

/** Steps that could start right now — useful for "what happens next" hints. */
export function readySteps(plan: Plan | null): PlanStep[] {
  if (!plan) return [];
  const statuses = new Map(plan.steps.map((step) => [step.id, step.status]));
  return plan.steps.filter(
    (step) =>
      step.status === "pending" &&
      step.depends_on.every((id) => statuses.get(id) === "succeeded"),
  );
}

export function pendingInputRequests(state: LoomState): InputRequest[] {
  const resolved = new Set([
    ...state.fulfilledInputRequestIds,
    ...state.cancelledInputRequestIds,
  ]);
  return state.timeline
    .filter(
      (item): item is Extract<TimelineItem, { kind: "input_request" }> =>
        item.kind === "input_request",
    )
    .map((item) => item.request)
    .filter((request) => !resolved.has(request.request_id));
}
