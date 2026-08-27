/**
 * Supporting panels: timeline, artifacts, file requests, approvals, progress.
 *
 * Each is a plain presentational component over reducer output, so a host can
 * take one, take all, or take none and render its own from the same state.
 */

import type {
  Artifact,
  Execution,
  InputRequest,
  OrientationActivity,
  Plan,
  TimelineItem,
} from "../types";
import { planProgress } from "../state";

function classNames(...values: Array<string | false | null | undefined>): string {
  return values.filter(Boolean).join(" ");
}

export function formatBytes(size: number): string {
  if (!Number.isFinite(size) || size <= 0) return "0 B";
  const units = ["B", "KB", "MB", "GB", "TB"];
  const index = Math.min(units.length - 1, Math.floor(Math.log(size) / Math.log(1024)));
  const value = size / 1024 ** index;
  return `${value >= 10 || index === 0 ? Math.round(value) : value.toFixed(1)} ${units[index]}`;
}

// ── Progress ────────────────────────────────────────────────────────────────

export function PlanProgress({ plan }: { plan: Plan | null }) {
  const progress = planProgress(plan);
  if (!progress.total) return null;
  return (
    <div className="lc-progress" role="group" aria-label="Plan progress">
      <div
        className="lc-progress__bar"
        role="progressbar"
        aria-valuemin={0}
        aria-valuemax={progress.total}
        aria-valuenow={progress.succeeded + progress.failed + progress.skipped}
      >
        <span
          className="lc-progress__fill"
          style={{ width: `${Math.round(progress.fraction * 100)}%` }}
        />
      </div>
      <p className="lc-progress__legend">
        <span className="lc-tag lc-tag--succeeded">{progress.succeeded} done</span>
        {progress.running > 0 && (
          <span className="lc-tag lc-tag--running">{progress.running} running</span>
        )}
        {progress.pending > 0 && (
          <span className="lc-tag lc-tag--pending">{progress.pending} pending</span>
        )}
        {progress.failed > 0 && (
          <span className="lc-tag lc-tag--failed">{progress.failed} failed</span>
        )}
        {progress.skipped > 0 && (
          <span className="lc-tag lc-tag--skipped">{progress.skipped} skipped</span>
        )}
      </p>
    </div>
  );
}

// ── Orientation ─────────────────────────────────────────────────────────────

export function OrientationPanel({
  activities,
}: {
  activities: OrientationActivity[];
}) {
  return (
    <section className="lc-orientation" aria-label="Agent orientation">
      <h4 className="lc-orientation__title">Understanding the task</h4>
      {activities.length === 0 ? (
        <p className="lc-orientation__empty">Waiting for the first observable activity…</p>
      ) : (
        <ul className="lc-orientation__list">
          {activities.map((activity) => (
            <li
              key={activity.id}
              className={classNames("lc-orientation__item", `is-${activity.status}`)}
            >
              <span className="lc-orientation__icon" aria-hidden="true">
                {activity.status === "running" ? "◐" : activity.status === "failed" ? "✕" : "✓"}
              </span>
              <span className="lc-orientation__label">{activity.label}</span>
              {activity.count > 1 && (
                <span className="lc-orientation__count">×{activity.count}</span>
              )}
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}

// ── Timeline ────────────────────────────────────────────────────────────────

export function Timeline({
  items,
  renderMarkdown,
}: {
  items: TimelineItem[];
  /** Optional markdown renderer; plain text is used when absent. */
  renderMarkdown?: (text: string) => React.ReactNode;
}) {
  const render = (text: string) => (renderMarkdown ? renderMarkdown(text) : text);
  return (
    <ol className="lc-timeline" aria-label="Task timeline">
      {items.map((item) => {
        switch (item.kind) {
          case "user":
            return (
              <li key={item.id} className="lc-timeline__item lc-timeline__item--user">
                <span className="lc-timeline__role">You</span>
                <div className="lc-timeline__body">{item.text}</div>
              </li>
            );
          case "assistant":
            return (
              <li key={item.id} className="lc-timeline__item lc-timeline__item--assistant">
                <span className="lc-timeline__role">Agent</span>
                <div className="lc-timeline__body">{render(item.text)}</div>
              </li>
            );
          case "notice":
            return (
              <li key={item.id} className="lc-timeline__item lc-timeline__item--notice">
                <div className="lc-timeline__body">{item.text}</div>
              </li>
            );
          case "error":
            return (
              <li key={item.id} className="lc-timeline__item lc-timeline__item--error">
                <span className="lc-timeline__role">Error</span>
                <div className="lc-timeline__body">{item.text}</div>
              </li>
            );
          case "tool":
            return (
              <li
                key={item.id}
                className={classNames(
                  "lc-timeline__item lc-timeline__item--tool",
                  item.status === "running" && "is-running",
                  item.ok === false && "is-failed",
                )}
              >
                <span className="lc-timeline__icon" aria-hidden="true">
                  {item.status === "running" ? "◐" : item.ok === false ? "✕" : "✓"}
                </span>
                <code className="lc-timeline__tool">{item.tool}</code>
                {item.stepId && <span className="lc-timeline__step">{item.stepId}</span>}
                {item.error && <span className="lc-timeline__error">{item.error}</span>}
              </li>
            );
          case "execution":
            return (
              <li key={item.id} className="lc-timeline__item lc-timeline__item--execution">
                <span className="lc-timeline__icon" aria-hidden="true">
                  ▸
                </span>
                <span>
                  Ran {item.executionKind}
                  {item.stepId ? ` for ${item.stepId}` : ""}
                </span>
              </li>
            );
          case "input_request":
            return (
              <li key={item.id} className="lc-timeline__item lc-timeline__item--request">
                <span className="lc-timeline__role">Files needed</span>
                <div className="lc-timeline__body">{item.request.title}</div>
              </li>
            );
          case "approval":
            return (
              <li key={item.id} className="lc-timeline__item lc-timeline__item--approval">
                <span className="lc-timeline__role">Approval</span>
                <div className="lc-timeline__body">
                  {item.resolved
                    ? `${item.resolved.node} was ${item.resolved.approved ? "approved" : "rejected"}`
                    : `Waiting on ${item.nodes.join(", ")}`}
                </div>
              </li>
            );
          default:
            return null;
        }
      })}
    </ol>
  );
}

// ── Artifacts ───────────────────────────────────────────────────────────────

export function ArtifactList({
  artifacts,
  onDownload,
  emptyMessage = "Deliverables appear here as steps produce them.",
}: {
  artifacts: Artifact[];
  onDownload?: (artifact: Artifact) => void;
  emptyMessage?: string;
}) {
  if (!artifacts.length) {
    return <p className="lc-artifacts__empty">{emptyMessage}</p>;
  }
  return (
    <ul className="lc-artifacts" aria-label="Produced artifacts">
      {artifacts.map((artifact) => (
        <li key={artifact.id} className="lc-artifacts__item">
          <span className="lc-artifacts__icon" aria-hidden="true">
            ⎙
          </span>
          <span className="lc-artifacts__meta">
            <span className="lc-artifacts__name" title={artifact.filename}>
              {artifact.display_name || artifact.filename}
            </span>
            <span className="lc-artifacts__detail">
              {formatBytes(artifact.size)}
              {artifact.step_id ? ` · ${artifact.step_id}` : ""}
            </span>
          </span>
          {onDownload && (
            <button
              type="button"
              className="lc-button lc-button--ghost"
              onClick={() => onDownload(artifact)}
            >
              Download
            </button>
          )}
        </li>
      ))}
    </ul>
  );
}

// ── Input requests ──────────────────────────────────────────────────────────

export function InputRequestPanel({
  request,
  uploadedFilenames = [],
  onUpload,
  onConfirm,
  onCancel,
  busy = false,
}: {
  request: InputRequest;
  uploadedFilenames?: string[];
  onUpload?: (files: FileList) => void;
  onConfirm?: () => void;
  onCancel?: () => void;
  busy?: boolean;
}) {
  return (
    <section className="lc-request" aria-label={request.title}>
      <header className="lc-request__head">
        <h4>{request.title}</h4>
        <p>{request.message}</p>
      </header>
      <ul className="lc-request__slots">
        {request.requirements.map((requirement) => (
          <li key={requirement.key} className="lc-request__slot">
            <span className="lc-request__slot-label">
              {requirement.label}
              {requirement.required ? (
                <span className="lc-request__badge">required</span>
              ) : (
                <span className="lc-request__badge lc-request__badge--optional">optional</span>
              )}
            </span>
            <p className="lc-request__slot-description">{requirement.description}</p>
            <p className="lc-request__slot-meta">
              {requirement.allowed_extensions.length
                ? requirement.allowed_extensions.join(", ")
                : "any file type"}
              {" · "}
              {requirement.min_files === requirement.max_files
                ? `${requirement.max_files} file(s)`
                : `${requirement.min_files}–${requirement.max_files} files`}
            </p>
            {requirement.field_hints.length > 0 && (
              <p className="lc-request__hints">
                Expected fields: {requirement.field_hints.join(", ")}
              </p>
            )}
          </li>
        ))}
      </ul>
      {uploadedFilenames.length > 0 && (
        <p className="lc-request__uploaded">Uploaded: {uploadedFilenames.join(", ")}</p>
      )}
      <div className="lc-request__actions">
        {onUpload && (
          <label className="lc-button">
            Choose files
            <input
              type="file"
              multiple
              hidden
              onChange={(event) => {
                if (event.target.files?.length) onUpload(event.target.files);
                event.target.value = "";
              }}
            />
          </label>
        )}
        {onConfirm && (
          <button
            type="button"
            className="lc-button lc-button--primary"
            disabled={busy}
            onClick={onConfirm}
          >
            {busy ? "Checking…" : "Continue"}
          </button>
        )}
        {onCancel && (
          <button
            type="button"
            className="lc-button lc-button--ghost"
            disabled={busy}
            onClick={onCancel}
          >
            Skip this
          </button>
        )}
      </div>
    </section>
  );
}

// ── Approvals ───────────────────────────────────────────────────────────────

export function ApprovalPanel({
  executionId,
  nodes,
  onResolve,
  busy = false,
}: {
  executionId: string;
  nodes: string[];
  onResolve?: (nodeId: string, approved: boolean) => void;
  busy?: boolean;
}) {
  if (!nodes.length) return null;
  return (
    <section className="lc-approval" aria-label="Pending approvals">
      <h4 className="lc-approval__title">Waiting for your approval</h4>
      <ul className="lc-approval__list">
        {nodes.map((nodeId) => (
          <li key={nodeId} className="lc-approval__item">
            <code>{nodeId}</code>
            <span className="lc-approval__actions">
              <button
                type="button"
                className="lc-button lc-button--primary"
                disabled={busy}
                onClick={() => onResolve?.(nodeId, true)}
              >
                Approve
              </button>
              <button
                type="button"
                className="lc-button lc-button--ghost"
                disabled={busy}
                onClick={() => onResolve?.(nodeId, false)}
              >
                Reject
              </button>
            </span>
          </li>
        ))}
      </ul>
      <p className="lc-approval__hint">Execution {executionId}</p>
    </section>
  );
}

// ── Executions ──────────────────────────────────────────────────────────────

export function ExecutionList({ executions }: { executions: Execution[] }) {
  if (!executions.length) return null;
  return (
    <ul className="lc-executions" aria-label="Executions">
      {executions.map((execution) => (
        <li
          key={`${execution.kind}-${execution.id}`}
          className={classNames("lc-executions__item", `is-${execution.status}`)}
        >
          <span className="lc-executions__name">{execution.capability}</span>
          <span className="lc-executions__status">{execution.status}</span>
          {execution.duration_seconds !== undefined && (
            <span className="lc-executions__duration">
              {execution.duration_seconds.toFixed(1)}s
            </span>
          )}
          {execution.nodes && Object.keys(execution.nodes).length > 1 && (
            <ul className="lc-executions__nodes">
              {Object.values(execution.nodes).map((node) => (
                <li key={node.node_id} className={`is-${node.status}`}>
                  <span>{node.node_id}</span>
                  <span>{node.status}</span>
                  {node.attempt && node.max_attempts && node.max_attempts > 1 && (
                    <span className="lc-executions__attempt">
                      attempt {node.attempt}/{node.max_attempts}
                    </span>
                  )}
                </li>
              ))}
            </ul>
          )}
          {execution.error && <p className="lc-executions__error">{execution.error}</p>}
        </li>
      ))}
    </ul>
  );
}
