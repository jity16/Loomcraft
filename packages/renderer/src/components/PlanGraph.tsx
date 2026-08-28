/**
 * The plan DAG canvas: SVG, pan/zoom, selection, live status — no chart library.
 *
 * Nodes are absolutely-positioned HTML (so titles wrap and stay selectable and
 * searchable) sitting over an SVG edge layer, both inside one transformed
 * container. That keeps text crisp at any zoom while the edges get real curves.
 *
 * Accessibility: the graph is `aria-hidden` and paired with a visually hidden
 * ordered list stating each step, its status, and its dependencies — a DAG whose
 * only representation is spatial is unusable with a screen reader.
 */

import { useCallback, useEffect, useId, useMemo, useRef, useState } from "react";

import { fitToViewport, layoutPlan, type LayoutOptions } from "../layout";
import type { Plan, PlanStep, StepKind, StepStatus } from "../types";

export interface PlanGraphProps {
  plan: Plan | null;
  /** All revisions; renders a revision switcher when more than one exists. */
  plans?: Plan[];
  selectedStepId?: string | null;
  onSelectStep?: (stepId: string | null) => void;
  onSelectRevision?: (revision: number) => void;
  layout?: LayoutOptions;
  className?: string;
  emptyMessage?: string;
  /** Show the built-in zoom controls. */
  controls?: boolean;
}

const KIND_LABEL: Record<StepKind, string> = {
  answer: "Answer",
  capability: "Capability",
  workflow: "Workflow",
  dynamic: "Dynamic",
  review: "Review",
};

const KIND_GLYPH: Record<StepKind, string> = {
  answer: "✎",
  capability: "◈",
  workflow: "⛭",
  dynamic: "⌘",
  review: "⌕",
};

const STATUS_LABEL: Record<StepStatus, string> = {
  pending: "Pending",
  ready: "Ready",
  running: "Running",
  waiting_approval: "Waiting for approval",
  succeeded: "Succeeded",
  failed: "Failed",
  skipped: "Skipped",
  cancelled: "Cancelled",
};

const MIN_ZOOM = 0.3;
const MAX_ZOOM = 2;

function classNames(...values: Array<string | false | null | undefined>): string {
  return values.filter(Boolean).join(" ");
}

function PlanGraphNode({
  step,
  selected,
  onSelect,
}: {
  step: PlanStep;
  selected: boolean;
  onSelect: () => void;
}) {
  return (
    <button
      type="button"
      onClick={(event) => {
        event.stopPropagation();
        onSelect();
      }}
      className={classNames(
        "lc-node",
        `lc-node--${step.status}`,
        selected && "lc-node--selected",
      )}
      data-step-id={step.id}
      tabIndex={-1}
    >
      <span className="lc-node__head">
        <span className="lc-node__glyph" aria-hidden="true">
          {KIND_GLYPH[step.kind] ?? "◈"}
        </span>
        <span className="lc-node__title" title={step.title}>
          {step.title}
        </span>
      </span>
      <span className="lc-node__meta" title={step.capability ?? KIND_LABEL[step.kind]}>
        {step.capability ?? KIND_LABEL[step.kind]}
      </span>
      <span className="lc-node__foot">
        <span className={classNames("lc-node__dot", `lc-node__dot--${step.status}`)} />
        <span className="lc-node__status">{STATUS_LABEL[step.status]}</span>
        <span className="lc-node__id">{step.id}</span>
      </span>
    </button>
  );
}

export function PlanGraph({
  plan,
  plans = [],
  selectedStepId = null,
  onSelectStep,
  onSelectRevision,
  layout: layoutOptions,
  className,
  emptyMessage = "No plan yet. Steps appear here as soon as the agent publishes one.",
  controls = true,
}: PlanGraphProps) {
  const containerRef = useRef<HTMLDivElement | null>(null);
  // SVG marker ids are document-global. Two graphs on one page (a revision
  // comparison, a dashboard) would otherwise share one set of arrowheads and
  // the second mount would silently repoint the first.
  const markerPrefix = `lc-arrow-${useId().replace(/:/g, "")}`;
  const [viewport, setViewport] = useState({ width: 0, height: 0 });
  const [transform, setTransform] = useState({ scale: 1, translateX: 0, translateY: 0 });
  const [userMoved, setUserMoved] = useState(false);
  const dragState = useRef<{ x: number; y: number; tx: number; ty: number } | null>(null);

  const layout = useMemo(() => layoutPlan(plan, layoutOptions), [plan, layoutOptions]);

  useEffect(() => {
    const element = containerRef.current;
    if (!element || typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(([entry]) => {
      setViewport({
        width: entry.contentRect.width,
        height: entry.contentRect.height,
      });
    });
    observer.observe(element);
    return () => observer.disconnect();
  }, []);

  const fit = useCallback(() => {
    if (!layout.width || !viewport.width) return;
    setTransform(fitToViewport(layout, viewport, { maxZoom: 1.05 }));
    setUserMoved(false);
  }, [layout, viewport]);

  // Re-fit when the graph changes shape, but never yank the view out from under
  // someone who has panned or zoomed deliberately.
  useEffect(() => {
    if (!userMoved) fit();
  }, [fit, userMoved, plan?.revision, layout.nodes.length]);

  const zoomBy = useCallback((factor: number, origin?: { x: number; y: number }) => {
    setUserMoved(true);
    setTransform((current) => {
      const scale = Math.min(MAX_ZOOM, Math.max(MIN_ZOOM, current.scale * factor));
      if (scale === current.scale) return current;
      const point = origin ?? { x: viewport.width / 2, y: viewport.height / 2 };
      // Keep the point under the cursor fixed while scaling.
      const ratio = scale / current.scale;
      return {
        scale,
        translateX: point.x - (point.x - current.translateX) * ratio,
        translateY: point.y - (point.y - current.translateY) * ratio,
      };
    });
  }, [viewport.height, viewport.width]);

  const handleWheel = (event: React.WheelEvent<HTMLDivElement>) => {
    if (!event.ctrlKey && !event.metaKey && Math.abs(event.deltaY) < 2) return;
    const rect = containerRef.current?.getBoundingClientRect();
    zoomBy(event.deltaY < 0 ? 1.1 : 1 / 1.1, {
      x: event.clientX - (rect?.left ?? 0),
      y: event.clientY - (rect?.top ?? 0),
    });
  };

  const handlePointerDown = (event: React.PointerEvent<HTMLDivElement>) => {
    if (event.button !== 0) return;
    dragState.current = {
      x: event.clientX,
      y: event.clientY,
      tx: transform.translateX,
      ty: transform.translateY,
    };
    event.currentTarget.setPointerCapture(event.pointerId);
  };

  const handlePointerMove = (event: React.PointerEvent<HTMLDivElement>) => {
    const drag = dragState.current;
    if (!drag) return;
    const dx = event.clientX - drag.x;
    const dy = event.clientY - drag.y;
    if (Math.abs(dx) + Math.abs(dy) > 3) setUserMoved(true);
    setTransform((current) => ({
      ...current,
      translateX: drag.tx + dx,
      translateY: drag.ty + dy,
    }));
  };

  const endDrag = (event: React.PointerEvent<HTMLDivElement>) => {
    dragState.current = null;
    if (event.currentTarget.hasPointerCapture(event.pointerId)) {
      event.currentTarget.releasePointerCapture(event.pointerId);
    }
  };

  if (!plan || !layout.nodes.length) {
    return (
      <div className={classNames("lc-graph lc-graph--empty", className)}>
        <p className="lc-graph__empty">{emptyMessage}</p>
      </div>
    );
  }

  const statusById = new Map(plan.steps.map((step) => [step.id, step.status]));

  return (
    <div className={classNames("lc-graph", className)}>
      <header className="lc-graph__header">
        <div className="lc-graph__identity">
          <span className="lc-graph__revision">R{plan.revision}</span>
          <span className="lc-graph__count">{plan.steps.length} steps</span>
          <h3 className="lc-graph__goal" title={plan.goal}>
            {plan.goal}
          </h3>
        </div>
        {plans.length > 1 && (
          <nav className="lc-graph__revisions" aria-label="Plan revisions">
            {plans.map((item) => (
              <button
                key={item.revision}
                type="button"
                aria-current={item.revision === plan.revision}
                className={classNames(
                  "lc-graph__revision-button",
                  item.revision === plan.revision && "is-active",
                )}
                onClick={() => onSelectRevision?.(item.revision)}
              >
                R{item.revision}
              </button>
            ))}
          </nav>
        )}
      </header>

      {plan.reason && (
        <p className="lc-graph__reason">
          <span className="lc-graph__reason-label">Replanned:</span> {plan.reason}
        </p>
      )}

      {/* Screen-reader equivalent of the spatial graph. */}
      <ol className="lc-sr-only" aria-label={`Plan revision ${plan.revision} steps`}>
        {plan.steps.map((step) => (
          <li key={step.id}>
            {step.title}. Kind: {KIND_LABEL[step.kind]}. Status:{" "}
            {STATUS_LABEL[step.status]}.{" "}
            {step.depends_on.length
              ? `Depends on ${step.depends_on.join(", ")}.`
              : "No dependencies."}
            {step.summary ? ` ${step.summary}` : ""}
          </li>
        ))}
      </ol>

      <div
        ref={containerRef}
        className="lc-graph__canvas"
        onWheel={handleWheel}
        onPointerDown={handlePointerDown}
        onPointerMove={handlePointerMove}
        onPointerUp={endDrag}
        onPointerCancel={endDrag}
        onClick={() => onSelectStep?.(null)}
        role="presentation"
      >
        <div
          className="lc-graph__stage"
          style={{
            transform: `translate(${transform.translateX}px, ${transform.translateY}px) scale(${transform.scale})`,
            width: layout.width,
            height: layout.height,
          }}
          aria-hidden="true"
        >
          <svg
            className="lc-graph__edges"
            width={layout.width}
            height={layout.height}
            viewBox={`0 0 ${layout.width} ${layout.height}`}
          >
            <defs>
              {["idle", "active", "done"].map((tone) => (
                <marker
                  key={tone}
                  id={`${markerPrefix}-${tone}`}
                  viewBox="0 0 10 10"
                  refX="9"
                  refY="5"
                  markerWidth="9"
                  markerHeight="9"
                  // Without this, marker size scales with stroke-width, so the
                  // 2.2px "active" edge grows an arrowhead twice the size of
                  // every other one and the canvas reads as broken.
                  markerUnits="userSpaceOnUse"
                  orient="auto-start-reverse"
                >
                  <path d="M 0 0 L 10 5 L 0 10 z" className={`lc-arrow lc-arrow--${tone}`} />
                </marker>
              ))}
            </defs>
            {layout.edges.map((edge) => {
              const targetStatus = statusById.get(edge.target);
              const sourceStatus = statusById.get(edge.source);
              const tone =
                targetStatus === "running"
                  ? "active"
                  : sourceStatus === "succeeded" && targetStatus === "succeeded"
                    ? "done"
                    : "idle";
              return (
                <path
                  key={edge.id}
                  d={edge.path}
                  className={classNames(
                    "lc-edge",
                    `lc-edge--${tone}`,
                    edge.long && "lc-edge--long",
                  )}
                  markerEnd={`url(#${markerPrefix}-${tone})`}
                />
              );
            })}
          </svg>

          {layout.nodes.map((node) => (
            <div
              key={node.id}
              className="lc-graph__node-slot"
              style={{
                left: node.x,
                top: node.y,
                width: node.width,
                height: node.height,
              }}
            >
              <PlanGraphNode
                step={node.step}
                selected={node.id === selectedStepId}
                onSelect={() => onSelectStep?.(node.id)}
              />
            </div>
          ))}
        </div>

        {controls && (
          <div className="lc-graph__controls" onClick={(event) => event.stopPropagation()}>
            <button type="button" aria-label="Zoom in" onClick={() => zoomBy(1.2)}>
              +
            </button>
            <button type="button" aria-label="Zoom out" onClick={() => zoomBy(1 / 1.2)}>
              −
            </button>
            <button type="button" aria-label="Fit to view" onClick={fit}>
              ⤢
            </button>
          </div>
        )}
      </div>
    </div>
  );
}

export interface StepDetailProps {
  step: PlanStep | null;
  onClose?: () => void;
}

/** Detail card for the selected node. */
export function StepDetail({ step, onClose }: StepDetailProps) {
  if (!step) return null;
  return (
    <aside className="lc-step-detail" aria-label={`Details for ${step.title}`}>
      <header className="lc-step-detail__head">
        <span className={classNames("lc-node__dot", `lc-node__dot--${step.status}`)} />
        <h4>{step.title}</h4>
        {onClose && (
          <button type="button" aria-label="Close details" onClick={onClose}>
            ×
          </button>
        )}
      </header>
      <dl className="lc-step-detail__body">
        <div>
          <dt>Kind</dt>
          <dd>{KIND_LABEL[step.kind]}</dd>
        </div>
        <div>
          <dt>Status</dt>
          <dd>{STATUS_LABEL[step.status]}</dd>
        </div>
        {step.capability && (
          <div>
            <dt>Target</dt>
            <dd className="lc-mono">{step.capability}</dd>
          </div>
        )}
        <div>
          <dt>Depends on</dt>
          <dd className="lc-mono">
            {step.depends_on.length ? step.depends_on.join(" · ") : "nothing"}
          </dd>
        </div>
      </dl>
      {(step.summary || step.description) && (
        <p className="lc-step-detail__summary">{step.summary || step.description}</p>
      )}
    </aside>
  );
}
