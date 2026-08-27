/**
 * `@loomcraft/renderer` — React components and state for LoomCraft plans.
 *
 * Three layers, use as many as you need:
 *
 * 1. **State** — `reduceLoomEvent` / `hydrateLoomState` are pure functions over
 *    the event stream. Framework-agnostic; usable from Redux, Zustand, or Svelte.
 * 2. **Transport** — `LoomClient` speaks the HTTP + SSE API.
 * 3. **UI** — `useLoomSession` wires them together; `PlanGraph` and the panels
 *    render them; `LoomWorkbench` is a ready-made layout of all of it.
 *
 * Import `@loomcraft/renderer/styles.css` once for the default theme, then
 * override `--lc-*` custom properties to match your design system.
 */

export * from "./types";
export * from "./state";
export * from "./layout";
export * from "./client";
export * from "./useLoomSession";
export { PlanGraph, StepDetail } from "./components/PlanGraph";
export type { PlanGraphProps, StepDetailProps } from "./components/PlanGraph";
export {
  ApprovalPanel,
  ArtifactList,
  ExecutionList,
  InputRequestPanel,
  OrientationPanel,
  PlanProgress,
  Timeline,
  formatBytes,
} from "./components/panels";
export { LoomWorkbench } from "./components/Workbench";
export type { LoomWorkbenchProps } from "./components/Workbench";
