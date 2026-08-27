/**
 * `LoomWorkbench` — a complete, drop-in task UI.
 *
 * Composes the hook and the panels into the layout most agent workbenches want:
 * the plan graph as the primary surface, a conversation and artifact rail beside
 * it, and interrupts (file requests, approvals) surfaced where they block.
 *
 * It is one opinion, not the API. Every piece it uses is exported individually,
 * so replacing this file with your own layout costs nothing.
 */

import { useState } from "react";

import { useLoomSession, type UseLoomSessionOptions } from "../useLoomSession";
import { PlanGraph, StepDetail } from "./PlanGraph";
import {
  ApprovalPanel,
  ArtifactList,
  ExecutionList,
  InputRequestPanel,
  OrientationPanel,
  PlanProgress,
  Timeline,
} from "./panels";

export interface LoomWorkbenchProps extends UseLoomSessionOptions {
  title?: string;
  placeholder?: string;
  /** Optional markdown renderer for agent messages. */
  renderMarkdown?: (text: string) => React.ReactNode;
  className?: string;
}

export function LoomWorkbench({
  title = "LoomCraft",
  placeholder = "Describe what you want done…",
  renderMarkdown,
  className,
  ...sessionOptions
}: LoomWorkbenchProps) {
  const session = useLoomSession(sessionOptions);
  const [draft, setDraft] = useState("");

  const selectedStep =
    session.visiblePlan?.steps.find((step) => step.id === session.selectedStepId) ?? null;

  const submit = async (event: React.FormEvent) => {
    event.preventDefault();
    const message = draft.trim();
    if (!message) return;
    setDraft("");
    await session.send(message);
  };

  return (
    <div className={["lc-workbench", className].filter(Boolean).join(" ")}>
      <header className="lc-workbench__header">
        <h2 className="lc-workbench__title">{title}</h2>
        <span className={`lc-phase lc-phase--${session.phase}`}>{session.phase}</span>
        {session.busy && (
          <button type="button" className="lc-button lc-button--ghost" onClick={session.cancel}>
            Stop
          </button>
        )}
      </header>

      {session.error && (
        <p className="lc-workbench__error" role="alert">
          {session.error}
        </p>
      )}

      <div className="lc-workbench__body">
        <main className="lc-workbench__main">
          {session.visiblePlan ? (
            <>
              <PlanProgress plan={session.visiblePlan} />
              <PlanGraph
                plan={session.visiblePlan}
                plans={session.state.plans}
                selectedStepId={session.selectedStepId}
                onSelectStep={session.selectStep}
                onSelectRevision={(revision) =>
                  session.selectRevision(
                    revision === session.state.currentPlan?.revision ? null : revision,
                  )
                }
              />
              {selectedStep && (
                <StepDetail step={selectedStep} onClose={() => session.selectStep(null)} />
              )}
            </>
          ) : (
            <OrientationPanel activities={session.orientation} />
          )}
        </main>

        <aside className="lc-workbench__rail">
          {session.pendingRequests.map((request) => (
            <InputRequestPanel
              key={request.request_id}
              request={request}
              uploadedFilenames={session.state.uploads.map((upload) => upload.filename)}
              onUpload={(files) => void session.upload(files)}
              onConfirm={() => void session.fulfillRequest(request.request_id)}
              onCancel={() => void session.cancelRequest(request.request_id)}
              busy={session.busy}
            />
          ))}

          {Object.entries(session.state.pendingApprovals).map(([runId, nodes]) => (
            <ApprovalPanel
              key={runId}
              executionId={runId}
              nodes={nodes}
              onResolve={(nodeId, approved) =>
                void session.approve(runId, nodeId, approved)
              }
            />
          ))}

          <section className="lc-panel">
            <h3 className="lc-panel__title">Conversation</h3>
            <Timeline items={session.state.timeline} renderMarkdown={renderMarkdown} />
          </section>

          {session.state.executions.length > 0 && (
            <section className="lc-panel">
              <h3 className="lc-panel__title">Executions</h3>
              <ExecutionList executions={session.state.executions} />
            </section>
          )}

          <section className="lc-panel">
            <h3 className="lc-panel__title">Deliverables</h3>
            <ArtifactList
              artifacts={session.state.artifacts}
              onDownload={(artifact) => void session.download(artifact)}
            />
          </section>
        </aside>
      </div>

      <form className="lc-composer" onSubmit={submit}>
        <label className="lc-button lc-button--ghost">
          Attach
          <input
            type="file"
            multiple
            hidden
            onChange={(event) => {
              if (event.target.files?.length) void session.upload(event.target.files);
              event.target.value = "";
            }}
          />
        </label>
        <input
          className="lc-composer__input"
          value={draft}
          placeholder={placeholder}
          onChange={(event) => setDraft(event.target.value)}
          disabled={session.busy}
          aria-label="Task instruction"
        />
        <button
          type="submit"
          className="lc-button lc-button--primary"
          disabled={session.busy || !draft.trim()}
        >
          {session.busy ? "Working…" : "Send"}
        </button>
      </form>
    </div>
  );
}
