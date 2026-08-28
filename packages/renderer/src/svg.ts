import { layoutPlan, type LayoutOptions } from "./layout.js";
import type { Plan, StepStatus } from "./types.js";

const colors: Record<StepStatus, string> = {
  pending: "#84918d",
  ready: "#b9c56f",
  running: "#83b8e6",
  waiting_approval: "#e6bd72",
  succeeded: "#78d19d",
  failed: "#f08c88",
  skipped: "#687b76",
  cancelled: "#687b76",
};

function escapeXml(value: string): string {
  return value.replace(/[&<>"']/g, (character) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", "\"": "&quot;", "'": "&apos;" }[character] ?? character));
}

/** Render a standalone, dependency-free SVG snapshot for reports or exports. */
export function renderPlanSvg(plan: Plan, layoutOptions?: LayoutOptions): string {
  const graph = layoutPlan(plan, layoutOptions);
  const points = new Map(graph.nodes.map((node) => [node.id, node]));
  const edges = graph.edges.map((edge) => {
    const source = points.get(edge.source);
    const target = points.get(edge.target);
    if (!source || !target) return "";
    const sourceStep = plan.steps.find((item) => item.id === edge.source);
    const targetStep = plan.steps.find((item) => item.id === edge.target);
    const active = sourceStep?.status === "succeeded" && targetStep?.status === "running";
    const complete = sourceStep?.status === "succeeded" && targetStep?.status === "succeeded";
    const stroke = active ? "#83b8e6" : complete ? "#78d19d" : "#84918d";
    return `<path d="${edge.path}" fill="none" stroke="${stroke}" stroke-width="${active ? 2.5 : 1.6}" marker-end="url(#arrow)"${edge.long ? ' stroke-dasharray="5 4"' : ""}/>`;
  }).join("");
  const nodes = graph.nodes.map((point) => {
    const step = plan.steps.find((item) => item.id === point.id);
    if (!step) return "";
    const tone = colors[step.status] ?? colors.pending;
    return `<g transform="translate(${point.x} ${point.y})"><rect width="${point.width}" height="${point.height}" rx="13" fill="#1b2b29" stroke="${tone}" stroke-width="1.3"/><text x="18" y="30" fill="#e9f0ed" font-family="system-ui,sans-serif" font-size="12" font-weight="650">${escapeXml(step.title)}</text><text x="18" y="51" fill="#9aaba6" font-family="monospace" font-size="9">${escapeXml(step.id)} · ${escapeXml(step.status)}</text></g>`;
  }).join("");
  return `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 ${graph.width} ${graph.height}" role="img" aria-label="${escapeXml(plan.goal)}"><defs><marker id="arrow" markerWidth="8" markerHeight="8" refX="7" refY="4" orient="auto"><path d="M0,0 L8,4 L0,8z" fill="#84918d"/></marker></defs><rect width="100%" height="100%" fill="#111c1b"/>${edges}${nodes}</svg>`;
}
