/** Compatibility components for the pre-monorepo extracted renderer API. */

import type { Artifact, InputRequest, LoomState, PlanStep, TimelineItem } from "./types";
import { pendingInputRequests } from "./state";
import { PlanGraph } from "./components/PlanGraph";
import {
  ArtifactList as RemoteArtifactList,
  InputRequestPanel,
  Timeline,
} from "./components/panels";
import type { ReactNode } from "react";

export interface LoomcraftWorkbenchProps {
  state: LoomState;
  className?: string;
  showTimeline?: boolean;
  onStepSelect?: (step: PlanStep) => void;
  artifacts?: Artifact[];
  inputRequest?: InputRequest | null;
  onChooseFiles?: (requirementKey: string) => void;
  latestUserMessage?: string | null;
  renderMarkdown?: (text: string) => ReactNode;
}

/** State-driven workbench retained for hosts that own their own transport. */
export function LoomcraftWorkbench({
  state,
  className,
  showTimeline = true,
  onStepSelect,
  artifacts,
  inputRequest,
  onChooseFiles,
  latestUserMessage,
  renderMarkdown,
}: LoomcraftWorkbenchProps) {
  const plan = state.currentPlan ?? state.plans[state.plans.length - 1] ?? null;
  const pending = inputRequest ?? pendingInputRequests(state).at(-1);
  return (
    <section className={["lc-workbench lc-workbench--state", className ?? ""].filter(Boolean).join(" ")} aria-label="LoomCraft plan">
      {latestUserMessage ? <p className="lc-workbench__user-message">{latestUserMessage}</p> : null}
      {plan ? (
        <PlanGraph
          plan={plan}
          plans={state.plans}
          onSelectStep={(id) => {
            const step = plan.steps.find((item) => item.id === id);
            if (step) onStepSelect?.(step);
          }}
        />
      ) : (
        <p className="lc-workbench__empty">No plan has been published yet.</p>
      )}
      {showTimeline ? <Timeline items={state.timeline} renderMarkdown={renderMarkdown} /> : null}
      {pending ? (
        <InputRequestPanel
          request={pending}
          onUpload={() => onChooseFiles?.(pending.requirements[0]?.key ?? "")}
          onConfirm={() => undefined}
          onCancel={() => undefined}
        />
      ) : null}
      <RemoteArtifactList artifacts={artifacts ?? state.artifacts} />
      {onChooseFiles ? <span className="lc-workbench__file-hook" data-file-hook="true" /> : null}
    </section>
  );
}

export interface TaskFlowPanelProps extends Omit<LoomcraftWorkbenchProps, "state"> {
  plans?: LoomState["plans"];
  current?: LoomState["currentPlan"];
  timeline?: TimelineItem[];
}

/** Thin state projection matching the extracted ``TaskFlowPanel`` name. */
export function TaskFlowPanel({
  plans = [],
  current = null,
  timeline = [],
  ...rest
}: TaskFlowPanelProps) {
  const state: LoomState = {
    plans,
    currentPlan: current ?? plans[plans.length - 1] ?? null,
    timeline,
    executions: [],
    artifacts: rest.artifacts ?? [],
    uploads: [],
    fulfilledInputRequestIds: [],
    cancelledInputRequestIds: [],
    pendingApprovals: {},
    lastSeq: 0,
    error: null,
    done: false,
  };
  return <LoomcraftWorkbench {...rest} state={state} />;
}

export function InputRequestCard({
  request,
  fulfilled = false,
  onChooseFiles,
}: {
  request: InputRequest;
  fulfilled?: boolean;
  onChooseFiles?: (requirementKey: string) => void;
}) {
  void onChooseFiles;
  return (
    <InputRequestPanel
      request={request}
      uploadedFilenames={fulfilled ? request.requirements.map((item) => item.label) : []}
      onUpload={() => onChooseFiles?.(request.requirements[0]?.key ?? "")}
      onConfirm={() => undefined}
      onCancel={() => undefined}
    />
  );
}

export function EventTimeline({ items }: { items: TimelineItem[] }) {
  return <Timeline items={items} />;
}

export { RemoteArtifactList as CompatArtifactList };
