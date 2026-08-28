/**
 * Wire types shared with the Python core.
 *
 * These mirror `loomcraft.plan`, `loomcraft.events`, and `loomcraft.inputs`. They
 * are intentionally permissive on the read side: the reducer coerces unknown
 * payloads rather than throwing, so a server that adds a field cannot blank the
 * UI for clients that have not shipped yet.
 */

export type StepKind = "answer" | "capability" | "workflow" | "dynamic" | "review";

export type StepStatus =
  | "pending"
  | "ready"
  | "running"
  | "waiting_approval"
  | "succeeded"
  | "failed"
  | "skipped"
  | "cancelled";

/** What the workbench is doing right now, derived from plan + busy state. */
export type TaskPhase = "idle" | "orienting" | "planned" | "executing" | "completed";

export interface PlanStep {
  id: string;
  title: string;
  kind: StepKind;
  depends_on: string[];
  capability: string | null;
  description?: string;
  status: StepStatus;
  summary: string | null;
  execution: Record<string, unknown> | null;
  attempts?: number;
  retry?: {
    max_attempts: number;
    backoff_seconds?: number;
    backoff_multiplier?: number;
    max_backoff_seconds?: number;
  };
  timeout_seconds?: number | null;
  on_failure?: "stop" | "continue" | "require_approval";
  metadata?: Record<string, unknown>;
}

export interface AnalysisObjective {
  id: string;
  question: string;
  status?: string | null;
  estimand?: string;
  independent_unit?: string;
  expected_outputs?: string[];
  method_families?: string[];
  validation_requirements?: string[];
}

export interface AnalysisCoverage {
  objective_id: string;
  status: string;
  reason: string;
  selected_method?: string | null;
  step_ids?: string[];
  artifact_refs?: string[];
  next_action?: string | null;
}

export interface Plan {
  goal: string;
  summary?: string;
  revision: number;
  reason?: string | null;
  steps: PlanStep[];
  analysis_profile?: string | null;
  objectives?: AnalysisObjective[];
  analysis_coverage?: AnalysisCoverage[];
  metadata?: Record<string, unknown>;
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
  path?: string | null;
  output_key?: string;
}

export interface Execution {
  id: string | null;
  kind: "capability" | "workflow" | "plan" | string;
  capability: string;
  status: string;
  step_id?: string;
  duration_seconds?: number;
  error?: string | null;
  failed_nodes?: Array<Record<string, unknown>>;
  artifacts: Artifact[];
  /** Per-node progress within this execution, keyed by node id. */
  nodes?: Record<string, NodeProgress>;
  revision?: number;
  run_id?: string | null;
  attempts?: number;
  run_url?: string | null;
  current_slot?: string | null;
  slots?: Record<string, string>;
  [key: string]: unknown;
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
  path?: string;
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
      command?: string;
      toolKind?: string;
      stepId?: string;
      status: "running" | "done";
      ok?: boolean;
      exit?: number | null;
      error?: string;
      errorCode?: string;
      at?: string;
    }
  | {
      kind: "execution";
      id: string;
      executionKind: string;
      executionId: string | null;
      stepId?: string;
      at?: string;
    }
  | { kind: "input_request"; id: string; request: InputRequest; at?: string }
  | { kind: "input"; id: string; request: InputRequest; at?: string }
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

// Compatibility aliases for the first extracted renderer API.
export type LoomcraftEvent<T = unknown> = LoomEvent & { data: T };
export type LoomcraftState = LoomState;
export type IntelligentPlan = Plan;
export type IntelligentPlanStep = PlanStep;
export type IntelligentAnalysisCoverage = AnalysisCoverage;
export type IntelligentPlanObjective = AnalysisObjective;
export type IntelligentEvent = LoomEvent;
export type IntelligentState = LoomState;
export type IntelligentExecution = Execution;
export type IntelligentArtifact = Artifact;
export type IntelligentFileRequirement = FileRequirement;
export type IntelligentInputRequest = InputRequest;
export type IntelligentTimelineItem = TimelineItem;
export type IntelligentOrientationActivity = OrientationActivity;
export type IntelligentStepKind = StepKind;
export type IntelligentStepStatus = StepStatus;
export type IntelligentTaskPhase = TaskPhase;

export interface History {
  session?: Record<string, unknown>;
  current_plan?: unknown;
  plans?: unknown[];
  events?: unknown[];
  uploads?: unknown[];
  executions?: unknown[];
  artifacts?: unknown[];
  /** Optional transcript rows supplied by compatibility hosts. */
  messages?: unknown[];
}

export type IntelligentHistory = History;
