/**
 * Deterministic layered DAG layout — no dagre, no d3, no runtime dependencies.
 *
 * Three passes, the classic Sugiyama skeleton minus the parts a plan graph does
 * not need (plans are ≤24 nodes, so an exact-but-slow step would still be free):
 *
 * 1. **Layering.** Longest-path assignment: a node sits one row below its
 *    deepest dependency. Nodes sharing a row have no path between them, so a row
 *    is exactly what the engine will run in parallel — the picture and the
 *    execution model agree by construction.
 * 2. **Ordering.** Repeated barycenter sweeps reduce edge crossings. Ties break
 *    on node id so the same plan always draws identically; a layout that jitters
 *    between renders reads as motion the user has to re-parse.
 * 3. **Positioning.** Each node is centred over its neighbours where the row's
 *    slot spacing allows, then rows are centred against the widest row.
 *
 * Edges are cubic Béziers with vertical control points, which keeps a long
 * skip-edge visually distinct from a short parent edge.
 */

import type { Plan, PlanStep } from "./types";

export interface LayoutOptions {
  nodeWidth?: number;
  nodeHeight?: number;
  /** Horizontal gap between sibling nodes. */
  columnGap?: number;
  /** Vertical gap between dependency layers. */
  rowGap?: number;
  padding?: number;
  direction?: "vertical" | "horizontal";
  /** Barycenter sweeps. More passes, fewer crossings, diminishing returns. */
  sweeps?: number;
}

export interface LayoutNode {
  id: string;
  step: PlanStep;
  x: number;
  y: number;
  width: number;
  height: number;
  layer: number;
  order: number;
}

export interface LayoutEdge {
  id: string;
  source: string;
  target: string;
  path: string;
  /** Midpoint, for labels or badges. */
  labelX: number;
  labelY: number;
  /** True when the edge skips at least one layer. */
  long: boolean;
}

export interface Layout {
  nodes: LayoutNode[];
  edges: LayoutEdge[];
  width: number;
  height: number;
  layers: string[][];
}

const DEFAULTS: Required<LayoutOptions> = {
  nodeWidth: 224,
  nodeHeight: 92,
  columnGap: 32,
  rowGap: 64,
  padding: 28,
  direction: "vertical",
  sweeps: 6,
};

/** Longest-path layering: depth(n) = 1 + max(depth(dependencies)). */
export function assignLayers(steps: PlanStep[]): string[][] {
  const byId = new Map(steps.map((step) => [step.id, step]));
  const depth = new Map<string, number>();
  const visiting = new Set<string>();

  const resolve = (id: string): number => {
    const cached = depth.get(id);
    if (cached !== undefined) return cached;
    // A cycle should have been rejected server-side; degrade instead of hanging.
    if (visiting.has(id)) return 0;
    visiting.add(id);
    const step = byId.get(id);
    const dependencies = (step?.depends_on ?? []).filter((dep) => byId.has(dep));
    const value = dependencies.length
      ? 1 + Math.max(...dependencies.map(resolve))
      : 0;
    visiting.delete(id);
    depth.set(id, value);
    return value;
  };

  for (const step of steps) resolve(step.id);

  const grouped = new Map<number, string[]>();
  for (const step of steps) {
    const level = depth.get(step.id) ?? 0;
    grouped.set(level, [...(grouped.get(level) ?? []), step.id]);
  }
  return [...grouped.keys()]
    .sort((a, b) => a - b)
    .map((level) => (grouped.get(level) ?? []).slice().sort());
}

/** Barycenter crossing reduction, alternating down and up sweeps. */
function orderLayers(layers: string[][], steps: PlanStep[], sweeps: number): string[][] {
  const byId = new Map(steps.map((step) => [step.id, step]));
  const parentsOf = new Map<string, string[]>();
  const childrenOf = new Map<string, string[]>();
  for (const step of steps) {
    parentsOf.set(step.id, step.depends_on.filter((dep) => byId.has(dep)));
    for (const dep of step.depends_on) {
      if (!byId.has(dep)) continue;
      childrenOf.set(dep, [...(childrenOf.get(dep) ?? []), step.id]);
    }
  }

  let ordered = layers.map((layer) => [...layer]);

  const positions = (layer: string[]) => new Map(layer.map((id, index) => [id, index]));

  const sweep = (down: boolean) => {
    const range = down
      ? [...ordered.keys()].slice(1)
      : [...ordered.keys()].slice(0, -1).reverse();
    for (const index of range) {
      const neighbourIndex = down ? index - 1 : index + 1;
      const neighbourPositions = positions(ordered[neighbourIndex]);
      const related = down ? parentsOf : childrenOf;
      const scored = ordered[index].map((id, fallback) => {
        const neighbours = (related.get(id) ?? [])
          .map((other) => neighbourPositions.get(other))
          .filter((value): value is number => value !== undefined);
        const barycenter = neighbours.length
          ? neighbours.reduce((sum, value) => sum + value, 0) / neighbours.length
          : fallback;
        return { id, barycenter, fallback };
      });
      scored.sort(
        (a, b) => a.barycenter - b.barycenter || a.id.localeCompare(b.id),
      );
      ordered[index] = scored.map((entry) => entry.id);
    }
  };

  for (let pass = 0; pass < sweeps; pass += 1) sweep(pass % 2 === 0);
  return ordered;
}

function bezier(
  from: { x: number; y: number },
  to: { x: number; y: number },
  vertical: boolean,
): string {
  if (vertical) {
    const delta = Math.max(24, (to.y - from.y) / 2);
    return `M ${from.x} ${from.y} C ${from.x} ${from.y + delta}, ${to.x} ${to.y - delta}, ${to.x} ${to.y}`;
  }
  const delta = Math.max(24, (to.x - from.x) / 2);
  return `M ${from.x} ${from.y} C ${from.x + delta} ${from.y}, ${to.x - delta} ${to.y}, ${to.x} ${to.y}`;
}

/** Compute node boxes and edge paths for a plan. */
export function layoutPlan(plan: Plan | null, options: LayoutOptions = {}): Layout {
  const settings = { ...DEFAULTS, ...options };
  const steps = plan?.steps ?? [];
  if (!steps.length) {
    return { nodes: [], edges: [], width: 0, height: 0, layers: [] };
  }

  const vertical = settings.direction === "vertical";
  const layers = orderLayers(assignLayers(steps), steps, settings.sweeps);
  const byId = new Map(steps.map((step) => [step.id, step]));

  const spanSize = vertical ? settings.nodeWidth : settings.nodeHeight;
  const spanGap = vertical ? settings.columnGap : settings.rowGap;
  const depthSize = vertical ? settings.nodeHeight : settings.nodeWidth;
  const depthGap = vertical ? settings.rowGap : settings.columnGap;

  const widest = Math.max(...layers.map((layer) => layer.length));
  const spanExtent = widest * spanSize + (widest - 1) * spanGap;

  const nodes: LayoutNode[] = [];
  const placed = new Map<string, LayoutNode>();

  layers.forEach((layer, layerIndex) => {
    const layerExtent = layer.length * spanSize + (layer.length - 1) * spanGap;
    const offset = settings.padding + (spanExtent - layerExtent) / 2;
    layer.forEach((id, orderIndex) => {
      const step = byId.get(id);
      if (!step) return;
      const spanPosition = offset + orderIndex * (spanSize + spanGap);
      const depthPosition = settings.padding + layerIndex * (depthSize + depthGap);
      const node: LayoutNode = {
        id,
        step,
        x: vertical ? spanPosition : depthPosition,
        y: vertical ? depthPosition : spanPosition,
        width: settings.nodeWidth,
        height: settings.nodeHeight,
        layer: layerIndex,
        order: orderIndex,
      };
      nodes.push(node);
      placed.set(id, node);
    });
  });

  const edges: LayoutEdge[] = [];
  for (const step of steps) {
    const target = placed.get(step.id);
    if (!target) continue;
    for (const dependency of step.depends_on) {
      const source = placed.get(dependency);
      if (!source) continue;
      const from = vertical
        ? { x: source.x + source.width / 2, y: source.y + source.height }
        : { x: source.x + source.width, y: source.y + source.height / 2 };
      const to = vertical
        ? { x: target.x + target.width / 2, y: target.y }
        : { x: target.x, y: target.y + target.height / 2 };
      edges.push({
        id: `${dependency}->${step.id}`,
        source: dependency,
        target: step.id,
        path: bezier(from, to, vertical),
        labelX: (from.x + to.x) / 2,
        labelY: (from.y + to.y) / 2,
        long: target.layer - source.layer > 1,
      });
    }
  }

  const width =
    Math.max(...nodes.map((node) => node.x + node.width)) + settings.padding;
  const height =
    Math.max(...nodes.map((node) => node.y + node.height)) + settings.padding;

  return { nodes, edges, width, height, layers };
}

/** Scale + translate that fits `layout` inside a viewport, capped at `maxZoom`. */
export function fitToViewport(
  layout: Layout,
  viewport: { width: number; height: number },
  { padding = 24, maxZoom = 1 } = {},
): { scale: number; translateX: number; translateY: number } {
  if (!layout.width || !layout.height || !viewport.width || !viewport.height) {
    return { scale: 1, translateX: 0, translateY: 0 };
  }
  const scale = Math.min(
    maxZoom,
    (viewport.width - padding * 2) / layout.width,
    (viewport.height - padding * 2) / layout.height,
  );
  const safeScale = Number.isFinite(scale) && scale > 0 ? scale : 1;
  return {
    scale: safeScale,
    translateX: (viewport.width - layout.width * safeScale) / 2,
    translateY: (viewport.height - layout.height * safeScale) / 2,
  };
}

/** Count edge crossings — used by the layout tests to guard the ordering pass. */
export function countCrossings(layout: Layout): number {
  const position = new Map(layout.nodes.map((node) => [node.id, node]));
  let crossings = 0;
  for (let i = 0; i < layout.edges.length; i += 1) {
    for (let j = i + 1; j < layout.edges.length; j += 1) {
      const a = layout.edges[i];
      const b = layout.edges[j];
      const a1 = position.get(a.source);
      const a2 = position.get(a.target);
      const b1 = position.get(b.source);
      const b2 = position.get(b.target);
      if (!a1 || !a2 || !b1 || !b2) continue;
      if (a1.layer !== b1.layer || a2.layer !== b2.layer) continue;
      const sameStart = Math.sign(a1.order - b1.order);
      const sameEnd = Math.sign(a2.order - b2.order);
      if (sameStart !== 0 && sameEnd !== 0 && sameStart !== sameEnd) crossings += 1;
    }
  }
  return crossings;
}
