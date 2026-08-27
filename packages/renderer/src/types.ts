/**
 * Wire types shared with the Python core.
 *
 * These mirror `loomcraft.plan`, `loomcraft.events`, and `loomcraft.inputs`. They
 * are intentionally permissive on the read side: the reducer coerces unknown
 * payloads rather than throwing, so a server that adds a field cannot blank the
 * UI for clients that have not shipped yet.
 */

export type StepKind = "answer" | "capability" | "workflow" | "dynamic" | "review";

export type StepStatus = "pending" | "running" | "succeeded" | "failed" | "skipped";

/** What the workbench is doing right now, derived from plan + busy state. */
export type TaskPhase = "idle" | "orienting" | "planned" | "executing" | "completed";

export interface PlanStep {
  id: string;
  title: string;
  kind: StepKind;
  depends_on: string[];
  capability: string | null;
  description: string;
  status: StepStatus;
  summary: string | null;
  execution: Record<string, unknown> | null;
}

export interface Plan {
  goal: string;
  summary: string;
  revision: number;
  reason: string | null;
  steps: PlanStep[];
}

export interface Artifact {
  id: string;
  filename: string;
  display_name?: string;
  size: number;
  checksum?: string;
  content_type?: string;
  source_ref?: string;
  port_name?: string | null;
  step_id?: string | null;
  run_id?: string | null;
  download_url?: string;
}

export interface Execution {
  id: string | null;
  kind: "capability" | "workflow";
  capability: string;
  status: string;
  step_id?: string;
  duration_seconds?: number;
  error?: string | null;
  failed_nodes?: Array<Record<string, unknown>>;
  artifacts: Artifact[];
  /** Per-node progress within this execution, keyed by node id. */
  nodes?: Record<string, NodeProgress>;
}

export interface NodeProgress {
  node_id: string;
  status: string;
  attempt?: number;
  max_attempts?: number;
  fraction?: number;
  message?: string;
  error?: string | null;
  retry_in_seconds?: number;
  duration_seconds?: number;
}

export interface FileRequirement {
  key: string;
  label: string;
  description: string;
  required: boolean;
  min_files: number;
  max_files: number;
  allowed_extensions: string[];
  field_hints: string[];
}

export interface InputRequest {
  request_id: string;
  title: string;
  message: string;
  requirements: FileRequirement[];
  continue_prompt: string;
}

export interface Upload {
  id: string;
  filename: string;
  size: number;
  checksum?: string;
  content_type?: string;
  source_ref: string;
  created_at?: string;
}

export interface LoomEvent {
  seq?: number;
  event: string;
  data: unknown;
  ts?: string;
}

export type TimelineItem =
  | { kind: "user"; id: string; text: string; at?: string }
  | { kind: "assistant"; id: string; text: string; at?: string }
  | { kind: "notice"; id: string; text: string; at?: string }
  | { kind: "error"; id: string; text: string; at?: string }
  | {
      kind: "tool";
      id: string;
      tool: string;
      stepId?: string;
      status: "running" | "done";
      ok?: boolean;
      error?: string;
      errorCode?: string;
      at?: string;
    }
  | {
      kind: "execution";
      id: string;
      executionKind: "capability" | "workflow";
      executionId: string | null;
      stepId?: string;
      at?: string;
    }
  | { kind: "input_request"; id: string; request: InputRequest; at?: string }
  | {
      kind: "approval";
      id: string;
      executionId: string;
      nodes: string[];
      resolved?: { node: string; approved: boolean };
      at?: string;
    };

/** Coarse activity summary shown while the agent is still orienting. */
export interface OrientationActivity {
  id: string;
  label: string;
  count: number;
  status: "running" | "succeeded" | "failed";
}

export interface LoomState {
  /** Every published revision, ascending. */
  plans: Plan[];
  currentPlan: Plan | null;
  timeline: TimelineItem[];
  executions: Execution[];
  artifacts: Artifact[];
  uploads: Upload[];
  fulfilledInputRequestIds: string[];
  cancelledInputRequestIds: string[];
  pendingApprovals: Record<string, string[]>;
  lastSeq: number;
  error: string | null;
  done: boolean;
}

export interface History {
  session?: Record<string, unknown>;
  current_plan?: unknown;
  plans?: unknown[];
  events?: unknown[];
  uploads?: unknown[];
  executions?: unknown[];
  artifacts?: unknown[];
}
